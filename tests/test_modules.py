"""Tests for hierarchical atoms (SPEC §14.1): tier field + modules ingester."""

from __future__ import annotations

from dlms_cli import atoms, edges
from dlms_cli.ingesters import modules
from dlms_cli.ingesters.base import IngestContext


def _atom(conn, topic, ref, *, tier="symbol"):
    return atoms.assert_fact(
        conn, type="glossary", topic_key=topic, tier=tier,
        summary_50w=f"{topic} fact", source_kind="manual", source_ref=ref,
    )


def test_tier_defaults_to_symbol(conn):
    a = _atom(conn, "sym:handler", "api/handlers.py")
    assert a.tier == "symbol"
    assert atoms.get_atom(conn, a.id).atom.tier == "symbol"


def test_module_tier_persists(conn):
    a = _atom(conn, "module:api", "api", tier="module")
    assert a.tier == "module"
    assert atoms.get_atom(conn, a.id).atom.tier == "module"


def test_rolls_up_edge_is_directed(conn):
    child = _atom(conn, "sym:h", "api/h.py")
    mod = _atom(conn, "module:api", "api", tier="module")
    e = edges.upsert_edge(
        conn, src_id=child.id, dst_id=mod.id, kind="ROLLS_UP",
        source="shared_ref", weight=0.6,
    )
    assert e.kind == "ROLLS_UP"
    assert e.directed is True
    nbrs = edges.graph_neighbors(conn, child.id, kinds=["ROLLS_UP"])
    assert [h.atom_id for h in nbrs] == [mod.id]


def test_modules_ingester_emits_and_rolls_up(conn, tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "handlers.py").write_text("def h(): pass")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.ts").write_text("export const x = 1")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# docs only, no source")

    # children that the symbols ingester would already have produced
    _atom(conn, "sym:h", "api/handlers.py")
    _atom(conn, "sym:x", "web/app.ts")

    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r1", exclude=[])
    res = modules.run(ctx)

    # docs/ has no source files → not a module
    assert res.atoms_inserted == 2
    module_topics = {
        a.topic_key for a in atoms.query_atoms(conn, type="glossary")
        if a.tier == "module"
    }
    assert module_topics == {"module:api", "module:web"}

    rollups = conn.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE kind = 'ROLLS_UP'"
    ).fetchone()
    assert rollups["c"] == 2


def test_modules_ingester_skips_excluded_dirs(conn, tmp_path):
    (tmp_path / "thirdparty").mkdir()
    (tmp_path / "thirdparty" / "lib.py").write_text("x = 1")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("x = 1")

    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r1", exclude=["thirdparty"])
    res = modules.run(ctx)

    # thirdparty excluded via config, .git excluded structurally → nothing emitted
    assert res.atoms_inserted == 0


def test_module_emitted_even_with_no_children(conn, tmp_path):
    # A source dir with no pre-existing child atoms still gets a module atom.
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "main.py").write_text("def run(): pass")

    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r1", exclude=[])
    res = modules.run(ctx)

    assert res.atoms_inserted == 1
    rollups = conn.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE kind = 'ROLLS_UP'"
    ).fetchone()
    assert rollups["c"] == 0


def test_rollup_matches_exact_and_nested_source_refs(conn, tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "h.py").write_text("x = 1")
    (tmp_path / "api" / "sub").mkdir()
    (tmp_path / "api" / "sub" / "deep.py").write_text("x = 1")

    # exact-match child (source_ref == dir name) hits the `source_ref = ?` branch;
    # nested child hits the `LIKE 'api/%'` branch.
    _atom(conn, "mod:api", "api", tier="file")          # source_ref exactly "api"
    _atom(conn, "sym:deep", "api/sub/deep.py")          # nested under api/

    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r1", exclude=[])
    modules.run(ctx)

    rollups = conn.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE kind = 'ROLLS_UP'"
    ).fetchone()
    assert rollups["c"] == 2  # both the exact-match and the nested child rolled up
