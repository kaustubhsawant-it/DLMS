"""SQLite store — open + initialize via schema.sql.

Embedding tables (sqlite-vss virtual table) are NOT created here; they are
loaded at runtime by the retrieval layer (see SPEC §7). This module owns only
the relational schema and basic CRUD helpers.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .paths import schema_sql_text


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and create) a DLMS sqlite db.

    Uses Python's default deferred-isolation mode so `with conn:` brackets a
    real transaction. Callers SHOULD wrap multi-statement writes in
    `with conn:` to get atomic supersede semantics.

    Runs additive schema migrations on open (idempotent, guarded) so that
    read-only commands — `route`, `query`, `digest`, the MCP server — never
    hit a column added by a later schema version on a DB created under an
    older one. Previously only `init_schema` (run by `init`/`ingest`) migrated,
    so a pre-`tier` atoms.sqlite would fail with "no such column: tier".
    """
    if not isinstance(db_path, str) or db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """Apply schema.sql idempotently. Returns the schema_version after apply.

    Migrations run BEFORE the schema script: schema.sql creates indexes/views
    that reference columns added by later versions (e.g. `idx_atoms_tier` on
    `atoms.tier`). On a DB created under an older schema, `CREATE TABLE IF NOT
    EXISTS atoms` is a no-op so the column is absent, and those dependent
    objects would fail ("no such column: tier") unless the column is ALTERed in
    first. On a fresh DB the migrations are skipped (no tables yet) and the
    script creates everything with current columns.
    """
    _migrate(conn)
    conn.executescript(schema_sql_text())
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations for DBs created under an older schema.

    Runs ahead of schema.sql so columns exist before the script's dependent
    indexes/views are (re)created. Each step is guarded on the target table
    existing and on the column/constraint being absent, so it is a safe no-op
    on both a fresh DB and an already-migrated one.
    """
    if not _table_exists(conn, "atoms"):
        return  # fresh DB — schema.sql creates everything with current columns
    _migrate_v2_tier(conn)
    _migrate_v2_edges_rolls_up(conn)


def _migrate_v2_tier(conn: sqlite3.Connection) -> None:
    """v2 (SPEC §14.1): add `atoms.tier`. SQLite ADD COLUMN can't carry a CHECK,
    but the NOT NULL DEFAULT backfills existing rows to the leaf tier."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    if "tier" not in cols:
        with conn:
            conn.execute(
                "ALTER TABLE atoms ADD COLUMN tier TEXT NOT NULL DEFAULT 'symbol'"
            )


def _migrate_v2_edges_rolls_up(conn: sqlite3.Connection) -> None:
    """v2 (SPEC §14.1): widen the `edges.kind` CHECK to accept `ROLLS_UP`.

    A column CHECK can't be altered in place, so a pre-v2 `edges` table silently
    rejects the new kind — a module-atom ingest would fail with a CHECK
    violation when writing roll-up edges. The supported fix is rebuild:
    rename → create-widened → copy → drop → recreate indexes, preserving every
    row and the id sequence (so `edge_evidence.edge_id` FKs stay valid). FKs are
    toggled off for the swap and restored after; the toggle sits OUTSIDE any
    transaction because `PRAGMA foreign_keys` is a no-op inside one.

    No-op when `edges` is absent (fresh DB) or its DDL already lists ROLLS_UP.
    """
    if not _table_exists(conn, "edges"):
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    if row and row[0] and "ROLLS_UP" in row[0]:
        return  # already widened (fresh schema or prior migration)

    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            # SQL is left-aligned in the heredoc (whitespace is insignificant to
            # SQLite) so the index/CHECK lines stay under the 100-col limit.
            conn.executescript(
                """
ALTER TABLE edges RENAME TO _edges_old;

CREATE TABLE edges (
  id          INTEGER PRIMARY KEY,
  src_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  dst_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK(kind IN (
                'MIRRORS','IMPLEMENTS','SUPERSEDES','CONTRADICTS',
                'LOCATED_IN','REFERENCES','DEPENDS_ON','OWNS','BLOCKS',
                'CO_CHANGED','DERIVED_FROM','ROLLS_UP'
              )),
  directed    INTEGER NOT NULL DEFAULT 0,
  weight      REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
  confidence  REAL NOT NULL DEFAULT 1.0,
  valid_from  INTEGER NOT NULL,
  valid_to    INTEGER,
  source      TEXT NOT NULL CHECK(source IN (
                'shared_ref','embed_sim','commit_couple','llm_extract',
                'git_blame','manual','schema_diff'
              )),
  status      TEXT NOT NULL DEFAULT 'live'
                CHECK(status IN ('live','suggested','rejected')),
  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE(src_id, dst_id, kind, valid_from)
) STRICT;

INSERT INTO edges
  SELECT id, src_id, dst_id, kind, directed, weight, confidence,
         valid_from, valid_to, source, status, created_at
    FROM _edges_old;

DROP TABLE _edges_old;

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind, weight DESC) WHERE valid_to IS NULL;
"""
            )
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_on else 'OFF'}")


def register_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    root_path: str,
    workspace_id: str = "default",
) -> None:
    conn.execute(
        """INSERT INTO repo_state (repo_id, workspace_id, root_path)
           VALUES (?, ?, ?)
           ON CONFLICT(repo_id) DO UPDATE SET root_path = excluded.root_path""",
        (repo_id, workspace_id, root_path),
    )


def mark_indexed(
    conn: sqlite3.Connection,
    repo_id: str,
    sha: str | None,
    branch: str | None,
) -> None:
    conn.execute(
        """UPDATE repo_state
              SET last_indexed_sha = ?,
                  last_indexed_at  = ?,
                  last_branch      = ?
            WHERE repo_id = ?""",
        (sha, int(time.time()), branch, repo_id),
    )


def atom_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM live_atoms").fetchone()
    return int(row["c"]) if row else 0


def repo_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM repo_state ORDER BY repo_id"))


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0
