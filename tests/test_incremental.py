"""Tests for incremental, git-diff-keyed ingest (SPEC §14.2)."""

from __future__ import annotations

import subprocess

from dlms_cli import atoms, git_utils, ingesters
from dlms_cli.ingesters import manifest, modules, readme, symbols
from dlms_cli.ingesters.base import IngestContext, iter_files


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_changed_files_since_none_means_full_scan():
    # None sentinel (first run) — not an empty set.
    assert git_utils.changed_files_since("/tmp", None) is None


def test_is_changed_full_vs_incremental(conn, tmp_path):
    full = IngestContext(conn=conn, root=tmp_path, repo_id="r")  # changed_files=None
    assert full.is_changed("anything.py") is True
    inc = IngestContext(conn=conn, root=tmp_path, repo_id="r", changed_files={"a.py"})
    assert inc.is_changed("a.py") is True
    assert inc.is_changed("b.py") is False


def test_iter_files_changed_visits_only_changed(tmp_path):
    for n in ("a.py", "b.py", "c.py"):
        (tmp_path / n).write_text("x = 1")
    got = iter_files(tmp_path, suffixes=(".py",), exclude=[], max_kb=512,
                     changed={"a.py", "c.py"})
    assert sorted(p.name for p in got) == ["a.py", "c.py"]  # b.py never visited


def test_iter_files_changed_applies_suffix_and_skips_missing(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "a.txt").write_text("x")
    got = iter_files(tmp_path, suffixes=(".py",), exclude=[], max_kb=512,
                     changed={"a.py", "a.txt", "deleted.py"})
    # non-matching suffix dropped; missing path (e.g. deleted) skipped gracefully
    assert [p.name for p in got] == ["a.py"]


def test_changed_files_since_real_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "one.py").write_text("x = 1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    sha = git_utils.head_sha(tmp_path)

    (tmp_path / "two.py").write_text("y = 2")  # untracked working-tree change
    changed = git_utils.changed_files_since(tmp_path, sha)
    assert changed is not None
    assert "two.py" in changed       # dirty file picked up
    assert "one.py" not in changed   # unchanged committed file excluded


def test_modules_incremental_only_touches_changed_dirs(conn, tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "h.py").write_text("x = 1")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "a.py").write_text("x = 1")

    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r",
                        changed_files={"api/h.py"})
    modules.run(ctx)

    mods = {
        a.topic_key for a in atoms.query_atoms(conn, type="glossary")
        if a.tier == "module"
    }
    assert mods == {"module:api"}  # web/ untouched, never rglob'd


def _module_topics(conn):
    return {
        a.topic_key for a in atoms.query_atoms(conn, type="glossary")
        if a.tier == "module"
    }


def test_modules_incremental_nested_root_and_nonsource(conn, tmp_path):
    (tmp_path / "api" / "sub").mkdir(parents=True)
    (tmp_path / "api" / "sub" / "deep.py").write_text("x = 1")
    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r", changed_files={
        "api/sub/deep.py",  # nested → maps to top-level 'api'
        "setup.py",          # root-level file → no module
        "api/notes.md",      # non-source under a dir → ignored
    })
    modules.run(ctx)
    assert _module_topics(conn) == {"module:api"}


def test_readme_incremental_skips_unchanged(conn, tmp_path):
    (tmp_path / "README.md").write_text("# Title\n\nIntro paragraph here.")
    skip = readme.run(IngestContext(conn=conn, root=tmp_path, repo_id="r",
                                    changed_files=set()))
    assert skip.atoms_inserted == 0
    do = readme.run(IngestContext(conn=conn, root=tmp_path, repo_id="r",
                                  changed_files={"README.md"}))
    assert do.atoms_inserted >= 1


def test_manifest_incremental_skips_unchanged(conn, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nrequires-python=">=3.11"\ndependencies=["rich>=13"]\n'
    )
    skip = manifest.run(IngestContext(conn=conn, root=tmp_path, repo_id="r",
                                      changed_files=set()))
    assert skip.atoms_inserted == 0
    do = manifest.run(IngestContext(conn=conn, root=tmp_path, repo_id="r",
                                    changed_files={"pyproject.toml"}))
    assert do.atoms_inserted >= 1


def test_budget_exhausted_marks_truncated(conn, tmp_path):
    # budget 0 → every adapter is skipped as truncated, so the caller must NOT
    # advance the indexed sha (durability guard, SPEC §14.2).
    ctx = IngestContext(conn=conn, root=tmp_path, repo_id="r")
    results = ingesters.run(ctx, enabled=["readme", "symbols"], budget_seconds=0.0)
    assert all(r.truncated for r in results.values())


def test_symbols_cap_marks_truncated(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(symbols, "MAX_ATOMS", 1)
    (tmp_path / "a.py").write_text("def one(): pass\ndef two(): pass\n")
    (tmp_path / "b.py").write_text("def three(): pass\n")
    res = symbols.run(IngestContext(conn=conn, root=tmp_path, repo_id="r"))
    assert res.truncated is True


def test_changed_files_since_rename_and_committed_only(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "one.py").write_text("x = 1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    base = git_utils.head_sha(tmp_path)

    # committed-only change with a clean tree exercises the diff arm
    (tmp_path / "one.py").write_text("x = 2")
    _git(tmp_path, "commit", "-aqm", "edit")
    changed = git_utils.changed_files_since(tmp_path, base)
    assert changed == {"one.py"}

    # rename in the working tree: the NEW path is reported, not the old
    head = git_utils.head_sha(tmp_path)
    _git(tmp_path, "mv", "one.py", "renamed.py")
    changed2 = git_utils.changed_files_since(tmp_path, head)
    assert "renamed.py" in changed2
    assert "one.py" not in changed2


def test_changed_files_since_full_scan_on_bad_sha(tmp_path):
    # not a git repo → committed diff fails → None (full scan), never a crash
    assert git_utils.changed_files_since(tmp_path, "deadbeef") is None
