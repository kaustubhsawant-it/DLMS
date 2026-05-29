"""Health-check command — `dlms doctor`.

Composes existing helpers to produce a one-screen health report. Read-only:
opens the db, runs a fixed set of integrity + freshness queries, never writes.

Severity ladder (one finding per check):
  ok       — green ✓, nothing to do
  warn     — yellow ⚠, surface but don't fail
  fail     — red ✗, exit code = count of fails

The goal is to surface silent rot — schema drift, embedding-model drift,
liveness coverage erosion, dead retrieval pipeline — before they turn into
mystery hallucinations during a real tick.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import store
from .paths import Layout

Severity = Literal["ok", "warn", "fail"]

EXPECTED_SCHEMA_VERSION = 2  # v2: hierarchical atoms (SPEC §14.1)
STALE_INDEX_WARN_SECONDS = 30 * 86_400          # repo not re-indexed in 30d
RETRIEVAL_IDLE_WARN_SECONDS = 7 * 86_400        # no routed queries in 7d
LIVENESS_STALE_THRESHOLD_SECONDS = 24 * 3_600   # matches statusline default


@dataclass(frozen=True)
class Finding:
    name: str
    severity: Severity
    summary: str          # one-line, ≤80 chars
    detail: str | None = None  # multi-line, shown under -v


@dataclass(frozen=True)
class Report:
    findings: list[Finding]

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")


def run(layout: Layout) -> Report:
    """Run every check against `layout`. Each check is independent; one failing
    check never short-circuits the others — operators need the full picture."""
    findings: list[Finding] = []

    # Layout presence is the precondition for every db-touching check.
    layout_ok = _check_layout(layout, findings)
    if not layout_ok:
        return Report(findings)

    # All db checks share one connection (read-only intent, but sqlite3 in
    # Python doesn't expose read-only mode without a URI; we just never write).
    try:
        conn = store.connect(layout.db)
    except sqlite3.Error as e:
        findings.append(Finding(
            name="db.open",
            severity="fail",
            summary=f"cannot open atoms.sqlite: {e}",
        ))
        return Report(findings)

    checks: list[tuple[str, Callable[[], None]]] = [
        ("schema.version", lambda: _check_schema(conn, findings)),
        ("db.integrity", lambda: _check_integrity(conn, findings)),
        ("db.journal", lambda: _check_journal_mode(conn, findings)),
        ("atoms.census", lambda: _check_atom_census(conn, findings)),
        ("liveness.coverage", lambda: _check_liveness_coverage(conn, findings)),
        ("liveness.freshness", lambda: _check_liveness_freshness(conn, findings)),
        ("embeddings.drift", lambda: _check_embedding_drift(conn, findings)),
        ("edges.density", lambda: _check_edge_density(conn, findings)),
        ("retrieval.activity", lambda: _check_retrieval_activity(conn, findings)),
        ("repo.freshness", lambda: _check_repo_freshness(conn, findings)),
        ("handoff.dir", lambda: _check_handoff_dir(layout, findings)),
    ]
    try:
        for name, fn in checks:
            try:
                fn()
            except sqlite3.OperationalError as e:
                # Missing table/view on old schema is a known degradation path.
                findings.append(Finding(
                    name=name, severity="warn",
                    summary=f"check skipped: {e}",
                ))
            except Exception as e:  # noqa: BLE001 — one bad check must not kill the report
                findings.append(Finding(
                    name=name, severity="fail",
                    summary=f"check crashed: {type(e).__name__}: {e}",
                ))
    finally:
        conn.close()

    return Report(findings)


# ---------------------------------------------------------------------------
# individual checks — each appends 0 or 1 finding
# ---------------------------------------------------------------------------

def _check_layout(layout: Layout, findings: list[Finding]) -> bool:
    if not layout.state_dir.exists():
        findings.append(Finding(
            name="layout.state_dir",
            severity="fail",
            summary=f"missing {layout.state_dir} — run `dlms init`",
        ))
        return False
    if not layout.db.exists():
        findings.append(Finding(
            name="layout.db",
            severity="fail",
            summary=f"missing {layout.db} — run `dlms init`",
        ))
        return False
    findings.append(Finding(
        name="layout",
        severity="ok",
        summary=f"workspace {layout.root}",
    ))
    return True


def _check_schema(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    v = store.schema_version(conn)
    if v == EXPECTED_SCHEMA_VERSION:
        findings.append(Finding(
            name="schema.version",
            severity="ok",
            summary=f"v{v}",
        ))
    elif v < EXPECTED_SCHEMA_VERSION:
        # warn (not fail): doctor is the tool you reach for when something is
        # wrong — bumping exit code here discourages exactly the diagnosis run
        # the user needs. Later checks may surface table-missing warnings as
        # the schema gap manifests.
        findings.append(Finding(
            name="schema.version",
            severity="warn",
            summary=f"db at v{v}, expected v{EXPECTED_SCHEMA_VERSION} — run `dlms migrate`",
        ))
    else:
        # v > expected: db newer than installed CLI. Don't auto-downgrade.
        findings.append(Finding(
            name="schema.version",
            severity="warn",
            summary=f"db at v{v}, CLI expects v{EXPECTED_SCHEMA_VERSION} — CLI may be out of date",
        ))


def _check_integrity(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    msg = row[0] if row else "no result"
    if msg == "ok":
        findings.append(Finding(name="db.integrity", severity="ok", summary="integrity_check ok"))
    else:
        findings.append(Finding(
            name="db.integrity",
            severity="fail",
            summary="sqlite integrity_check failed",
            detail=msg,
        ))


def _check_journal_mode(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = (row[0] if row else "").lower()
    if mode == "wal":
        findings.append(Finding(name="db.journal", severity="ok", summary="WAL active"))
    else:
        # schema.sql asks for WAL; if we got something else the pragma may have
        # been overridden by a tool. Not fatal, but worth surfacing.
        findings.append(Finding(
            name="db.journal",
            severity="warn",
            summary=f"journal_mode={mode!r}, expected wal — concurrent writers may block",
        ))


def _check_atom_census(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = store.atom_count(conn)
    if total == 0:
        findings.append(Finding(
            name="atoms.census",
            severity="warn",
            summary="0 live atoms — run `dlms ingest` or start asserting facts",
        ))
        return
    by_type = dict(conn.execute(
        "SELECT type, COUNT(*) c FROM live_atoms GROUP BY type ORDER BY c DESC"
    ).fetchall())
    pinned = conn.execute("SELECT COUNT(*) FROM live_atoms WHERE pinned = 1").fetchone()[0]
    detail = ", ".join(f"{t}:{n}" for t, n in by_type.items())
    findings.append(Finding(
        name="atoms.census",
        severity="ok",
        summary=f"{total} live atoms ({pinned} pinned)",
        detail=detail,
    ))


def _check_liveness_coverage(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = store.atom_count(conn)
    if total == 0:
        return  # already flagged by census
    with_kind = conn.execute(
        "SELECT COUNT(*) FROM live_atoms WHERE liveness_kind IS NOT NULL "
        "AND liveness_kind != 'none'"
    ).fetchone()[0]
    pct = (with_kind / total) * 100
    summary = f"{with_kind}/{total} atoms carry a liveness predicate ({pct:.0f}%)"
    if pct >= 60:
        sev: Severity = "ok"
    elif pct >= 30:
        sev = "warn"
    else:
        sev = "warn"  # not fatal — some atom types (decision, owner) legitimately lack predicates
    findings.append(Finding(name="liveness.coverage", severity=sev, summary=summary))


def _check_liveness_freshness(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    cutoff = int(time.time()) - LIVENESS_STALE_THRESHOLD_SECONDS
    total_with_kind = conn.execute(
        "SELECT COUNT(*) FROM live_atoms "
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none'"
    ).fetchone()[0]
    stale = conn.execute(
        "SELECT COUNT(*) FROM live_atoms "
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none' "
        "   AND (liveness_last_ok IS NULL OR liveness_last_ok < ?)",
        (cutoff,),
    ).fetchone()[0]
    if stale == 0:
        findings.append(Finding(
            name="liveness.freshness",
            severity="ok",
            summary="no stale atoms (all predicates passed in last 24h)",
        ))
        return
    # Use a relative threshold so the boundary scales with workspace size.
    # Absolute floor (5) prevents nuisance fails on tiny workspaces.
    pct_stale = (stale / total_with_kind * 100) if total_with_kind else 0
    if pct_stale >= 20 and stale >= 5:
        sev: Severity = "fail"
    else:
        sev = "warn"
    findings.append(Finding(
        name="liveness.freshness",
        severity=sev,
        summary=(
            f"{stale}/{total_with_kind} predicates stale "
            f"({pct_stale:.0f}%) — last_ok > 24h"
        ),
        detail="Run `dlms watch --all` or investigate via `dlms query`.",
    ))


def _check_embedding_drift(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    rows = conn.execute(
        "SELECT model, COUNT(*) c FROM atom_embeddings GROUP BY model"
    ).fetchall()
    if not rows:
        findings.append(Finding(
            name="embeddings.drift",
            severity="ok",
            summary="no embeddings written yet",
        ))
        return
    if len(rows) == 1:
        m, c = rows[0]
        findings.append(Finding(
            name="embeddings.drift",
            severity="ok",
            summary=f"{c} embeddings, single model: {m}",
        ))
    else:
        models = ", ".join(f"{r[0]}({r[1]})" for r in rows)
        findings.append(Finding(
            name="embeddings.drift",
            severity="fail",
            summary=f"{len(rows)} embedding models present — retrieval will be inconsistent",
            detail=models,
        ))


def _check_edge_density(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = conn.execute("SELECT COUNT(*) FROM live_edges").fetchone()[0]
    if total == 0:
        atom_n = store.atom_count(conn)
        if atom_n == 0:
            return  # census already noted no atoms
        findings.append(Finding(
            name="edges.density",
            severity="warn",
            summary=f"0 edges across {atom_n} atoms — auto-discovery may be off",
        ))
        return
    by_kind = dict(conn.execute(
        "SELECT kind, COUNT(*) c FROM live_edges GROUP BY kind ORDER BY c DESC"
    ).fetchall())
    detail = ", ".join(f"{k}:{n}" for k, n in by_kind.items())
    findings.append(Finding(
        name="edges.density",
        severity="ok",
        summary=f"{total} edges across {len(by_kind)} kind(s)",
        detail=detail,
    ))


def _check_retrieval_activity(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    cutoff = int(time.time()) - RETRIEVAL_IDLE_WARN_SECONDS
    recent = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE ts >= ?",
        (cutoff,),
    ).fetchone()[0]
    if recent > 0:
        findings.append(Finding(
            name="retrieval.activity",
            severity="ok",
            summary=f"{recent} retrieval(s) logged in last 7 days",
        ))
    else:
        # Empty log can mean (a) router/hook isn't firing or (b) router doesn't
        # log yet. Either way, surface as a warning, not a fail.
        findings.append(Finding(
            name="retrieval.activity",
            severity="warn",
            summary="no retrievals logged in last 7 days — router may not be wired",
        ))


def _check_repo_freshness(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    rows = store.repo_rows(conn)
    if not rows:
        findings.append(Finding(
            name="repo.freshness",
            severity="warn",
            summary="no repos registered — run `dlms ingest`",
        ))
        return
    cutoff = int(time.time()) - STALE_INDEX_WARN_SECONDS
    stale: list[str] = []
    for r in rows:
        ts = r["last_indexed_at"]
        if ts is None or ts < cutoff:
            stale.append(r["repo_id"])
    if not stale:
        findings.append(Finding(
            name="repo.freshness",
            severity="ok",
            summary=f"{len(rows)} repo(s), all indexed within 30 days",
        ))
    else:
        findings.append(Finding(
            name="repo.freshness",
            severity="warn",
            summary=f"{len(stale)}/{len(rows)} repo(s) not re-indexed in 30+ days",
            detail=", ".join(stale),
        ))


def _check_handoff_dir(layout: Layout, findings: list[Finding]) -> None:
    if not layout.handoffs.exists():
        findings.append(Finding(
            name="handoff.dir",
            severity="warn",
            summary="no handoffs directory — first session hasn't written one",
        ))
        return
    files = [p for p in layout.handoffs.iterdir() if p.is_file() and p.suffix == ".md"]
    if not files:
        findings.append(Finding(
            name="handoff.dir",
            severity="ok",
            summary="handoffs dir exists, no files yet",
        ))
        return
    newest = max(files, key=lambda p: p.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    findings.append(Finding(
        name="handoff.dir",
        severity="ok",
        summary=f"{len(files)} handoff(s), latest {age_h:.1f}h ago",
    ))
