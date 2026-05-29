from __future__ import annotations

import subprocess

import pytest

from dlms_cli import enumerator


@pytest.fixture
def staged_repo(tmp_path, monkeypatch):
    def g(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    (tmp_path / "app.py").write_text("def existing():\n    return 0\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_staged_diff_returns_empty(staged_repo):
    out = enumerator.enumerate_edge_cases()
    assert out == []


def test_python_change_flags_test_gap(staged_repo):
    (staged_repo / "app.py").write_text(
        "def existing():\n    return 0\n\n"
        "def new_processor():\n    return 1\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=staged_repo, check=True, capture_output=True)
    out = enumerator.enumerate_edge_cases()
    assert any(f.kind == "test_gap" and "new_processor" in f.claim for f in out)


def test_sql_change_mandates_schema_migration(staged_repo):
    (staged_repo / "schema.sql").write_text(
        "CREATE TABLE widgets (id INT);\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=staged_repo, check=True, capture_output=True)
    out = enumerator.enumerate_edge_cases()
    assert any(f.kind == "schema_migration" for f in out)


def test_docs_only_emits_nothing(staged_repo):
    (staged_repo / "README.md").write_text("# Updated docs\n")
    subprocess.run(["git", "add", "-A"], cwd=staged_repo, check=True, capture_output=True)
    out = enumerator.enumerate_edge_cases()
    assert out == []


def test_format_report_empty():
    assert "no edge cases" in enumerator.format_report([])


def test_format_report_with_findings():
    f = enumerator.Finding(
        kind="test_gap", severity="high", claim="X needs a test",
        evidence=["app.py:10"], suggested_action="write one",
    )
    rep = enumerator.format_report([f])
    assert "[high]" in rep
    assert "X needs a test" in rep
