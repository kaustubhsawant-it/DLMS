from __future__ import annotations

import subprocess

import pytest

from dlms_cli import handoff as handoff_mod


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def g(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    (tmp_path / "f.txt").write_text("hi\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_render_emits_frontmatter(workspace):
    inp = handoff_mod.HandoffInput(
        current_tick_title="hello",
        next_action="ship task #42",
        do_not_redo=["mock the database"],
    )
    doc = handoff_mod.render(inp)
    assert doc.startswith("---\n")
    assert "ship task #42" in doc
    assert "mock the database" in doc
    assert doc.count("---") >= 2  # opening + closing


def test_write_creates_file_and_latest(workspace):
    inp = handoff_mod.HandoffInput(
        current_tick_title="t", next_action="x", narrative="short."
    )
    path = handoff_mod.write(inp)
    assert path.exists()
    latest = path.parent / "LATEST.md"
    assert latest.exists()


def test_narrative_truncated_at_cap(workspace):
    long = ". ".join(["sentence " + "x" * 20] * 200)
    inp = handoff_mod.HandoffInput(narrative=long, next_action="x")
    doc = handoff_mod.render(inp)
    body = doc.split("## Narrative", 1)[1]
    assert handoff_mod._approx_tokens(body) <= handoff_mod.NARRATIVE_TOKEN_CAP + 50


def test_yaml_serializer_quotes_special_chars(workspace):
    inp = handoff_mod.HandoffInput(
        next_action="rename: foo to bar",  # contains :
        narrative="ok",
    )
    doc = handoff_mod.render(inp)
    assert '"rename: foo to bar"' in doc
