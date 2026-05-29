"""DLMS MCP server — exposes the atom store over the Model Context Protocol.

This is a standard MCP server: any MCP-capable client (Claude Code, Claude
Desktop, Cursor, Windsurf, Cline, Continue, …) can launch it and call its
tools. Nothing here is Claude-specific. See the README "Connecting other
agents" section for per-client config snippets.

Tools exposed (matching SPEC §6 + sub-agent JSON contract in §10):

* ``query_facts(query, file_context, top_n)`` — PPR retrieval, returns
  atom rows with provenance trails.
* ``assert_fact(type, topic_key, summary_50w, ...)`` — write-back path used
  by the Remember phase.
* ``supersede(old_id, new_id)`` — explicit closure of a prior fact.
* ``graph_neighbors(atom_id, hops, kinds)`` — BFS over live edges.
* ``embed_status()`` — number of embedded atoms + model name in use.
* ``ping()`` — health check.

Run with ``dlms mcp`` (stdio, default — what desktop/CLI MCP clients use) or
``dlms mcp --transport http --port 8765`` for remote/web agents.

FastMCP is an *optional* extra (``dlms[mcp]``). When the import fails we
print a helpful install hint instead of crashing.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import atoms as atoms_mod
from . import edges as edges_mod
from . import embeddings, retrieval, store
from .paths import detect_layout

# Transports we accept on the `dlms mcp` surface. stdio is the local default;
# http/sse expose the server over a port for remote or web-based agents.
SUPPORTED_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http", "sse"})


def _open() -> Any:
    layout = detect_layout()
    if not layout.db.exists():
        raise FileNotFoundError(
            f"no atoms.sqlite at {layout.db}; run `dlms init` then `dlms ingest`"
        )
    return store.connect(layout.db)


def _atom_to_dict(view: atoms_mod.AtomView) -> dict[str, Any]:
    return {
        "id": view.atom.id,
        "type": view.atom.type,
        "topic_key": view.atom.topic_key,
        "decision_status": view.atom.decision_status,
        "summary_10w": view.summaries.get(10),
        "summary_50w": view.summaries.get(50),
        "summary_250w": view.summaries.get(250),
        "source_kind": view.atom.source_kind,
        "source_ref": view.atom.source_ref,
        "valid_from": view.atom.valid_from,
        "confidence": view.atom.confidence,
        "repo_id": view.atom.repo_id,
    }


def build_server():  # noqa: ANN201 — return type is FastMCP, not always importable
    """Build (but don't run) the FastMCP server. Importable for tests."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover — exercised in `dlms mcp`
        raise RuntimeError(
            "fastmcp not installed. Install with: pip install 'dlms[mcp]'"
        ) from exc

    server = FastMCP("dlms")

    @server.tool()
    def ping() -> dict[str, str]:
        """Health check. Returns workspace path."""
        return {"status": "ok", "workspace": str(detect_layout().root)}

    @server.tool()
    def query_facts(
        query: str,
        file_context: list[str] | None = None,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """Retrieve atoms relevant to `query`, optionally seeded by file paths."""
        with _open() as conn:
            hits = retrieval.retrieve(
                conn, query=query, file_context=file_context, top_n=top_n,
                repo_root=detect_layout().root,
            )
            out: list[dict[str, Any]] = []
            for h in hits:
                view = atoms_mod.get_atom(conn, h.atom_id)
                if not view:
                    continue
                row = _atom_to_dict(view)
                row["_retrieval"] = {
                    "score": h.score,
                    "trail": h.trail(),
                    "depth": h.depth,
                    "via_kind": h.via_kind,
                    "seed": h.seed,
                }
                out.append(row)
            return out

    @server.tool()
    def assert_fact(
        type: str,
        topic_key: str,
        summary_50w: str,
        source_kind: str = "manual",
        source_ref: str | None = None,
        decision_status: str | None = None,
        summary_10w: str | None = None,
        summary_250w: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Write a new atom (or no-op if identical to an existing live atom)."""
        with _open() as conn:
            atom = atoms_mod.assert_fact(
                conn,
                type=type,  # type: ignore[arg-type]
                topic_key=topic_key,
                summary_50w=summary_50w,
                summary_10w=summary_10w,
                summary_250w=summary_250w,
                source_kind=source_kind,  # type: ignore[arg-type]
                source_ref=source_ref,
                decision_status=decision_status,  # type: ignore[arg-type]
                confidence=confidence,
            )
            return {"id": atom.id, "topic_key": atom.topic_key, "type": atom.type}

    @server.tool()
    def supersede(old_id: str, new_id: str) -> dict[str, str]:
        """Close `old_id` and point its supersession at `new_id`."""
        with _open() as conn:
            atoms_mod.supersede(conn, old_id=old_id, new_id=new_id)
        return {"status": "ok", "old": old_id, "new": new_id}

    @server.tool()
    def graph_neighbors(
        atom_id: str,
        hops: int = 1,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """BFS over live edges from `atom_id`. Returns hop metadata + atom summary."""
        with _open() as conn:
            hops_out = edges_mod.graph_neighbors(
                conn, atom_id, hops=hops, kinds=kinds  # type: ignore[arg-type]
            )
            out: list[dict[str, Any]] = []
            for hop in hops_out:
                view = atoms_mod.get_atom(conn, hop.atom_id)
                if not view:
                    continue
                row = _atom_to_dict(view)
                row["_hop"] = {
                    "depth": hop.depth,
                    "via_kind": hop.via_kind,
                    "via_edge": hop.via_edge,
                    "weight_acc": hop.weight_acc,
                }
                out.append(row)
            return out

    @server.tool()
    def embed_status() -> dict[str, Any]:
        """Embeddings progress for the live atom set."""
        with _open() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM live_atoms").fetchone()["c"]
            embedded = conn.execute(
                """SELECT COUNT(*) AS c FROM atom_embeddings e
                     JOIN live_atoms a ON a.id = e.atom_id"""
            ).fetchone()["c"]
            model_row = conn.execute(
                "SELECT model FROM atom_embeddings GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
        return {
            "live_atoms": total,
            "embedded": embedded,
            "model": (model_row["model"] if model_row else embeddings.default_embedder().name),
        }

    return server


def run(
    transport: str = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Entry point — build and run the MCP server on the chosen transport.

    * ``stdio`` (default) — for local clients that spawn the server as a
      subprocess (Claude Code/Desktop, Cursor, Windsurf, Cline, Continue).
    * ``http`` / ``sse`` — bind ``host:port`` so remote or browser-based
      agents can connect over the network.

    Transport is validated before FastMCP is imported so an obvious typo fails
    fast with a clear message rather than a deep library traceback.
    """
    if transport not in SUPPORTED_TRANSPORTS:
        print(
            json.dumps({
                "error": f"unsupported transport {transport!r}; "
                         f"choose one of {sorted(SUPPORTED_TRANSPORTS)}"
            }),
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        server = build_server()
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(2)
    if transport == "stdio":
        server.run()  # FastMCP defaults to stdio transport.
    else:
        server.run(transport=transport, host=host, port=port)


__all__ = ["SUPPORTED_TRANSPORTS", "build_server", "run"]
