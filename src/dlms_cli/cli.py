"""DLMS CLI entry point.

Subcommands shipped in Task #4:
  dlms init     — write dlms.toml, create .dlms/ + atoms.sqlite (schema applied)
  dlms ingest   — record HEAD SHA per workspace root (ingesters are stubs for now)
  dlms status   — show config path, db path, atom count, last-indexed SHA per repo

The ingesters listed in dlms.toml (`readme`, `manifest`, `schema`, `git`,
`symbols`) are implemented in later tasks. This scaffold establishes the
control surface so subsequent tasks can plug adapters into the `jobs` queue.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, config, doctor, embeddings, git_utils, ingesters, retrieval, store
from . import atoms as atoms_mod
from .ingesters.base import IngestContext
from .paths import Layout, detect_layout, layout_for

app = typer.Typer(
    name="dlms",
    no_args_is_help=True,
    add_completion=False,
    help="Dynamic Living Memory Substrate.",
)
console = Console()


def _repo_id(root: Path) -> str:
    """Stable repo_id derived from absolute path. 12 hex chars is plenty."""
    return hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]


def _resolve_root_path(layout: Layout, raw: str) -> Path:
    p = Path(raw)
    return (p if p.is_absolute() else layout.root / p).resolve()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing dlms.toml."),
    all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="After init, also run ingest + embed (one-shot setup).",
    ),
) -> None:
    """Initialize DLMS in the current workspace.

    Writes `dlms.toml` (if missing), creates `.dlms/` with `atoms.sqlite`
    initialized from `schema.sql`, and registers each configured root in
    `repo_state`.

    Use `--all` (or `-a`) to chain `ingest` + `embed` immediately after init —
    one command, fully set up.
    """
    layout = detect_layout()
    name = layout.root.name

    # 1. Write dlms.toml
    if layout.config.exists() and not force:
        console.print(f"[yellow]·[/yellow] {layout.config.name} exists (use --force to overwrite)")
    else:
        layout.config.write_text(config.render_default(name=name, roots=["."]))
        action = "rewrote" if force else "created"
        console.print(f"[green]✓[/green] {action} {layout.config.relative_to(layout.root)}")

    # 2. Create .dlms/
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.handoffs.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/green] state dir {layout.state_dir.relative_to(layout.root)}/")

    # 3. Initialize sqlite
    cfg = config.load(layout.config)
    with store.connect(layout.db) as conn:
        version = store.init_schema(conn)
        for raw_root in cfg.workspace.roots:
            rp = _resolve_root_path(layout, raw_root)
            store.register_repo(conn, repo_id=_repo_id(rp), root_path=str(rp))
    console.print(
        f"[green]✓[/green] atoms.sqlite (schema v{version}, "
        f"{len(cfg.workspace.roots)} root(s) registered)"
    )
    if all:
        console.print()
        console.print("[bold]→ ingest[/bold]")
        ingest(budget=None)
        console.print()
        console.print("[bold]→ embed[/bold]")
        embed(limit=500)
        console.print()
        console.print("[green]✓[/green] workspace ready — try [bold]dlms status[/bold]")
    else:
        console.print()
        console.print(f"Next: [bold]dlms ingest[/bold]  (budget: {cfg.bootstrap.budget_seconds}s)")
        console.print("Or run [bold]dlms init --all[/bold] next time to chain init + ingest + embed.")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    budget: int | None = typer.Option(
        None, "--budget", help="Time budget in seconds (overrides dlms.toml)."
    ),
) -> None:
    """Run ingestion across configured roots.

    Walks each configured root, runs every enabled adapter
    (readme/manifest/schema/git/symbols), and reports per-adapter atom counts.
    `last_indexed_sha` from a previous ingest is fed to stateful adapters so
    incremental runs are cheap.
    """
    layout = detect_layout()
    if not layout.config.exists():
        console.print("[red]✗[/red] no dlms.toml — run [bold]dlms init[/bold] first")
        raise typer.Exit(code=2)

    cfg = config.load(layout.config)
    effective_budget = budget if budget is not None else cfg.bootstrap.budget_seconds
    enabled = cfg.ingesters.enabled or []

    started = time.monotonic()
    table = Table(
        title=f"ingest (budget {effective_budget}s, adapters: {', '.join(enabled) or 'none'})"
    )
    table.add_column("root", style="cyan")
    table.add_column("branch")
    table.add_column("head")
    for name in enabled:
        table.add_column(name, justify="right")
    table.add_column("total", justify="right", style="bold")

    grand_total = 0
    with store.connect(layout.db) as conn:
        for raw_root in cfg.workspace.roots:
            rp = _resolve_root_path(layout, raw_root)
            if not rp.exists():
                row = [str(rp), "-", "-"] + ["-"] * len(enabled) + ["[red]missing[/red]"]
                table.add_row(*row)
                continue
            rid = _repo_id(rp)
            store.register_repo(conn, repo_id=rid, root_path=str(rp))
            prior_sha = _prior_indexed_sha(conn, rid)
            sha = git_utils.head_sha(rp)
            branch = git_utils.current_branch(rp)
            # Incremental ingest (SPEC §14.2): None on first run → full scan;
            # otherwise only files changed since the last indexed sha.
            changed = git_utils.changed_files_since(rp, prior_sha)

            remaining = max(0.0, effective_budget - (time.monotonic() - started))
            ctx = IngestContext(
                conn=conn,
                root=rp,
                repo_id=rid,
                workspace_id="default",
                last_indexed_sha=prior_sha,
                exclude=cfg.scan.exclude,
                max_file_kb=cfg.scan.max_file_kb,
                changed_files=changed,
            )
            results = ingesters.run(ctx, enabled=enabled, budget_seconds=remaining)

            # Only advance the indexed sha when the run was COMPLETE. If any
            # adapter was budget-skipped or hit an internal cap (truncated),
            # keep the prior sha so the next incremental run re-derives the same
            # changed set and retries the skipped files — otherwise they'd be
            # permanently orphaned (SPEC §14.2).
            truncated = any(r.truncated for r in results.values() if r is not None)
            if not truncated:
                store.mark_indexed(conn, repo_id=rid, sha=sha, branch=branch)

            row_total = 0
            cells: list[str] = []
            for name in enabled:
                r = results.get(name)
                if r is None or not r.ok:
                    label = r.status_label() if r else "n/a"
                    cells.append(f"[dim]{label}[/dim]")
                else:
                    cells.append(str(r.atoms_inserted))
                    row_total += r.atoms_inserted
            grand_total += row_total
            rel = (
                str(rp.relative_to(layout.root))
                if rp.is_relative_to(layout.root) else str(rp)
            )
            table.add_row(rel, branch or "-", (sha[:7] if sha else "-"), *cells, str(row_total))

    elapsed = time.monotonic() - started
    console.print(table)
    console.print(f"[dim]done in {elapsed:.2f}s — {grand_total} atoms inserted.[/dim]")


def _prior_indexed_sha(conn, repo_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_indexed_sha FROM repo_state WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    return row["last_indexed_sha"] if row else None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status() -> None:
    """Show DLMS state: config path, db path, schema version, atom count, repos."""
    layout = detect_layout()
    if not layout.config.exists() and not layout.db.exists():
        console.print(
            f"[yellow]·[/yellow] DLMS not initialized in {layout.root}. "
            f"Run [bold]dlms init[/bold]."
        )
        raise typer.Exit(code=1)

    console.print(f"[bold]workspace[/bold]  {layout.root}")
    console.print(f"[bold]config[/bold]     {layout.config}"
                  f"{'  [dim](missing)[/dim]' if not layout.config.exists() else ''}")
    console.print(f"[bold]db[/bold]         {layout.db}"
                  f"{'  [dim](missing)[/dim]' if not layout.db.exists() else ''}")

    if not layout.db.exists():
        raise typer.Exit(code=1)

    with store.connect(layout.db) as conn:
        version = store.schema_version(conn)
        n = store.atom_count(conn)
        console.print(f"[bold]schema[/bold]     v{version}")
        console.print(f"[bold]atoms[/bold]      {n} live")
        rows = store.repo_rows(conn)

    if not rows:
        console.print("[dim]no repos registered yet — run `dlms init` or `dlms ingest`[/dim]")
        return

    table = Table(title=f"repos ({len(rows)})")
    table.add_column("repo_id", style="cyan")
    table.add_column("root")
    table.add_column("branch")
    table.add_column("last sha")
    table.add_column("last indexed")
    for r in rows:
        ts = r["last_indexed_at"]
        when = (
            dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%MZ")
            if ts else "-"
        )
        table.add_row(
            r["repo_id"],
            r["root_path"],
            r["last_branch"] or "-",
            (r["last_indexed_sha"][:7] if r["last_indexed_sha"] else "-"),
            when,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------

@app.command()
def embed(
    limit: int = typer.Option(500, "--limit", help="Max atoms to embed this run."),
) -> None:
    """Embed live atoms whose summary changed since last run.

    Uses the deterministic hash backend unless the ``dlms[embed]`` extra is
    installed (then sentence-transformers + bge-small-en-v1.5).
    """
    layout = detect_layout()
    if not layout.db.exists():
        console.print("[red]✗[/red] no atoms.sqlite — run [bold]dlms init[/bold] first")
        raise typer.Exit(code=2)
    embedder = embeddings.default_embedder()
    with store.connect(layout.db) as conn:
        n = embeddings.embed_pending(conn, embedder=embedder, limit=limit)
    console.print(f"[green]✓[/green] embedded {n} atom(s) with [bold]{embedder.name}[/bold]")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@app.command()
def query(
    text: str = typer.Argument(..., help="Natural-language query."),
    files: list[str] = typer.Option(  # noqa: B008
        None, "--file", "-f", help="File context for structural seeding."
    ),
    top_n: int = typer.Option(10, "--top", help="Number of atoms to return."),
) -> None:
    """Retrieve relevant atoms via PPR over the typed edge graph."""
    layout = detect_layout()
    if not layout.db.exists():
        console.print("[red]✗[/red] no atoms.sqlite — run [bold]dlms init[/bold] first")
        raise typer.Exit(code=2)
    with store.connect(layout.db) as conn:
        results = retrieval.retrieve(
            conn, query=text, file_context=files or None, top_n=top_n,
            repo_root=layout.root,
        )
        if not results:
            console.print("[dim]no atoms matched.[/dim]")
            return
        table = Table(title=f"top {len(results)} for: {text!r}")
        table.add_column("score", justify="right")
        table.add_column("type")
        table.add_column("topic_key")
        table.add_column("trail")
        table.add_column("summary")
        for r in results:
            view = atoms_mod.get_atom(conn, r.atom_id)
            if not view:
                continue
            table.add_row(
                f"{r.score:.3f}",
                view.atom.type,
                view.atom.topic_key,
                r.trail(),
                view.summaries.get(50, "")[:60],
            )
    console.print(table)


# ---------------------------------------------------------------------------
# enumerate (edge-case enumerator, SPEC §11)
# ---------------------------------------------------------------------------

@app.command(name="enumerate")
def enumerate_cmd(
    json_out: bool = typer.Option(False, "--json", help="JSON output."),
    max_findings: int = typer.Option(5, "--max", help="Cap on findings."),
) -> None:
    """Enumerate edge cases for the staged diff (SPEC §11)."""
    from . import enumerator
    findings = enumerator.enumerate_edge_cases(max_findings=max_findings)
    if json_out:
        typer.echo(enumerator.to_json(findings))
    else:
        typer.echo(enumerator.format_report(findings))


# ---------------------------------------------------------------------------
# statusline (Memory Pulse, SPEC §13)
# ---------------------------------------------------------------------------

@app.command()
def statusline(
    compact: bool = typer.Option(False, "--compact", help="Single-line output."),
) -> None:
    """Render the Memory Pulse statusline."""
    from . import statusline as sl_mod
    line = sl_mod.build_pulse().render()
    typer.echo(line)


# ---------------------------------------------------------------------------
# handoff (PreCompact hook output)
# ---------------------------------------------------------------------------

@app.command()
def handoff(
    next_action: str = typer.Option("", "--next", help="Literal verb+target."),
    title: str = typer.Option("", "--title", help="Current tick title."),
    reason: str = typer.Option("user_handoff", "--reason"),
    narrative: str = typer.Option("", "--narrative"),
) -> None:
    """Write a handoff document to .dlms/handoffs/ (and refresh LATEST.md)."""
    from . import handoff as handoff_mod
    inp = handoff_mod.HandoffInput(
        current_tick_title=title or None,
        next_action=next_action,
        ended_reason=reason,
        narrative=narrative,
    )
    path = handoff_mod.write(inp)
    typer.echo(str(path))


# ---------------------------------------------------------------------------
# watch (PostToolUse hook output)
# ---------------------------------------------------------------------------

@app.command()
def watch(
    files: list[str] = typer.Argument(..., help="Files touched by the tool call."),  # noqa: B008
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """Re-run liveness predicates for atoms attached to `files`. JSON output."""
    from . import watcher
    rep = watcher.watch(files)
    typer.echo(rep.to_json(pretty=pretty))
    if rep.has_violations:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# route (UserPromptSubmit hook output)
# ---------------------------------------------------------------------------

@app.command()
def route(
    prompt: str = typer.Argument(..., help="The user prompt to classify + retrieve for."),
    file_context: list[str] = typer.Option(  # noqa: B008
        None, "--file", "-f", help="Files providing structural seed context."
    ),
) -> None:
    """Classify a prompt and emit a token-capped atom injection block (JSON)."""
    from . import router
    typer.echo(router.route(prompt, file_context=file_context or None).to_json())


# ---------------------------------------------------------------------------
# digest (SessionStart hook output)
# ---------------------------------------------------------------------------

@app.command()
def digest(
    pretty: bool = typer.Option(False, "--pretty", help="Indent JSON output."),
) -> None:
    """Print the SessionStart digest as JSON (consumed by the SessionStart hook)."""
    from . import digest as digest_mod
    typer.echo(digest_mod.build_digest().to_json(pretty=pretty))


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------

@app.command()
def mcp(
    transport: str = typer.Option(
        "stdio", "--transport", "-t",
        help="MCP transport: stdio (local clients) | http | sse (remote/web agents).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for http/sse."),
    port: int = typer.Option(8765, "--port", help="Bind port for http/sse."),
) -> None:
    """Run the DLMS MCP server. Requires the ``dlms[mcp]`` extra.

    Standard Model Context Protocol — usable by any MCP client (Claude Code,
    Claude Desktop, Cursor, Windsurf, Cline, Continue). Default ``stdio`` suits
    local clients that spawn it as a subprocess; use ``--transport http`` to
    serve remote/web agents over a port.
    """
    from . import mcp_server
    mcp_server.run(transport, host=host, port=port)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@app.command(name="doctor")
def doctor_cmd(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show detail line for each finding."
    ),
) -> None:
    """Health check across schema, atoms, liveness, embeddings, edges, retrieval.

    Exit code = number of fail-level findings (0 = green). Warnings do not
    affect exit code — use them as nudges, not gates.
    """
    layout = detect_layout()
    report = doctor.run(layout)

    glyph = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    for f in report.findings:
        console.print(f"{glyph[f.severity]} [bold]{f.name}[/bold]  {f.summary}")
        if verbose and f.detail:
            for line in f.detail.splitlines():
                console.print(f"    [dim]{line}[/dim]")

    fails, warns = report.fail_count, report.warn_count
    if fails == 0 and warns == 0:
        console.print("\n[green]doctor: all green[/green]")
    else:
        console.print(
            f"\n[bold]doctor:[/bold] {fails} fail, {warns} warn, "
            f"{len(report.findings) - fails - warns} ok"
        )
    # Cap exit code at 1 — unix convention is binary success/fail, and bytes
    # >125 wrap on POSIX so a literal fail-count is unreliable past that.
    raise typer.Exit(code=0 if fails == 0 else 1)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

@app.command()
def migrate() -> None:
    """Apply pending schema migrations to the workspace database.

    Brings a DB created under an older schema up to the current version:
    additive column adds (`atoms.tier`), constraint widening (`edges.kind`
    ROLLS_UP via table rebuild), and any missing indexes/views. Idempotent —
    safe to run repeatedly. This is the command `dlms doctor` points you to
    when it reports a schema-version gap.
    """
    import sqlite3

    layout = detect_layout()
    if not layout.db.exists():
        console.print("[red]✗[/red] no atoms.sqlite — run [bold]dlms init[/bold] first")
        raise typer.Exit(code=2)

    # Read the version with a raw connection FIRST — store.connect() runs
    # migrations on open, so going through it would mask the "before" state.
    raw = sqlite3.connect(layout.db)
    try:
        row = raw.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        before = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        before = 0
    finally:
        raw.close()

    with store.connect(layout.db) as conn:
        after = store.init_schema(conn)
        edges_ok = bool(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
            ).fetchone()[0].find("ROLLS_UP") >= 0
        )

    if after > before:
        console.print(f"[green]✓[/green] migrated schema v{before} → v{after}")
    else:
        console.print(f"[green]✓[/green] schema already current (v{after})")
    console.print(f"[dim]edges.kind accepts ROLLS_UP: {edges_ok}[/dim]")


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the DLMS CLI version."""
    console.print(f"dlms {__version__}")


# Keep `layout_for` importable from this module for tests/external callers.
__all__ = ["app", "layout_for"]
