from __future__ import annotations

import pytest

from dlms_cli import router


@pytest.mark.parametrize("prompt,expected", [
    ("where is the auth handler?", "navigational"),
    ("show me the schema for users", "navigational"),
    ("why does this race when we hit logout", "semantic"),
    ("is it safe to drop this column", "semantic"),
    ("but we decided not to add bcrypt", "contradictory"),
    ("didn't we already close that?", "contradictory"),
    ("fix the avatar upload bug", "implementation"),
    ("refactor auth_handler", "implementation"),
    ("hello there", "semantic"),  # default
])
def test_classifier(prompt, expected):
    cls, conf = router.classify(prompt)
    assert cls == expected
    assert 0 < conf <= 1


def test_route_returns_empty_when_no_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = router.route("where is X")
    assert out.atoms == []
    assert out.classified_as == "navigational"
