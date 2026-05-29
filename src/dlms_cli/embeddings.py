"""Pluggable embedding layer.

SPEC §7 calls for `bge-small-en-v1.5` via sentence-transformers, but pulling
that into the core install adds ~600MB of weights. We keep the core stdlib
pure and ship two backends:

* ``HashEmbedder`` (default) — deterministic hash-based pseudo-embedding.
  Useful for tests, offline use, and the smoke path. Hashes 3-grams into
  a fixed-dim vector. Not semantically meaningful, but exact-match queries
  work and the contract is identical.
* ``SentenceTransformerEmbedder`` — loads ``bge-small-en-v1.5`` when the
  `dlms[embed]` extra is installed. Selected automatically by
  :func:`default_embedder` when the import succeeds.

Embeddings are stored in the ``atom_embeddings`` table (SPEC schema §3) plus a
JSON sidecar (``atom_embedding_vectors``). When the optional ``dlms[vec]`` extra
is installed, vectors are ALSO mirrored into a ``sqlite-vec`` ``vec0`` virtual
table and :func:`top_k` runs an ANN KNN query instead of a linear scan
(SPEC §14.3). The JSON sidecar remains the source of truth and the fallback,
so retrieval is correct with or without the extension.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol

DEFAULT_DIM = 384  # matches bge-small-en-v1.5 so the swap is drop-in


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]:
        """Return an **L2-normalized** vector of length `dim`.

        Normalization is a hard contract: the sqlite-vec backend converts L2
        distance to cosine via ``1 - d²/2``, which is only correct for unit
        vectors. A non-normalizing embedder would silently mis-rank.
        """
        ...


@dataclass
class HashEmbedder:
    """Deterministic 3-gram hash → fixed-dim vector. Cosine-comparable.

    Not semantically meaningful — co-occurring trigrams cluster, but
    paraphrases will not. Sufficient as a placeholder embedder so the
    retrieval contract works end-to-end without weights on disk.
    """

    dim: int = DEFAULT_DIM
    name: str = "hash-3gram-v1"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text:
            return vec
        norm = text.lower()
        for i in range(len(norm) - 2):
            tri = norm[i : i + 3]
            h = int.from_bytes(
                hashlib.blake2s(tri.encode(), digest_size=8).digest(), "big"
            )
            idx = h % self.dim
            sign = 1.0 if (h >> 32) & 1 else -1.0
            vec[idx] += sign
        # L2 normalize so cosine == dot product.
        mag = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / mag for v in vec]


def default_embedder() -> Embedder:
    """Pick the best available embedder. Falls back to hash."""
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except ImportError:
        return HashEmbedder()
    return SentenceTransformerEmbedder()


@dataclass
class SentenceTransformerEmbedder:
    """Loads bge-small-en-v1.5 lazily. Behind the ``embed`` extra."""

    name: str = "bge-small-en-v1.5"
    dim: int = DEFAULT_DIM
    _model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._load()
        vec = model.encode(text, normalize_embeddings=True)  # type: ignore[attr-defined]
        return list(map(float, vec))


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Vector index backend — sqlite-vec ANN with graceful linear fallback (§14.3)
# ---------------------------------------------------------------------------

_VEC_TABLE = "atom_vec"
_VEC_LIB: object | None = None  # None=unprobed, False=unavailable, else the module


def _vec_lib() -> object | None:
    """The sqlite_vec module if installed, else False. Probed once."""
    global _VEC_LIB
    if _VEC_LIB is None:
        try:
            import sqlite_vec  # type: ignore

            _VEC_LIB = sqlite_vec
        except ImportError:
            _VEC_LIB = False
    return _VEC_LIB


def _ensure_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load the sqlite-vec extension on `conn`. Never raises.

    Returns False (→ linear fallback) when `dlms[vec]` isn't installed or the
    sqlite build forbids extension loading. Probes via ``vec_version()`` so a
    connection that already loaded the extension is detected cheaply.
    """
    lib = _vec_lib()
    if lib is False:
        return False
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True  # already loaded on this connection
    except sqlite3.OperationalError:
        pass
    try:
        conn.enable_load_extension(True)
        lib.load(conn)  # type: ignore[attr-defined]
        conn.enable_load_extension(False)
        return True
    except Exception:  # noqa: BLE001 — any failure → linear fallback
        return False


def _ensure_vec_table(conn: sqlite3.Connection, dim: int = DEFAULT_DIM) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0(embedding float[{dim}])"
    )


def _vec_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_VEC_TABLE,)
    ).fetchone() is not None


def sync_vec_index(conn: sqlite3.Connection) -> int:
    """Backfill the vec0 index from the JSON sidecar (vectors embedded before
    the extension was available). Returns rows added. No-op without sqlite-vec.
    """
    if not _ensure_vec(conn):
        return 0
    _ensure_vec_table(conn)
    rows = conn.execute(
        f"""SELECT e.vss_rowid AS rid, v.vector AS vec
              FROM atom_embedding_vectors v
              JOIN atom_embeddings e ON e.atom_id = v.atom_id
             WHERE e.vss_rowid NOT IN (SELECT rowid FROM {_VEC_TABLE})"""
    ).fetchall()
    with conn:
        for r in rows:
            conn.execute(
                f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
                (r["rid"], r["vec"]),
            )
    return len(rows)


def _use_vec(conn: sqlite3.Connection) -> bool:
    """True only when the vec index is loaded, present, and FULLY synced with
    the JSON sidecar — otherwise linear scan stays correct (no partial KNN)."""
    if not _ensure_vec(conn) or not _vec_table_exists(conn):
        return False
    jcount = conn.execute("SELECT COUNT(*) AS c FROM atom_embedding_vectors").fetchone()["c"]
    vcount = conn.execute(f"SELECT COUNT(*) AS c FROM {_VEC_TABLE}").fetchone()["c"]
    return jcount > 0 and jcount == vcount


def _top_k_vec(
    conn: sqlite3.Connection, query_vec: list[float], k: int
) -> list[tuple[str, float]]:
    """ANN KNN via sqlite-vec. Over-fetches then filters to live atoms.

    vec0 distance is L2; vectors are L2-normalized, so cosine = 1 - d²/2.
    Superseded atoms keep their vectors in the index, so an over-fetch of k·4
    can come back all-dead and yield <k live results. When that happens we
    escalate to an exhaustive scan so live recall is never silently truncated.
    """
    qjson = json.dumps(query_vec)

    def _knn(limit: int) -> list[tuple[str, float]]:
        rows = conn.execute(
            f"""SELECT rowid AS rid, distance AS dist
                  FROM {_VEC_TABLE}
                 WHERE embedding MATCH ? ORDER BY distance LIMIT {int(limit)}""",
            (qjson,),
        ).fetchall()
        if not rows:
            return []
        rid_to_dist = {r["rid"]: r["dist"] for r in rows}
        ph = ",".join("?" for _ in rid_to_dist)
        live = conn.execute(
            f"""SELECT e.vss_rowid AS rid, e.atom_id AS aid
                  FROM atom_embeddings e
                  JOIN live_atoms a ON a.id = e.atom_id
                 WHERE e.vss_rowid IN ({ph})""",
            list(rid_to_dist),
        ).fetchall()
        scored = [(r["aid"], 1.0 - (rid_to_dist[r["rid"]] ** 2) / 2.0) for r in live]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    over = max(k * 4, k)
    res = _knn(over)
    if len(res) < k:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM {_VEC_TABLE}").fetchone()["c"]
        if total > over:
            res = _knn(total)  # dead vectors crowded the margin — scan exhaustively
    return res


def upsert_embedding(
    conn: sqlite3.Connection,
    *,
    atom_id: str,
    vector: list[float],
    model: str,
    resolution: int,
    text_hash: str,
) -> None:
    """Insert or refresh the embedding for an atom.

    The vector itself is stored as a JSON blob in a sidecar table — when
    the sqlite-vss virtual table lands, we'll mirror these rows into it.
    ``vss_rowid`` is assigned as an incrementing integer for compatibility
    with the schema (real wiring TBD with the vector backend).
    """
    with conn:
        existing = conn.execute(
            "SELECT vss_rowid FROM atom_embeddings WHERE atom_id = ?", (atom_id,)
        ).fetchone()
        if existing:
            rowid = existing["vss_rowid"]
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(vss_rowid), 0) + 1 AS nx FROM atom_embeddings"
            ).fetchone()
            rowid = int(row["nx"])
        now = int(time.time())
        conn.execute(
            """INSERT INTO atom_embeddings (
                  atom_id, vss_rowid, model, resolution, hash, embedded_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(atom_id) DO UPDATE SET
                  vss_rowid   = excluded.vss_rowid,
                  model       = excluded.model,
                  resolution  = excluded.resolution,
                  hash        = excluded.hash,
                  embedded_at = excluded.embedded_at""",
            (atom_id, rowid, model, resolution, text_hash, now),
        )
        # Sidecar JSON store — created on demand.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS atom_embedding_vectors (
                  atom_id TEXT PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
                  vector  TEXT NOT NULL
               )"""
        )
        vector_json = json.dumps(vector)
        conn.execute(
            """INSERT INTO atom_embedding_vectors (atom_id, vector)
               VALUES (?, ?)
               ON CONFLICT(atom_id) DO UPDATE SET vector = excluded.vector""",
            (atom_id, vector_json),
        )
        # Mirror into the ANN index when available (§14.3); JSON stays the
        # source of truth, so this is purely additive.
        if _ensure_vec(conn):
            _ensure_vec_table(conn)
            conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (rowid,))
            conn.execute(
                f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
                (rowid, vector_json),
            )


def embed_pending(
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    *,
    resolution: int = 50,
    limit: int = 500,
) -> int:
    """Embed atoms whose summary has changed (or never been embedded).

    Returns the number of atoms (re-)embedded.
    """
    embedder = embedder or default_embedder()
    # 1. Atoms with no embedding at all (fresh after ingest).
    rows = conn.execute(
        """SELECT a.id AS atom_id, s.text AS text
              FROM live_atoms a
              JOIN atom_summaries s ON s.atom_id = a.id AND s.resolution = ?
              LEFT JOIN atom_embeddings e ON e.atom_id = a.id
             WHERE e.atom_id IS NULL
             LIMIT ?""",
        (resolution, limit),
    ).fetchall()
    n = 0
    for row in rows:
        text = row["text"]
        text_hash = _hash_text(text)
        vec = embedder.embed(text)
        upsert_embedding(
            conn,
            atom_id=row["atom_id"],
            vector=vec,
            model=embedder.name,
            resolution=resolution,
            text_hash=text_hash,
        )
        n += 1

    # 2. Atoms whose summary text changed (hash differs from stored).
    #    Uses the actual text hash to compare in Python — no placeholder.
    if n < limit:
        reembed = conn.execute(
            """SELECT a.id AS atom_id, s.text AS text
                  FROM live_atoms a
                  JOIN atom_summaries s ON s.atom_id = a.id AND s.resolution = ?
                  JOIN atom_embeddings e ON e.atom_id = a.id
                 LIMIT ?""",
            (resolution, limit - n),
        ).fetchall()
        for row in reembed:
            text = row["text"]
            text_hash = _hash_text(text)
            existing = conn.execute(
                "SELECT hash FROM atom_embeddings WHERE atom_id = ?",
                (row["atom_id"],),
            ).fetchone()
            if existing and existing["hash"] == text_hash:
                continue
            vec = embedder.embed(text)
            upsert_embedding(
                conn,
                atom_id=row["atom_id"],
                vector=vec,
                model=embedder.name,
                resolution=resolution,
                text_hash=text_hash,
            )
            n += 1

    # Keep the ANN index in sync (backfills any JSON-only vectors, e.g. those
    # embedded before sqlite-vec was installed). No-op without the extension.
    sync_vec_index(conn)
    return n


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dim mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def top_k(
    conn: sqlite3.Connection,
    query_vec: list[float],
    *,
    k: int = 10,
) -> list[tuple[str, float]]:
    """Top-K most similar atoms. Uses the sqlite-vec ANN index when it is
    loaded and fully synced (§14.3); otherwise a brute-force cosine scan.
    """
    if _use_vec(conn):
        try:
            return _top_k_vec(conn, query_vec, k)
        except sqlite3.Error:
            pass  # any vec error (e.g. dim mismatch) → linear fallback
    return _top_k_linear(conn, query_vec, k)


def _top_k_linear(
    conn: sqlite3.Connection,
    query_vec: list[float],
    k: int,
) -> list[tuple[str, float]]:
    """Brute-force cosine top-K against atom_embedding_vectors (≤10k atoms)."""
    # Table may not exist if no embeddings written yet.
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='atom_embedding_vectors'"
    ).fetchone()
    if not has_table:
        return []
    rows = conn.execute(
        """SELECT v.atom_id, v.vector
             FROM atom_embedding_vectors v
             JOIN live_atoms a ON a.id = v.atom_id"""
    ).fetchall()
    scored: list[tuple[str, float]] = []
    for r in rows:
        try:
            other = json.loads(r["vector"])
        except (TypeError, json.JSONDecodeError):
            continue
        if len(other) != len(query_vec):
            continue
        scored.append((r["atom_id"], cosine(query_vec, other)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


__all__ = [
    "DEFAULT_DIM",
    "Embedder",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "cosine",
    "default_embedder",
    "embed_pending",
    "sync_vec_index",
    "top_k",
    "upsert_embedding",
]
