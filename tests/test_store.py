"""Tests for store init + in-place migrations (SPEC §14.1 v2 migration)."""

from __future__ import annotations

from dlms_cli import store


def test_init_schema_reports_current_version(conn):
    assert store.schema_version(conn) == 2


def test_migrate_adds_tier_to_v1_db():
    """A v1 atoms table (no `tier`) must gain the column, backfilled to 'symbol'."""
    conn = store.connect(":memory:")
    # Simulate a pre-v2 atoms table created before the tier column existed.
    conn.execute(
        "CREATE TABLE atoms (id TEXT PRIMARY KEY, type TEXT, topic_key TEXT) STRICT"
    )
    conn.execute("INSERT INTO atoms (id, type, topic_key) VALUES ('a', 'invariant', 'k')")
    assert "tier" not in {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}

    store._migrate(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    assert "tier" in cols
    # Existing row backfilled by the NOT NULL DEFAULT.
    assert conn.execute("SELECT tier FROM atoms WHERE id = 'a'").fetchone()["tier"] == "symbol"
    conn.close()


def test_migrate_is_idempotent():
    """Re-running _migrate on an already-migrated DB is a safe no-op."""
    conn = store.connect(":memory:")
    store.init_schema(conn)
    before = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    store._migrate(conn)  # must not raise or duplicate the column
    after = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    assert before == after
    conn.close()


# A pre-v2 `edges` table: the v1 kind CHECK with no ROLLS_UP. Mirrors a real
# pre-§14.1 DB whose edges table silently rejects the new edge kind.
_V1_EDGES_NARROW = """
CREATE TABLE edges (
  id INTEGER PRIMARY KEY,
  src_id TEXT NOT NULL, dst_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN (
    'MIRRORS','IMPLEMENTS','SUPERSEDES','CONTRADICTS','LOCATED_IN','REFERENCES',
    'DEPENDS_ON','OWNS','BLOCKS','CO_CHANGED','DERIVED_FROM')),
  directed INTEGER NOT NULL DEFAULT 0,
  weight REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
  confidence REAL NOT NULL DEFAULT 1.0,
  valid_from INTEGER NOT NULL, valid_to INTEGER,
  source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'live',
  created_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE(src_id, dst_id, kind, valid_from)
) STRICT;
"""


def test_migrate_widens_edges_check_for_rolls_up():
    """A pre-v2 edges table must be rebuilt to accept ROLLS_UP, preserving rows.

    Without this, ingesting module atoms into an old DB fails with a CHECK
    violation when it tries to write roll-up edges.
    """
    conn = store.connect(":memory:")
    # Minimal atoms table so _migrate doesn't early-return; endpoints for the FK.
    conn.executescript(
        "CREATE TABLE atoms (id TEXT PRIMARY KEY, type TEXT, topic_key TEXT) STRICT;"
    )
    conn.executescript(_V1_EDGES_NARROW)
    conn.execute(
        "INSERT INTO atoms (id, type, topic_key) VALUES "
        "('a','glossary','k'), ('b','convention','m')"
    )
    conn.execute(
        "INSERT INTO edges (src_id, dst_id, kind, valid_from, source) "
        "VALUES ('a','b','MIRRORS',0,'manual')"
    )
    conn.commit()
    assert "ROLLS_UP" not in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()[0]

    store._migrate(conn)

    # Widened CHECK, existing row preserved, and a ROLLS_UP insert now succeeds.
    assert "ROLLS_UP" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    conn.execute(
        "INSERT INTO edges (src_id, dst_id, kind, valid_from, source) "
        "VALUES ('b','a','ROLLS_UP',0,'manual')"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='ROLLS_UP'"
    ).fetchone()[0] == 1
    conn.close()


# A v1 `atoms` table: every current column EXCEPT `tier` (added in v2, §14.1).
# Mirrors a real pre-§14.1 DB so init_schema's tier-dependent index/view path
# is exercised faithfully.
_V1_ATOMS = """
CREATE TABLE atoms (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, topic_key TEXT NOT NULL,
  decision_status TEXT, source_kind TEXT NOT NULL, source_ref TEXT,
  source_lines TEXT, valid_from INTEGER NOT NULL, valid_to INTEGER,
  asserted_at INTEGER NOT NULL, superseded_by TEXT,
  liveness_kind TEXT, liveness_target TEXT, liveness_pattern TEXT,
  liveness_last_ok INTEGER, confidence REAL NOT NULL DEFAULT 1.0,
  repo_id TEXT, workspace_id TEXT NOT NULL DEFAULT 'default',
  valid_in_refs TEXT NOT NULL DEFAULT '["main"]',
  pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;
"""


def test_init_schema_upgrades_v1_db_with_tier_dependent_objects():
    """Regression: `dlms init` against a pre-§14.1 DB must not crash with
    'no such column: tier'. schema.sql creates idx_atoms_tier / live_atoms which
    reference atoms.tier, so the column has to be ALTERed in before the script
    runs (migration order)."""
    conn = store.connect(":memory:")
    conn.executescript(_V1_ATOMS)
    conn.execute(
        """INSERT INTO atoms (id, type, topic_key, source_kind, valid_from, asserted_at)
           VALUES ('a', 'invariant', 'k', 'manual', 0, 0)"""
    )
    conn.commit()
    assert "tier" not in {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}

    version = store.init_schema(conn)  # would raise OperationalError before the fix

    assert version == 2
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    assert "tier" in cols
    assert conn.execute("SELECT tier FROM atoms WHERE id = 'a'").fetchone()["tier"] == "symbol"
    # tier-dependent objects from schema.sql now exist and the row is queryable.
    assert conn.execute("SELECT COUNT(*) AS c FROM live_atoms").fetchone()["c"] == 1
    conn.close()
