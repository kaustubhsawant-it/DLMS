import pytest

from dlms_cli import atoms, liveness
from dlms_cli.atoms import Liveness


def _assert(conn, **kw):
    return atoms.assert_fact(
        conn,
        type="invariant",
        topic_key=kw.pop("topic_key", "topic"),
        summary_50w=kw.pop("summary_50w", "x"),
        source_kind="manual",
        **kw,
    )


def test_regex_passes_and_bumps_last_ok(conn, tmp_path):
    (tmp_path / "README.md").write_text("This project is DLMS-powered.")
    a = _assert(conn, liveness=Liveness("regex", "README.md", r"DLMS"))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is True
    assert result.reason == "regex matched"
    row = conn.execute("SELECT liveness_last_ok FROM atoms WHERE id = ?", (a.id,)).fetchone()
    assert row["liveness_last_ok"] is not None


def test_regex_fails_when_pattern_missing(conn, tmp_path):
    (tmp_path / "README.md").write_text("no match here")
    a = _assert(conn, liveness=Liveness("regex", "README.md", r"DLMS"))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is False
    assert "did not match" in result.reason
    row = conn.execute("SELECT liveness_last_ok FROM atoms WHERE id = ?", (a.id,)).fetchone()
    assert row["liveness_last_ok"] is None


def test_regex_fails_when_file_missing(conn, tmp_path):
    a = _assert(conn, liveness=Liveness("regex", "missing.txt", r".*"))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is False
    assert "missing" in result.reason


def test_regex_rejects_path_traversal(conn, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    a = _assert(conn, liveness=Liveness("regex", "../outside.txt", r"secret"))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is False
    assert "escape" in result.reason


def test_regex_invalid_pattern(conn, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    a = _assert(conn, liveness=Liveness("regex", "f.txt", r"["))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is False
    assert "invalid regex" in result.reason


def test_none_kind_trivially_passes(conn, tmp_path):
    a = _assert(conn, liveness=Liveness("none"))
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is True
    # `none` doesn't bump last_ok (nothing was actually checked)
    row = conn.execute("SELECT liveness_last_ok FROM atoms WHERE id = ?", (a.id,)).fetchone()
    assert row["liveness_last_ok"] is None


def test_no_liveness_metadata_passes(conn, tmp_path):
    a = _assert(conn)  # no liveness=
    result = liveness.check_atom(conn, a.id, repo_root=tmp_path)
    assert result.ok is True


def test_ast_and_sql_kinds_not_implemented(conn, tmp_path):
    a_ast = _assert(conn, topic_key="ast", summary_50w="a", liveness=Liveness("ast", "f", "p"))
    a_sql = _assert(conn, topic_key="sql", summary_50w="b", liveness=Liveness("sql", "f", "p"))
    with pytest.raises(NotImplementedError):
        liveness.check_atom(conn, a_ast.id, repo_root=tmp_path)
    with pytest.raises(NotImplementedError):
        liveness.check_atom(conn, a_sql.id, repo_root=tmp_path)


def test_stale_atoms_finds_unchecked(conn, tmp_path):
    a = _assert(conn, topic_key="t1", summary_50w="never checked",
                liveness=Liveness("regex", "README.md", r"x"))
    _assert(conn, topic_key="t2", summary_50w="no predicate")  # not stale-eligible
    stale = liveness.stale_atoms(conn)
    assert [r["id"] for r in stale] == [a.id]


def test_check_atom_unknown_raises(conn, tmp_path):
    with pytest.raises(KeyError):
        liveness.check_atom(conn, "ghost", repo_root=tmp_path)


# --- SPEC §14.5 background revalidation sweep -------------------------------

def test_revalidate_sweep_rechecks_and_bumps(conn, tmp_path):
    (tmp_path / "README.md").write_text("DLMS lives here")
    a = _assert(conn, topic_key="live.one", summary_50w="ok",
                liveness=Liveness("regex", "README.md", r"DLMS"))
    dead = _assert(conn, topic_key="dead.one", summary_50w="gone",
                   liveness=Liveness("regex", "README.md", r"NOPE"))
    res = liveness.revalidate_sweep(conn, repo_root=tmp_path)
    assert res.checked == 2
    assert res.passed == 1
    assert res.failed == [dead.id]
    # Passing atom got its last_ok bumped (so a later sweep skips it as fresh).
    assert conn.execute("SELECT liveness_last_ok FROM atoms WHERE id=?", (a.id,)).fetchone()["liveness_last_ok"] is not None


def test_revalidate_sweep_is_bounded_by_limit(conn, tmp_path):
    (tmp_path / "README.md").write_text("DLMS")
    for i in range(5):
        _assert(conn, topic_key=f"t{i}", summary_50w=f"s{i}",
                liveness=Liveness("regex", "README.md", r"DLMS"))
    res = liveness.revalidate_sweep(conn, repo_root=tmp_path, limit=2)
    assert res.checked == 2  # never scans all 5 — bounded by limit


def test_revalidate_sweep_counts_unrunnable_as_failed(conn, tmp_path):
    a = _assert(conn, topic_key="ast.one", summary_50w="a",
                liveness=Liveness("ast", "f", "p"))
    res = liveness.revalidate_sweep(conn, repo_root=tmp_path)
    assert res.checked == 1
    assert res.passed == 0
    assert res.failed == [a.id]


def test_revalidate_sweep_empty_when_nothing_stale(conn, tmp_path):
    _assert(conn, topic_key="np", summary_50w="no predicate")  # not stale-eligible
    res = liveness.revalidate_sweep(conn, repo_root=tmp_path)
    assert res.checked == 0
    assert res.passed == 0
    assert res.failed == []
