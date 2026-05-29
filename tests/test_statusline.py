from __future__ import annotations

from dlms_cli import statusline


def test_pulse_renders_minimal():
    p = statusline.Pulse(
        atoms=42, phase="Orient", last_class=None, last_class_count=0,
        stale=0, ticks_today=0,
    )
    line = p.render()
    assert "🧠 42" in line
    assert "⏵Orient" in line
    assert "✦ tick 0" in line
    assert "stale" not in line


def test_pulse_with_class_and_stale():
    p = statusline.Pulse(
        atoms=10, phase="Act", last_class="implementation",
        last_class_count=3, stale=2, ticks_today=5,
    )
    line = p.render()
    assert "🟣→3" in line
    assert "⚠ 2 stale" in line


def test_write_and_read_pulse_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dlms").mkdir()
    statusline.write_pulse(phase="Verify", last_class="semantic", last_class_count=4)
    p = statusline.build_pulse()
    assert p.phase == "Verify"
    assert p.last_class == "semantic"
    assert p.last_class_count == 4
