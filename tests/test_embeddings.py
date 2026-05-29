from __future__ import annotations

from dlms_cli import atoms as atoms_mod
from dlms_cli import embeddings


def _seed_atom(conn, text="API rotates the session token; the web client re-reads it via cache."):
    return atoms_mod.assert_fact(
        conn,
        type="invariant",
        topic_key="auth.token.rotation",
        summary_50w=text,
        source_kind="manual",
    )


def test_hash_embedder_deterministic():
    e = embeddings.HashEmbedder()
    v1 = e.embed("hello world")
    v2 = e.embed("hello world")
    assert v1 == v2
    assert len(v1) == embeddings.DEFAULT_DIM


def test_hash_embedder_normalized():
    v = embeddings.HashEmbedder().embed("rng matters")
    mag = sum(x * x for x in v) ** 0.5
    assert abs(mag - 1.0) < 1e-9


def test_embed_pending_writes_rows(conn):
    a = _seed_atom(conn)
    n = embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    assert n == 1
    row = conn.execute(
        "SELECT * FROM atom_embeddings WHERE atom_id = ?", (a.id,)
    ).fetchone()
    assert row["model"] == "hash-3gram-v1"
    assert row["resolution"] == 50


def test_embed_pending_skips_unchanged(conn):
    _seed_atom(conn)
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    n2 = embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    assert n2 == 0


def test_top_k_finds_seed_atom(conn):
    a = _seed_atom(conn)
    e = embeddings.HashEmbedder()
    embeddings.embed_pending(conn, embedder=e)
    qv = e.embed("auth session token rotation")
    results = embeddings.top_k(conn, qv, k=5)
    ids = [r[0] for r in results]
    assert a.id in ids


def test_top_k_empty_when_no_embeddings(conn):
    assert embeddings.top_k(conn, [0.0] * embeddings.DEFAULT_DIM) == []


def test_top_k_falls_back_to_linear_without_vec(conn, monkeypatch):
    # Force the sqlite-vec backend unavailable; top_k must still work (§14.3).
    monkeypatch.setattr(embeddings, "_VEC_LIB", False)
    a = _seed_atom(conn)
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    assert embeddings._use_vec(conn) is False
    qv = embeddings.HashEmbedder().embed("auth session token rotation")
    res = embeddings.top_k(conn, qv, k=5)
    assert any(r[0] == a.id for r in res)
