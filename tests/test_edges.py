import pytest

from dlms_cli import atoms, edges


def _atom(conn, topic, claim="claim"):
    return atoms.assert_fact(
        conn,
        type="invariant",
        topic_key=topic,
        summary_50w=claim,
        source_kind="manual",
    )


def test_upsert_edge_creates(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    assert e.kind == "REFERENCES"
    assert e.directed is True


def test_upsert_edge_idempotent_refresh(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e1 = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                           source="manual", weight=0.6)
    e2 = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                           source="manual", weight=0.9)
    assert e1.id == e2.id  # same edge row refreshed
    assert e2.weight == 0.9  # max-merge


def test_upsert_edge_weight_only_increases(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                      source="manual", weight=0.9)
    e = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                          source="manual", weight=0.1)
    assert e.weight == 0.9


def test_symmetric_edge_canonicalized(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e1 = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="CO_CHANGED",
                           source="commit_couple", weight=0.5)
    e2 = edges.upsert_edge(conn, src_id=b.id, dst_id=a.id, kind="CO_CHANGED",
                           source="commit_couple", weight=0.7)
    assert e1.id == e2.id  # same row regardless of insert order
    assert e2.directed is False
    n = conn.execute("SELECT COUNT(*) AS c FROM edges WHERE kind='CO_CHANGED'").fetchone()
    assert n["c"] == 1


def test_self_edge_rejected(conn):
    a = _atom(conn, "a")
    with pytest.raises(ValueError, match="self-edge"):
        edges.upsert_edge(conn, src_id=a.id, dst_id=a.id, kind="REFERENCES", source="manual")


def test_endpoints_must_exist(conn):
    a = _atom(conn, "a")
    with pytest.raises(KeyError):
        edges.upsert_edge(conn, src_id=a.id, dst_id="ghost", kind="REFERENCES", source="manual")


def test_invalid_weight(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    with pytest.raises(ValueError):
        edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                          source="manual", weight=1.5)


def test_evidence_appended(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e = edges.upsert_edge(
        conn, src_id=a.id, dst_id=b.id, kind="MIRRORS", source="shared_ref",
        evidence=[("file_path", "src/foo.py"), ("symbol", "token_ttl")],
    )
    edges.add_evidence(conn, e.id, kind="commit_sha", payload="abc123")
    rows = list(conn.execute(
        "SELECT kind, payload FROM edge_evidence WHERE edge_id = ? ORDER BY rowid",
        (e.id,),
    ))
    assert [r["kind"] for r in rows] == ["file_path", "symbol", "commit_sha"]


def test_expire_edge_sets_valid_to(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    edges.expire_edge(conn, e.id)
    row = conn.execute("SELECT valid_to FROM edges WHERE id = ?", (e.id,)).fetchone()
    assert row["valid_to"] is not None
    live = edges.list_edges(conn, atom_id=a.id)
    assert all(le.id != e.id for le in live)


def test_reject_edge(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    e = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                          source="embed_sim", status="suggested")
    edges.reject_edge(conn, e.id)
    row = conn.execute("SELECT status FROM edges WHERE id = ?", (e.id,)).fetchone()
    assert row["status"] == "rejected"
    assert edges.list_edges(conn, atom_id=a.id, status="live") == []


def test_list_edges_filters_by_kind(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b"); c = _atom(conn, "c")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    edges.upsert_edge(conn, src_id=a.id, dst_id=c.id, kind="MIRRORS", source="shared_ref")
    refs = edges.list_edges(conn, atom_id=a.id, kind="REFERENCES")
    assert len(refs) == 1
    assert refs[0].kind == "REFERENCES"


def test_graph_neighbors_one_hop_directed(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b"); c = _atom(conn, "c")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                      source="manual", weight=0.8)
    edges.upsert_edge(conn, src_id=b.id, dst_id=c.id, kind="REFERENCES",
                      source="manual", weight=0.5)
    hops1 = edges.graph_neighbors(conn, a.id, hops=1)
    assert [h.atom_id for h in hops1] == [b.id]
    hops2 = edges.graph_neighbors(conn, a.id, hops=2)
    assert {h.atom_id for h in hops2} == {b.id, c.id}
    by_id = {h.atom_id: h for h in hops2}
    assert by_id[c.id].depth == 2
    assert by_id[c.id].weight_acc == pytest.approx(0.8 * 0.5)


def test_graph_neighbors_directed_does_not_traverse_reverse(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    # Seed from b — REFERENCES is directional, so we should NOT reach a
    hops = edges.graph_neighbors(conn, b.id, hops=2)
    assert [h.atom_id for h in hops] == []


def test_graph_neighbors_symmetric_traverses_both_ways(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="CO_CHANGED",
                      source="commit_couple")
    hops_from_a = edges.graph_neighbors(conn, a.id, hops=1)
    hops_from_b = edges.graph_neighbors(conn, b.id, hops=1)
    assert [h.atom_id for h in hops_from_a] == [b.id]
    assert [h.atom_id for h in hops_from_b] == [a.id]


def test_graph_neighbors_kind_filter(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b"); c = _atom(conn, "c")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    edges.upsert_edge(conn, src_id=a.id, dst_id=c.id, kind="MIRRORS", source="shared_ref")
    only_mirrors = edges.graph_neighbors(conn, a.id, hops=1, kinds=["MIRRORS"])
    assert [h.atom_id for h in only_mirrors] == [c.id]


def test_graph_neighbors_skips_expired_and_rejected(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b"); c = _atom(conn, "c")
    e1 = edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES", source="manual")
    e2 = edges.upsert_edge(conn, src_id=a.id, dst_id=c.id, kind="REFERENCES",
                           source="embed_sim", status="suggested")
    edges.expire_edge(conn, e1.id)
    edges.reject_edge(conn, e2.id)
    assert edges.graph_neighbors(conn, a.id, hops=1) == []


def test_graph_neighbors_min_weight(conn):
    a = _atom(conn, "a"); b = _atom(conn, "b"); c = _atom(conn, "c")
    edges.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="REFERENCES",
                      source="manual", weight=0.9)
    edges.upsert_edge(conn, src_id=a.id, dst_id=c.id, kind="REFERENCES",
                      source="manual", weight=0.2)
    hops = edges.graph_neighbors(conn, a.id, hops=1, min_weight=0.5)
    assert [h.atom_id for h in hops] == [b.id]


def test_graph_neighbors_unknown_seed_raises(conn):
    with pytest.raises(KeyError):
        edges.graph_neighbors(conn, "ghost", hops=1)


def test_graph_neighbors_invalid_hops(conn):
    a = _atom(conn, "a")
    with pytest.raises(ValueError):
        edges.graph_neighbors(conn, a.id, hops=0)
