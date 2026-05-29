from __future__ import annotations

import pytest

from dlms_cli import atoms as atoms_mod
from dlms_cli import edges as edges_mod
from dlms_cli import embeddings, retrieval


def _atom(conn, key: str, text: str):
    return atoms_mod.assert_fact(
        conn, type="invariant", topic_key=key, summary_50w=text, source_kind="manual"
    )


def test_retrieve_seed_hit_via_embedding(conn):
    a = _atom(conn, "auth.token", "API rotates the session token, the web client reads it.")
    _atom(conn, "irrelevant", "Completely unrelated stuff about widgets and gizmos.")
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    out = retrieval.retrieve(
        conn, query="session token api", embedder=embeddings.HashEmbedder(), top_n=3
    )
    assert any(r.atom_id == a.id and r.seed for r in out)


def test_retrieve_two_hop_via_mirrors(conn):
    a = _atom(conn, "web.token", "Web client reads the session token from cache")
    b = _atom(conn, "api.token", "API writes the session token to the sessions table")
    edges_mod.upsert_edge(
        conn, src_id=a.id, dst_id=b.id, kind="MIRRORS", source="shared_ref", weight=1.0
    )
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    # Query strongly matches A only; expect B to surface via MIRRORS.
    out = retrieval.retrieve(
        conn,
        query="Web client reads the session token from cache",
        embedder=embeddings.HashEmbedder(),
        top_n=5,
    )
    ids = [r.atom_id for r in out]
    assert a.id in ids
    assert b.id in ids
    b_row = next(r for r in out if r.atom_id == b.id)
    if not b_row.seed:
        assert b_row.via_kind == "MIRRORS"
        assert b_row.depth == 1


def test_retrieve_empty_store_returns_empty(conn):
    out = retrieval.retrieve(
        conn, query="anything", embedder=embeddings.HashEmbedder(), top_n=5
    )
    assert out == []


def test_retrieve_falls_back_to_recent_when_no_seed(conn):
    # Atoms exist but no embeddings yet — fallback path.
    a = _atom(conn, "x", "hello")
    out = retrieval.retrieve(
        conn, query="completely different terms", embedder=embeddings.HashEmbedder(), top_n=5
    )
    # Hash embedder always gives *some* signal, so embedding seed-set is
    # almost always non-empty. We just assert the function returns rows.
    assert any(r.atom_id == a.id for r in out)


def test_rolls_up_weight_is_configured():
    # A typo/omission here would silently fall back to the 0.3 default.
    assert retrieval.EDGE_WEIGHTS["ROLLS_UP"] == 0.6


def test_retrieve_surfaces_module_via_rolls_up(conn):
    # Child matches the query; its module should surface via the ROLLS_UP edge
    # carrying PPR mass — not just be reachable in graph_neighbors.
    child = _atom(conn, "api.handler", "API request handler module entrypoint code")
    module = _atom(conn, "module.api", "The api module groups request handlers")
    edges_mod.upsert_edge(
        conn, src_id=child.id, dst_id=module.id, kind="ROLLS_UP",
        source="shared_ref", weight=0.6,
    )
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    out = retrieval.retrieve(
        conn,
        query="API request handler module entrypoint code",
        embedder=embeddings.HashEmbedder(),
        top_n=5,
    )
    ids = [r.atom_id for r in out]
    assert child.id in ids
    assert module.id in ids
    m_row = next(r for r in out if r.atom_id == module.id)
    if not m_row.seed:  # module may also be an embedding seed; only assert trail if not
        assert m_row.via_kind == "ROLLS_UP"
        assert m_row.depth == 1


def test_drill_down_surfaces_sibling_under_hot_module(conn):
    # Deterministic seeding via file_context (no embeddings) so the seed set is
    # exactly {seed}. seed + sibling both roll up into the same module.
    def mk(topic, ref, tier="symbol"):
        return atoms_mod.assert_fact(
            conn, type="glossary", topic_key=topic, tier=tier,
            summary_50w=f"{topic} fact", source_kind="manual", source_ref=ref,
        )

    seed = mk("api.login", "api/login.py")
    module = mk("module.api", "api", tier="module")
    sibling = mk("api.logout", "api/logout.py")  # NOT a seed, no direct query match
    edges_mod.upsert_edge(conn, src_id=seed.id, dst_id=module.id,
                          kind="ROLLS_UP", source="shared_ref", weight=0.6)
    edges_mod.upsert_edge(conn, src_id=sibling.id, dst_id=module.id,
                          kind="ROLLS_UP", source="shared_ref", weight=0.6)

    emb = embeddings.HashEmbedder()
    fc = ["api/login.py"]  # seeds {seed} only; no embeddings stored
    drilled = retrieval.retrieve(conn, query="x", file_context=fc, embedder=emb,
                                 top_n=20, drill_down=True)
    flat = retrieval.retrieve(conn, query="x", file_context=fc, embedder=emb,
                              top_n=20, drill_down=False)
    drilled_ids = {r.atom_id for r in drilled}
    flat_ids = {r.atom_id for r in flat}

    assert {seed.id, module.id} <= drilled_ids
    # The sibling is reachable ONLY because its module went hot and we drilled in.
    assert sibling.id in drilled_ids
    assert sibling.id not in flat_ids
    s_row = next(r for r in drilled if r.atom_id == sibling.id)
    assert s_row.via_kind == "ROLLS_UP"
    assert s_row.score > 0  # surfaced via downward mass-flow, not just universe inclusion


def test_drill_modules_cap_bounds_expansion(conn):
    # 4 symmetric hot modules, each with a seeded child + a non-seeded sibling.
    # With drill_modules=2 only TWO modules' siblings may surface — the §14.1
    # bounding guarantee — regardless of which two win the (deterministic) tie.
    def mk(topic, ref, tier="symbol"):
        return atoms_mod.assert_fact(
            conn, type="glossary", topic_key=topic, tier=tier,
            summary_50w=f"{topic} fact", source_kind="manual", source_ref=ref,
        )

    siblings = []
    fc = []
    for i in range(4):
        seed = mk(f"m{i}.seed", f"m{i}/seed.py")
        module = mk(f"module.m{i}", f"m{i}", tier="module")
        sib = mk(f"m{i}.sib", f"m{i}/sib.py")
        edges_mod.upsert_edge(conn, src_id=seed.id, dst_id=module.id,
                              kind="ROLLS_UP", source="shared_ref", weight=0.6)
        edges_mod.upsert_edge(conn, src_id=sib.id, dst_id=module.id,
                              kind="ROLLS_UP", source="shared_ref", weight=0.6)
        siblings.append(sib.id)
        fc.append(f"m{i}/seed.py")

    out = retrieval.retrieve(conn, query="x", file_context=fc,
                             embedder=embeddings.HashEmbedder(),
                             top_n=50, drill_modules=2)
    surfaced = {r.atom_id for r in out} & set(siblings)
    assert len(surfaced) == 2  # capped — not all 4


def test_drill_down_is_noop_without_module_atoms(conn):
    # With no module-tier atoms, drill_down on/off must produce identical output.
    a = _atom(conn, "x.one", "alpha beta gamma delta token flow")
    b = _atom(conn, "x.two", "alpha beta gamma delta token flow sibling")
    edges_mod.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="MIRRORS",
                          source="shared_ref", weight=1.0)
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    q = "alpha beta gamma delta token flow"
    on = retrieval.retrieve(conn, query=q, embedder=embeddings.HashEmbedder(),
                            top_n=10, drill_down=True)
    off = retrieval.retrieve(conn, query=q, embedder=embeddings.HashEmbedder(),
                             top_n=10, drill_down=False)
    assert [r.atom_id for r in on] == [r.atom_id for r in off]


def test_file_context_seeds_structural_hits(conn):
    a = atoms_mod.assert_fact(
        conn,
        type="schema_fact",
        topic_key="table:users",
        summary_50w="users table",
        source_kind="schema_snapshot",
        source_ref="db/schema.sql",
    )
    out = retrieval.retrieve(
        conn,
        query="zzz unrelated",
        file_context=["db/schema.sql"],
        embedder=embeddings.HashEmbedder(),
        top_n=3,
    )
    assert any(r.atom_id == a.id and r.seed for r in out)


def test_max_hops_bounds_universe(conn):
    # Linear MIRRORS chain a-b-c-d-e seeded at `a`; max_hops=2 must exclude
    # everything beyond 2 hops (SPEC §14.4).
    def mk(key, ref):
        return atoms_mod.assert_fact(
            conn, type="invariant", topic_key=key, summary_50w=f"{key} body text",
            source_kind="manual", source_ref=ref,
        )
    nodes = {n: mk(n, f"{n}.py") for n in "abcde"}
    for x, y in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")):
        edges_mod.upsert_edge(conn, src_id=nodes[x].id, dst_id=nodes[y].id,
                              kind="MIRRORS", source="shared_ref", weight=1.0)
    out = retrieval.retrieve(conn, query="x", file_context=["a.py"],
                             embedder=embeddings.HashEmbedder(), top_n=50,
                             max_hops=2, drill_down=False)
    ids = {r.atom_id for r in out}
    assert {nodes["a"].id, nodes["b"].id, nodes["c"].id} <= ids   # within 2 hops
    assert nodes["d"].id not in ids and nodes["e"].id not in ids  # beyond the cap


def test_ppr_ranking_is_stable_past_convergence(conn):
    # Early-exit (ε) means extra iterations don't change the ranking.
    a = _atom(conn, "a.one", "alpha beta gamma signal token")
    b = _atom(conn, "b.two", "alpha beta gamma signal token sibling")
    edges_mod.upsert_edge(conn, src_id=a.id, dst_id=b.id, kind="MIRRORS",
                          source="shared_ref", weight=1.0)
    embeddings.embed_pending(conn, embedder=embeddings.HashEmbedder())
    q = "alpha beta gamma signal token"
    few = retrieval.retrieve(conn, query=q, embedder=embeddings.HashEmbedder(),
                             top_n=10, iterations=20)
    many = retrieval.retrieve(conn, query=q, embedder=embeddings.HashEmbedder(),
                              top_n=10, iterations=1000)
    assert [r.atom_id for r in few] == [r.atom_id for r in many]


def test_run_ppr_epsilon_actually_fires_early_exit():
    # Directly prove the `break` fires: a huge epsilon must terminate after one
    # iteration, giving the SAME result as iterations=1. If the break were
    # removed, the huge-epsilon run would keep iterating and diverge from this.
    universe = {"x", "y"}
    seeds = {"x": 1.0}
    adj = {
        "x": [("y", "MIRRORS", 1, 1.0)],
        "y": [("x", "MIRRORS", 1, 1.0)],
    }
    forced = retrieval._run_ppr(universe, seeds, adj, alpha=0.15, iterations=1000, epsilon=10.0)
    one = retrieval._run_ppr(universe, seeds, adj, alpha=0.15, iterations=1, epsilon=0.0)
    assert forced == one  # epsilon=10 → broke after iteration 1


@pytest.mark.parametrize("max_hops", [1, 2, 3])
def test_max_hops_boundary_inclusive(conn, max_hops):
    # Chain n0-n1-n2-n3-n4 seeded at n0; exactly n0..n{max_hops} survive.
    # Covers the DEFAULT (3) and guards the `hops < max_hops` off-by-one.
    nodes = {
        i: atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"n{i}", summary_50w=f"n{i} body",
            source_kind="manual", source_ref=f"n{i}.py",
        )
        for i in range(5)
    }
    for i in range(4):
        edges_mod.upsert_edge(conn, src_id=nodes[i].id, dst_id=nodes[i + 1].id,
                              kind="MIRRORS", source="shared_ref", weight=1.0)
    out = retrieval.retrieve(conn, query="x", file_context=["n0.py"],
                             embedder=embeddings.HashEmbedder(), top_n=50,
                             max_hops=max_hops, drill_down=False)
    ids = {r.atom_id for r in out}
    for i in range(max_hops + 1):
        assert nodes[i].id in ids, f"n{i} should be within {max_hops} hops"
    for i in range(max_hops + 1, 5):
        assert nodes[i].id not in ids, f"n{i} should be beyond {max_hops} hops"


def test_default_max_hops_is_three(conn):
    # retrieve() with no max_hops arg reaches exactly 3 hops.
    nodes = {
        i: atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"m{i}", summary_50w=f"m{i} body",
            source_kind="manual", source_ref=f"m{i}.py",
        )
        for i in range(5)
    }
    for i in range(4):
        edges_mod.upsert_edge(conn, src_id=nodes[i].id, dst_id=nodes[i + 1].id,
                              kind="MIRRORS", source="shared_ref", weight=1.0)
    ids = {r.atom_id for r in retrieval.retrieve(
        conn, query="x", file_context=["m0.py"],
        embedder=embeddings.HashEmbedder(), top_n=50, drill_down=False)}
    assert nodes[3].id in ids       # depth 3 included by default
    assert nodes[4].id not in ids   # depth 4 excluded


def test_provenance_preserved_at_hop_boundary(conn):
    # The deepest in-universe atom (n2 at depth 2 under max_hops=2) must still
    # carry correct depth/via_kind despite the provenance-BFS cap.
    nodes = {
        i: atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"p{i}", summary_50w=f"p{i} body",
            source_kind="manual", source_ref=f"p{i}.py",
        )
        for i in range(3)
    }
    for i in range(2):
        edges_mod.upsert_edge(conn, src_id=nodes[i].id, dst_id=nodes[i + 1].id,
                              kind="MIRRORS", source="shared_ref", weight=1.0)
    out = retrieval.retrieve(conn, query="x", file_context=["p0.py"],
                             embedder=embeddings.HashEmbedder(), top_n=50,
                             max_hops=2, drill_down=False)
    n2 = next(r for r in out if r.atom_id == nodes[2].id)
    assert n2.depth == 2
    assert n2.via_kind == "MIRRORS"


def test_decay_downranks_stale_atom(conn):
    import time as _t
    now = int(_t.time())

    def mk(key, ref):
        return atoms_mod.assert_fact(
            conn, type="invariant", topic_key=key, summary_50w=f"{key} body",
            source_kind="manual", source_ref=ref,
        )
    fresh = mk("fresh", "fresh.py")
    stale = mk("stale", "stale.py")
    old = now - 200 * 86_400  # ~200 days > 90-day half-life
    conn.execute(
        "UPDATE atoms SET asserted_at=?, updated_at=?, liveness_last_ok=NULL WHERE id=?",
        (old, old, stale.id),
    )
    out = retrieval.retrieve(conn, query="x", file_context=["fresh.py", "stale.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10)
    scores = {r.atom_id: r.score for r in out}
    assert scores[fresh.id] > scores[stale.id]  # stale sank under decay


def test_decay_disabled_leaves_scores_equal(conn):
    import time as _t
    now = int(_t.time())

    def mk(key, ref):
        return atoms_mod.assert_fact(
            conn, type="invariant", topic_key=key, summary_50w=f"{key} body",
            source_kind="manual", source_ref=ref,
        )
    fresh = mk("fresh", "fresh.py")
    stale = mk("stale", "stale.py")
    old = now - 200 * 86_400
    conn.execute("UPDATE atoms SET asserted_at=?, updated_at=? WHERE id=?",
                 (old, old, stale.id))
    out = retrieval.retrieve(conn, query="x", file_context=["fresh.py", "stale.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10, decay=False)
    scores = {r.atom_id: r.score for r in out}
    assert abs(scores[fresh.id] - scores[stale.id]) < 1e-9  # equal base, decay off


def test_low_confidence_atom_downranked(conn):
    hi = atoms_mod.assert_fact(conn, type="invariant", topic_key="hi", summary_50w="hi body",
                               source_kind="manual", source_ref="hi.py", confidence=1.0)
    lo = atoms_mod.assert_fact(conn, type="invariant", topic_key="lo", summary_50w="lo body",
                               source_kind="manual", source_ref="lo.py", confidence=0.1)
    out = retrieval.retrieve(conn, query="x", file_context=["hi.py", "lo.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10)
    scores = {r.atom_id: r.score for r in out}
    assert scores[hi.id] > scores[lo.id]


def test_apply_decay_half_life_math(conn):
    # Numeric assertion on the curve itself, not just ordering: at exactly one
    # half-life the score must halve, at two half-lives it must quarter. Guards
    # against an exponent unit slip (age/half_life_days vs age/hl_secs).
    import time as _t
    now = int(_t.time())
    hl_days = 90.0

    def mk(key, age_days, conf=1.0):
        a = atoms_mod.assert_fact(
            conn, type="invariant", topic_key=key, summary_50w=f"{key} body",
            source_kind="manual", source_ref=f"{key}.py", confidence=conf,
        )
        ts = now - int(age_days * 86_400)
        conn.execute(
            "UPDATE atoms SET asserted_at=?, updated_at=?, liveness_last_ok=NULL WHERE id=?",
            (ts, ts, a.id),
        )
        return a

    a0 = mk("a0", 0)        # fresh → factor 1.0
    a1 = mk("a1", hl_days)  # one half-life → factor 0.5
    a2 = mk("a2", 2 * hl_days)  # two half-lives → factor 0.25
    rank = {a0.id: 1.0, a1.id: 1.0, a2.id: 1.0}
    adj = retrieval._apply_decay(conn, rank, half_life_days=hl_days, now=now)
    assert abs(adj[a0.id] - 1.0) < 1e-9
    assert abs(adj[a1.id] - 0.5) < 1e-3
    assert abs(adj[a2.id] - 0.25) < 1e-3


def test_apply_decay_coalesce_prefers_liveness_last_ok(conn):
    # A recent liveness_last_ok must keep an atom fresh even when asserted_at /
    # updated_at are ancient — exercises the first COALESCE branch.
    import time as _t
    now = int(_t.time())
    old = now - 300 * 86_400
    a = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="reconf", summary_50w="reconfirmed body",
        source_kind="manual", source_ref="reconf.py",
    )
    conn.execute(
        "UPDATE atoms SET asserted_at=?, updated_at=?, liveness_last_ok=? WHERE id=?",
        (old, old, now, a.id),
    )
    adj = retrieval._apply_decay(conn, {a.id: 1.0}, half_life_days=90.0, now=now)
    assert adj[a.id] > 0.99  # liveness_last_ok=now wins → effectively no decay


def test_apply_decay_edge_returns(conn):
    # half_life_days <= 0 and empty rank are no-op early returns.
    a = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="e", summary_50w="e body",
        source_kind="manual", source_ref="e.py",
    )
    assert retrieval._apply_decay(conn, {}, half_life_days=90.0, now=0) == {}
    rank = {a.id: 1.0}
    assert retrieval._apply_decay(conn, rank, half_life_days=0, now=0) == rank
    assert retrieval._apply_decay(conn, rank, half_life_days=-5, now=0) == rank


# --- SPEC §14.5 lazy read-time liveness -------------------------------------

def _live_pair(conn, tmp_path):
    """A live atom (predicate matches) and a dead one (predicate fails), both
    seeded via source_ref='f.py'."""
    from dlms_cli.atoms import Liveness
    (tmp_path / "f.py").write_text("ALIVE token here")
    live = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="live", summary_50w="live one",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("regex", "f.py", r"ALIVE"),
    )
    dead = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="dead", summary_50w="dead one",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("regex", "f.py", r"GONE"),
    )
    return live, dead


def test_read_time_verify_drops_dead_atom(conn, tmp_path):
    live, dead = _live_pair(conn, tmp_path)
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10,
                             repo_root=tmp_path)
    ids = {r.atom_id for r in out}
    assert live.id in ids       # predicate matched → surfaced
    assert dead.id not in ids   # predicate failed → not returned


def test_read_time_verify_skipped_without_repo_root(conn, tmp_path):
    # No repo_root → no verification → identical to pre-§14.5 (dead still shown).
    live, dead = _live_pair(conn, tmp_path)
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10)
    ids = {r.atom_id for r in out}
    assert live.id in ids
    assert dead.id in ids


def test_read_time_verify_trusts_fresh_check(conn, tmp_path):
    # A failing predicate is NOT re-run if its last check is within freshness —
    # we trust the recent result and keep the atom (lazy).
    import time as _t
    from dlms_cli.atoms import Liveness
    (tmp_path / "f.py").write_text("no match in here")
    a = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="fresh", summary_50w="fresh one",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("regex", "f.py", r"WONTMATCH"),
    )
    conn.execute("UPDATE atoms SET liveness_last_ok=? WHERE id=?",
                 (int(_t.time()), a.id))
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10,
                             repo_root=tmp_path, verify_freshness_seconds=3600)
    assert a.id in {r.atom_id for r in out}  # trusted as fresh, not re-run


def test_read_time_verify_backfills_dropped_slot(conn, tmp_path):
    # The key §14.5 bound: a dead atom INSIDE the top_n window is replaced by a
    # live atom that ranked just OUTSIDE it — result count stays at top_n. We
    # force the geometry deterministically: the dead atom is fresh (high decay
    # factor → ranks inside the window) while one live atom is made stale
    # (heavy decay → ranks just outside), so dropping the dead slot must pull
    # that stale-but-live atom in to backfill.
    import time as _t
    from dlms_cli.atoms import Liveness
    (tmp_path / "f.py").write_text("ALIVE")
    fresh_live = set()
    for i in range(4):
        a = atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"live{i}", summary_50w=f"live {i}",
            source_kind="manual", source_ref="f.py",
            liveness=Liveness("regex", "f.py", r"ALIVE"),
        )
        fresh_live.add(a.id)
    dead = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="dead", summary_50w="dead but fresh",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("regex", "f.py", r"GONE"),
    )
    stale_live = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="stalelive", summary_50w="live but stale",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("regex", "f.py", r"ALIVE"),
    )
    # Sink stale_live below the dead atom via decay (≈200 days unconfirmed).
    old = int(_t.time()) - 200 * 86_400
    conn.execute("UPDATE atoms SET asserted_at=?, updated_at=?, liveness_last_ok=NULL WHERE id=?",
                 (old, old, stale_live.id))
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=5,
                             repo_root=tmp_path)
    ids = [r.atom_id for r in out]
    assert len(ids) == 5                 # window stayed full despite the drop
    assert dead.id not in ids            # dead atom excluded
    assert stale_live.id in ids          # backfilled from just outside the window
    assert fresh_live <= set(ids)        # the 4 fresh live atoms remain


def test_read_time_verify_keeps_unrunnable_predicate(conn, tmp_path):
    # ast/sql predicates aren't implemented — "can't verify" != "verified dead",
    # so such atoms are KEPT in retrieve results, not dropped.
    from dlms_cli.atoms import Liveness
    a = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="astp", summary_50w="ast one",
        source_kind="manual", source_ref="f.py",
        liveness=Liveness("ast", "f.py", "somequery"),
    )
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10,
                             repo_root=tmp_path)
    assert a.id in {r.atom_id for r in out}


def test_verify_live_false_opts_out_with_repo_root(conn, tmp_path):
    # verify_live=False disables verification even when a repo_root is present
    # (distinct from the repo_root=None path) — dead atom still returned.
    live, dead = _live_pair(conn, tmp_path)
    out = retrieval.retrieve(conn, query="x", file_context=["f.py"],
                             embedder=embeddings.HashEmbedder(), top_n=10,
                             repo_root=tmp_path, verify_live=False)
    ids = {r.atom_id for r in out}
    assert dead.id in ids


def test_liveness_meta_empty_ids(conn):
    assert retrieval._liveness_meta(conn, []) == {}
