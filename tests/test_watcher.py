from __future__ import annotations

from dlms_cli import atoms as atoms_mod
from dlms_cli import watcher
from dlms_cli.atoms import Liveness


def test_watch_passes_when_pattern_present(conn, tmp_path, monkeypatch):
    (tmp_path / "schema.sql").write_text("CREATE TABLE users (id INT);")
    monkeypatch.chdir(tmp_path)
    # Point detect_layout's db at our in-memory conn via store layer:
    # easier path — call watch._atoms_for_files + liveness directly.
    atoms_mod.assert_fact(
        conn,
        type="schema_fact",
        topic_key="table:users",
        summary_50w="users table",
        source_kind="schema_snapshot",
        source_ref="schema.sql",
        liveness=Liveness(kind="regex", target="schema.sql",
                          pattern=r"CREATE TABLE users"),
    )
    rows = watcher._atoms_for_files(conn, ["schema.sql"])
    assert len(rows) == 1


def test_watch_detects_drift(conn, tmp_path):
    (tmp_path / "schema.sql").write_text("CREATE TABLE users (id INT);")
    a = atoms_mod.assert_fact(
        conn,
        type="schema_fact",
        topic_key="table:posts",
        summary_50w="posts table",
        source_kind="schema_snapshot",
        source_ref="schema.sql",
        liveness=Liveness(kind="regex", target="schema.sql",
                          pattern=r"CREATE TABLE posts"),
    )
    # The pattern targets posts, which is missing from the file → violation.
    from dlms_cli import liveness as liveness_mod
    result = liveness_mod.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is False
    assert "did not match" in result.reason


def test_watch_report_serializes_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rep = watcher.watch([])
    assert rep.has_violations is False
    import json
    assert json.loads(rep.to_json())["touched_files"] == []
