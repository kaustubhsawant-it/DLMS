import pytest

from dlms_cli import atoms
from dlms_cli.atoms import Liveness


def _assert(conn, **overrides):
    """Minimal-kwargs helper for invariant atoms."""
    defaults = dict(
        type="invariant",
        topic_key="auth.token.rotation",
        summary_50w="API rotates the session token; the web client re-reads it via cache.",
        source_kind="manual",
    )
    defaults.update(overrides)
    return atoms.assert_fact(conn, **defaults)


def test_assert_fact_round_trip(conn):
    a = _assert(conn)
    assert a.id.startswith("invariant_")
    assert a.valid_to is None
    view = atoms.get_atom(conn, a.id)
    assert view is not None
    assert view.atom.id == a.id
    assert view.summaries[50].startswith("API rotates")


def test_idempotent_assert_returns_same_atom(conn):
    a = _assert(conn)
    b = _assert(conn)
    assert a.id == b.id
    rows = list(conn.execute("SELECT COUNT(*) AS c FROM atoms"))
    assert rows[0]["c"] == 1


def test_auto_supersede_on_changed_claim(conn):
    old = _assert(conn, summary_50w="API rotates the session token; the web client re-reads it.")
    new = _assert(conn, summary_50w="API rotates the session token AND refreshes the cache entry.")
    assert old.id != new.id

    old_row = conn.execute("SELECT * FROM atoms WHERE id = ?", (old.id,)).fetchone()
    assert old_row["valid_to"] is not None
    assert old_row["superseded_by"] == new.id

    new_row = conn.execute("SELECT * FROM atoms WHERE id = ?", (new.id,)).fetchone()
    assert new_row["valid_to"] is None
    assert new_row["superseded_by"] is None


def test_explicit_supersede(conn):
    a = _assert(conn, topic_key="t.a")
    b = _assert(conn, topic_key="t.b", summary_50w="claim b")
    atoms.supersede(conn, old_id=a.id, new_id=b.id)
    row = conn.execute("SELECT valid_to, superseded_by FROM atoms WHERE id = ?", (a.id,)).fetchone()
    assert row["valid_to"] is not None
    assert row["superseded_by"] == b.id


def test_supersede_unknown_atom_raises(conn):
    a = _assert(conn)
    with pytest.raises(KeyError):
        atoms.supersede(conn, old_id="ghost", new_id=a.id)
    with pytest.raises(KeyError):
        atoms.supersede(conn, old_id=a.id, new_id="ghost")


def test_archive_hides_from_get(conn):
    a = _assert(conn)
    atoms.archive(conn, a.id)
    assert atoms.get_atom(conn, a.id) is None
    assert atoms.get_atom(conn, a.id, include_archived=True) is not None
    atoms.archive(conn, a.id, on=False)
    assert atoms.get_atom(conn, a.id) is not None


def test_pin(conn):
    a = _assert(conn)
    atoms.pin(conn, a.id)
    view = atoms.get_atom(conn, a.id)
    assert view.atom.pinned is True
    atoms.pin(conn, a.id, on=False)
    view = atoms.get_atom(conn, a.id)
    assert view.atom.pinned is False


def test_query_atoms_filters(conn):
    _assert(conn, topic_key="t.a", summary_50w="A")
    _assert(conn, type="decision", topic_key="t.b", summary_50w="B",
            decision_status="CLOSED")
    _assert(conn, type="decision", topic_key="t.c", summary_50w="C",
            decision_status="OPEN")

    inv = atoms.query_atoms(conn, type="invariant")
    assert len(inv) == 1
    assert inv[0].type == "invariant"

    closed = atoms.query_atoms(conn, type="decision", decision_status="CLOSED")
    assert len(closed) == 1
    assert closed[0].decision_status == "CLOSED"


def test_query_atoms_live_only_excludes_superseded(conn):
    _assert(conn, summary_50w="v1")
    _assert(conn, summary_50w="v2")
    live = atoms.query_atoms(conn)
    assert len(live) == 1
    all_versions = atoms.query_atoms(conn, live_only=False)
    assert len(all_versions) == 2


def test_history_orders_oldest_first(conn):
    a = _assert(conn, summary_50w="v1")
    b = _assert(conn, summary_50w="v2")
    c = _assert(conn, summary_50w="v3")
    hist = atoms.history(conn, topic_key=a.topic_key, type_="invariant")
    assert [h.id for h in hist] == [a.id, b.id, c.id]


def test_decision_status_required_for_decision_atoms(conn):
    with pytest.raises(ValueError, match="decision_status"):
        _assert(conn, type="decision", summary_50w="x")


def test_decision_status_rejected_on_non_decision(conn):
    with pytest.raises(ValueError, match="only valid for type=decision"):
        _assert(conn, decision_status="CLOSED")


def test_confidence_bounds_validated(conn):
    with pytest.raises(ValueError):
        _assert(conn, confidence=1.5)
    with pytest.raises(ValueError):
        _assert(conn, confidence=-0.1)


def test_write_summary_upserts(conn):
    a = _assert(conn)
    atoms.write_summary(conn, a.id, 10, "api→web token sync")
    atoms.write_summary(conn, a.id, 250, "Long form: " + ("x " * 40))
    view = atoms.get_atom(conn, a.id)
    assert view.summaries[10] == "api→web token sync"
    assert view.summaries[250].startswith("Long form:")
    atoms.write_summary(conn, a.id, 10, "updated")
    view = atoms.get_atom(conn, a.id)
    assert view.summaries[10] == "updated"


def test_write_summary_invalid_resolution(conn):
    a = _assert(conn)
    with pytest.raises(ValueError):
        atoms.write_summary(conn, a.id, 42, "x")  # type: ignore[arg-type]


def test_query_atoms_rejects_unsafe_order_by(conn):
    with pytest.raises(ValueError):
        atoms.query_atoms(conn, order_by="DROP TABLE atoms; --")


def test_liveness_metadata_persisted(conn):
    liv = Liveness(kind="regex", target="README.md", pattern=r"DLMS")
    a = _assert(conn, liveness=liv)
    row = conn.execute("SELECT liveness_kind, liveness_target, liveness_pattern FROM atoms WHERE id = ?",
                       (a.id,)).fetchone()
    assert row["liveness_kind"] == "regex"
    assert row["liveness_target"] == "README.md"
    assert row["liveness_pattern"] == "DLMS"


def test_topics_in_returns_distinct_topics(conn):
    _assert(conn, topic_key="t.a")
    _assert(conn, topic_key="t.b", summary_50w="B")
    _assert(conn, topic_key="t.a", summary_50w="A2")  # supersedes first
    topics = list(atoms.topics_in(conn, type_="invariant"))
    assert topics == ["t.a", "t.b"]


# --- SPEC §14.5 conflict detection ------------------------------------------

def _live_contradicts(conn, a_id, b_id):
    """True if a live CONTRADICTS edge connects a_id and b_id (either order)."""
    return any(
        {r["src_id"], r["dst_id"]} == {a_id, b_id}
        for r in conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE kind='CONTRADICTS' AND valid_to IS NULL"
        )
    )


def test_conflict_raises_contradicts_edge_on_divergent_supersede(conn):
    old = _assert(conn, summary_50w="API rotates the session token; web client re-reads it.")
    new = _assert(conn, summary_50w="API rotates the token AND refreshes the cache entry.")
    # Single-live invariant preserved: old closed, new live.
    assert conn.execute("SELECT valid_to FROM atoms WHERE id=?", (old.id,)).fetchone()["valid_to"] is not None
    # Conflict surfaced on the return path + recorded as a live edge.
    assert new.conflicts == [old.id]
    assert _live_contradicts(conn, old.id, new.id)
    # Exactly one CONTRADICTS row (symmetric kind is canonicalized — no dupe).
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE kind='CONTRADICTS' AND valid_to IS NULL"
    ).fetchone()["c"]
    assert n == 1


@pytest.mark.parametrize("src", ["commit", "schema_snapshot", "manifest"])
def test_no_conflict_for_natural_evolution_source(conn, src):
    # Mechanical re-ingestion supersedes silently — no CONTRADICTS — for every
    # source in NATURAL_EVOLUTION_SOURCES, not just commit.
    old = _assert(conn, topic_key="c.x", summary_50w="first", source_kind=src)
    new = _assert(conn, topic_key="c.x", summary_50w="second changed", source_kind=src)
    assert new.id != old.id
    assert conn.execute("SELECT valid_to FROM atoms WHERE id=?", (old.id,)).fetchone()["valid_to"] is not None
    assert new.conflicts == []
    assert not _live_contradicts(conn, old.id, new.id)


def test_edge_failure_does_not_break_supersede(conn, monkeypatch):
    # The CONTRADICTS edge is advisory: if upsert_edge raises, the supersede
    # must still stand and conflicts must stay empty — no exception propagates.
    from dlms_cli import edges as edges_mod

    old = _assert(conn, summary_50w="claim before the edge boom")
    monkeypatch.setattr(
        edges_mod, "upsert_edge",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("edge boom")),
    )
    new = _assert(conn, summary_50w="claim after the edge boom diverges")
    # Supersede survived the edge failure.
    assert conn.execute("SELECT valid_to FROM atoms WHERE id=?", (old.id,)).fetchone()["valid_to"] is not None
    assert conn.execute("SELECT valid_to FROM atoms WHERE id=?", (new.id,)).fetchone()["valid_to"] is None
    # Failed edge → not reported as a conflict, no edge row.
    assert new.conflicts == []
    assert not _live_contradicts(conn, old.id, new.id)


def test_detect_conflict_false_suppresses_edge(conn):
    old = _assert(conn, summary_50w="claim one variant a")
    new = _assert(conn, summary_50w="claim two variant b", detect_conflict=False)
    assert conn.execute("SELECT valid_to FROM atoms WHERE id=?", (old.id,)).fetchone()["valid_to"] is not None
    assert new.conflicts == []
    assert not _live_contradicts(conn, old.id, new.id)


def test_idempotent_assert_has_no_conflict(conn):
    a = _assert(conn)
    b = _assert(conn)  # same claim → idempotent, no supersede, no conflict
    assert a.id == b.id
    assert b.conflicts == []


def test_fresh_topic_assert_has_no_conflict(conn):
    a = _assert(conn, topic_key="brand.new")
    assert a.conflicts == []
