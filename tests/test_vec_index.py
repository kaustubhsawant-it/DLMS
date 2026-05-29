"""sqlite-vec ANN backend tests (SPEC §14.3).

Skipped unless the optional `sqlite-vec` extra is installed AND the local
sqlite build can load extensions. Run with: uv run --with sqlite-vec pytest
"""

from __future__ import annotations

import sqlite3

import pytest

from dlms_cli import atoms, embeddings

sqlite_vec = pytest.importorskip("sqlite_vec")


def _loadable() -> bool:
    c = sqlite3.connect(":memory:")
    try:
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        return True
    except Exception:
        return False
    finally:
        c.close()


pytestmark = pytest.mark.skipif(
    not _loadable(), reason="sqlite-vec extension cannot load in this environment"
)


def _atom(conn, key, text):
    return atoms.assert_fact(
        conn, type="invariant", topic_key=key, summary_50w=text, source_kind="manual"
    )


def test_vec_index_active_after_embed_and_finds_match(conn):
    a = _atom(conn, "auth.token", "API rotates the session token and the web client re-reads it")
    _atom(conn, "unrelated", "completely different widgets and gizmos over here")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())

    assert embeddings._use_vec(conn) is True  # synced by embed_pending
    qv = embeddings.HashEmbedder().embed("API rotates the session token")
    res = embeddings.top_k(conn, qv, k=2)
    assert res and res[0][0] == a.id


def test_vec_and_linear_agree_on_top_result(conn):
    for i in range(6):
        _atom(conn, f"t{i}", f"topic number {i} alpha beta gamma delta {i}")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    qv = embeddings.HashEmbedder().embed("topic number 3 alpha beta gamma delta 3")

    vec_res = embeddings.top_k(conn, qv, k=3)        # ANN path (_use_vec True)
    lin_res = embeddings._top_k_linear(conn, qv, 3)  # forced linear
    assert vec_res[0][0] == lin_res[0][0]


def test_sync_is_idempotent_after_embed(conn):
    _atom(conn, "x", "hello world session token")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    # embed_pending already synced — a follow-up sync adds nothing.
    assert embeddings.sync_vec_index(conn) == 0


def test_superseded_atom_excluded_from_vec_results(conn):
    a = _atom(conn, "topic", "first claim about the session token rotation flow")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    # Supersede with a new claim on the same topic; old atom leaves live set.
    _atom(conn, "topic", "second very different claim about caching entirely")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    qv = embeddings.HashEmbedder().embed("first claim about the session token rotation flow")
    ids = [r[0] for r in embeddings.top_k(conn, qv, k=5)]
    assert a.id not in ids  # superseded atom filtered out of KNN results


def test_unsynced_index_falls_back_to_linear(conn, monkeypatch):
    # Embed while sqlite-vec is "unavailable" → JSON sidecar only, no vec0 table.
    monkeypatch.setattr(embeddings, "_VEC_LIB", False)
    a = _atom(conn, "k", "session token rotation flow alpha")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    # Now the extension "appears" but the index was never synced.
    monkeypatch.setattr(embeddings, "_VEC_LIB", sqlite_vec)
    assert embeddings._use_vec(conn) is False  # not synced → no partial KNN
    qv = embeddings.HashEmbedder().embed("session token rotation flow alpha")
    assert any(r[0] == a.id for r in embeddings.top_k(conn, qv, k=3))  # linear served it


def test_sync_backfills_json_only_vectors(conn, monkeypatch):
    monkeypatch.setattr(embeddings, "_VEC_LIB", False)
    a = _atom(conn, "k", "session token rotation flow beta")
    _atom(conn, "k2", "another unrelated subject gamma delta")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())  # JSON only
    monkeypatch.setattr(embeddings, "_VEC_LIB", sqlite_vec)
    assert embeddings.sync_vec_index(conn) == 2  # both backfilled
    assert embeddings._use_vec(conn) is True
    qv = embeddings.HashEmbedder().embed("session token rotation flow beta")
    assert embeddings.top_k(conn, qv, k=1)[0][0] == a.id


def test_dim_mismatch_query_falls_back_without_crashing(conn):
    _atom(conn, "k", "hello world session token")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    assert embeddings._use_vec(conn) is True
    # wrong-length query → vec0 MATCH raises sqlite3.Error → caught → linear
    res = embeddings.top_k(conn, [0.0] * (embeddings.DEFAULT_DIM + 1), k=3)
    assert isinstance(res, list)  # no crash


def test_vec_escalates_past_dead_vectors(conn):
    # 5 near-query atoms embedded while live, then superseded by far-text
    # versions. The 5 dead-near vectors crowd the k*4 margin (k=1 → over=4),
    # so the first KNN returns all-dead; escalation must still surface a live one.
    q = "alpha beta gamma delta epsilon zeta eta"
    for i in range(5):
        atoms.assert_fact(conn, type="invariant", topic_key=f"t{i}",
                          summary_50w=q, source_kind="manual")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())  # embed near (live)
    for i in range(5):
        atoms.assert_fact(conn, type="invariant", topic_key=f"t{i}",
                          summary_50w=f"qqq www eee rrr ttt {i}", source_kind="manual")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())  # embed far (live)
    qv = embeddings.HashEmbedder().embed(q)
    res = embeddings.top_k(conn, qv, k=1)
    assert len(res) == 1  # escalation found a live atom past the dead near ones
