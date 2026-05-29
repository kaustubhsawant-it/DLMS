-- DLMS atom store schema
-- SQLite 3.38+ (for STRICT tables and JSONB)
-- Vector extension: sqlite-vss (loaded at runtime by dlms CLI)
--
-- Versioned via schema_version table. Migrations live in dlms/migrations/.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
) STRICT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (1, unixepoch());
-- v2 (2026-05-27): hierarchical atoms (SPEC §14.1) — atoms.tier + ROLLS_UP edge.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (2, unixepoch());

-- ---------------------------------------------------------------------------
-- 1. ATOMS — the unit of knowledge
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS atoms (
  id              TEXT PRIMARY KEY,            -- content-hash stable ID
  type            TEXT NOT NULL CHECK(type IN (
                    'invariant','schema_fact','decision','convention',
                    'dependency','owner','runtime','build_recipe','glossary'
                  )),
  topic_key       TEXT NOT NULL,               -- groups versions of "the same fact"
  decision_status TEXT CHECK(decision_status IN ('OPEN','CLOSED','DEFERRED')),

  -- Hierarchy tier (SPEC §14.1) — coarse→fine. Module atoms summarize a
  -- subsystem and hold ROLLS_UP edges to their file/symbol children.
  tier            TEXT NOT NULL DEFAULT 'symbol'
                    CHECK(tier IN ('module','file','symbol')),

  -- Provenance
  source_kind     TEXT NOT NULL CHECK(source_kind IN (
                    'commit','transcript','schema_snapshot','sentry_event',
                    'screenshot','readme','manifest','llm_extract','manual'
                  )),
  source_ref      TEXT,                        -- SHA | session_id | hash | path
  source_lines    TEXT,                        -- "10-25" or NULL

  -- Bi-temporal validity
  valid_from      INTEGER NOT NULL,            -- when fact became true (epoch)
  valid_to        INTEGER,                     -- when superseded (NULL = live)
  asserted_at     INTEGER NOT NULL,            -- when stored
  superseded_by   TEXT REFERENCES atoms(id),

  -- Liveness predicate (runnable check for staleness)
  liveness_kind   TEXT CHECK(liveness_kind IN ('regex','ast','sql','none')),
  liveness_target TEXT,                        -- file path / query
  liveness_pattern TEXT,                       -- regex / AST path / SQL
  liveness_last_ok INTEGER,                    -- last time it passed

  -- Confidence + workspace scoping
  confidence      REAL NOT NULL DEFAULT 1.0,
  repo_id         TEXT,                        -- NULL = workspace-global
  workspace_id    TEXT NOT NULL DEFAULT 'default',
  valid_in_refs   TEXT NOT NULL DEFAULT '["main"]',  -- JSON array of refs

  -- Pin / archive
  pinned          INTEGER NOT NULL DEFAULT 0,
  archived        INTEGER NOT NULL DEFAULT 0,

  created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE INDEX IF NOT EXISTS idx_atoms_topic ON atoms(topic_key);
CREATE INDEX IF NOT EXISTS idx_atoms_live  ON atoms(valid_to) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_atoms_type  ON atoms(type);
CREATE INDEX IF NOT EXISTS idx_atoms_repo  ON atoms(repo_id);
CREATE INDEX IF NOT EXISTS idx_atoms_tier  ON atoms(tier) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 2. ATOM SUMMARIES — multi-resolution (10w / 50w / 250w)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS atom_summaries (
  atom_id     TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  resolution  INTEGER NOT NULL CHECK(resolution IN (10, 50, 250)),
  text        TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  PRIMARY KEY (atom_id, resolution)
) STRICT;

-- ---------------------------------------------------------------------------
-- 3. EMBEDDINGS (sqlite-vss virtual table populated separately)
-- ---------------------------------------------------------------------------

-- Embeddings live in a sibling virtual table created by sqlite-vss at runtime:
--   CREATE VIRTUAL TABLE atom_vss USING vss0(embedding(384));
-- Mapping table keeps atom_id ↔ rowid stable.
CREATE TABLE IF NOT EXISTS atom_embeddings (
  atom_id     TEXT PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  vss_rowid   INTEGER UNIQUE NOT NULL,
  model       TEXT NOT NULL,             -- 'bge-small-en-v1.5'
  resolution  INTEGER NOT NULL,          -- which summary was embedded
  hash        TEXT NOT NULL,             -- of summary text — skip re-embed if same
  embedded_at INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------------------
-- 4. EDGES — typed graph
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edges (
  id          INTEGER PRIMARY KEY,
  src_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  dst_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK(kind IN (
                'MIRRORS','IMPLEMENTS','SUPERSEDES','CONTRADICTS',
                'LOCATED_IN','REFERENCES','DEPENDS_ON','OWNS','BLOCKS',
                'CO_CHANGED','DERIVED_FROM','ROLLS_UP'
              )),
  directed    INTEGER NOT NULL DEFAULT 0,  -- 0 = symmetric (store once)
  weight      REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
  confidence  REAL NOT NULL DEFAULT 1.0,

  -- Bi-temporal
  valid_from  INTEGER NOT NULL,
  valid_to    INTEGER,

  -- Provenance
  source      TEXT NOT NULL CHECK(source IN (
                'shared_ref','embed_sim','commit_couple','llm_extract',
                'git_blame','manual','schema_diff'
              )),
  status      TEXT NOT NULL DEFAULT 'live'
                CHECK(status IN ('live','suggested','rejected')),

  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE(src_id, dst_id, kind, valid_from)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind, weight DESC) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 5. EDGE EVIDENCE — why an edge exists
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edge_evidence (
  edge_id    INTEGER NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,              -- 'commit_sha'|'file_path'|'symbol'|'llm_quote'
  payload    TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edge_evidence_edge ON edge_evidence(edge_id);

-- ---------------------------------------------------------------------------
-- 6. INGESTION JOB QUEUE
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS jobs (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,             -- 'commit'|'schema_diff'|'transcript'|'manual'
  payload      TEXT NOT NULL,             -- JSON blob (sha, paths, etc.)
  status       TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','running','done','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  enqueued_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  started_at   INTEGER,
  finished_at  INTEGER
) STRICT;

CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(enqueued_at) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- 7. SESSION / REPO STATE
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_state (
  repo_id          TEXT PRIMARY KEY,
  workspace_id     TEXT NOT NULL DEFAULT 'default',
  root_path        TEXT NOT NULL,
  last_indexed_sha TEXT,
  last_indexed_at  INTEGER,
  last_branch      TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS session_state (
  session_id      TEXT PRIMARY KEY,
  started_at      INTEGER NOT NULL,
  ended_at        INTEGER,
  ended_reason    TEXT,                   -- 'capacity'|'user'|'tick_complete'|'halt_safety'
  context_pct_end REAL,
  handoff_path    TEXT,
  branch          TEXT,
  last_commit_sha TEXT
) STRICT;

-- ---------------------------------------------------------------------------
-- 8. EMBEDDING LEDGER — cost discipline
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS embedding_ledger (
  day          TEXT PRIMARY KEY,          -- 'YYYY-MM-DD'
  tokens_used  INTEGER NOT NULL DEFAULT 0,
  call_count   INTEGER NOT NULL DEFAULT 0,
  budget_cap   INTEGER NOT NULL DEFAULT 50000
) STRICT;

-- ---------------------------------------------------------------------------
-- 9. RETRIEVAL LOG — for routing classifier improvement + audit
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS retrieval_log (
  id              INTEGER PRIMARY KEY,
  ts              INTEGER NOT NULL DEFAULT (unixepoch()),
  session_id      TEXT,
  query           TEXT NOT NULL,
  classified_as   TEXT,                   -- 'navigational'|'semantic'|'contradictory'|'implementation'
  atoms_returned  TEXT,                   -- JSON array of atom IDs
  injected_tokens INTEGER,
  user_feedback   TEXT                    -- 'good'|'bad'|'unused' (post-hoc)
) STRICT;

-- ---------------------------------------------------------------------------
-- 10. VIEWS — convenience for common queries
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS live_atoms AS
  SELECT * FROM atoms
   WHERE valid_to IS NULL AND archived = 0;

CREATE VIEW IF NOT EXISTS live_edges AS
  SELECT * FROM edges
   WHERE valid_to IS NULL AND status = 'live';

CREATE VIEW IF NOT EXISTS invariants_live AS
  SELECT * FROM live_atoms WHERE type = 'invariant';

CREATE VIEW IF NOT EXISTS closed_decisions AS
  SELECT * FROM live_atoms WHERE type = 'decision' AND decision_status = 'CLOSED';
