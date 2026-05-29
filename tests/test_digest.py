from __future__ import annotations

import json
import subprocess

import pytest

from dlms_cli import digest as digest_mod


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """A throwaway workspace with .dlms initialized + a git history."""
    def g(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    (tmp_path / "f.txt").write_text("hello\n")
    g("add", "-A")
    g("commit", "-q", "-m", "first commit")

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_build_digest_smoke(tmp_workspace):
    d = digest_mod.build_digest()
    assert d.workspace == str(tmp_workspace)
    assert d.branch == "main"
    assert d.dirty_files == []
    assert d.recent_commits and d.recent_commits[0]["subject"] == "first commit"
    assert "halt" in d.halt_contract.lower()


def test_build_digest_emits_valid_json(tmp_workspace):
    s = digest_mod.build_digest().to_json()
    parsed = json.loads(s)
    assert parsed["branch"] == "main"


def test_digest_dirty_files_listed(tmp_workspace):
    (tmp_workspace / "new.txt").write_text("x\n")
    d = digest_mod.build_digest()
    assert "new.txt" in d.dirty_files
