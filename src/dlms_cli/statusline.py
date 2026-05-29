"""Memory Pulse statusline (SPEC §13).

Format:
    DLMS │ 🧠 142 · ⏵Orient · 🟢→3 · ⚠ 1 stale · ✦ tick 3

  🧠 N      — live atom count
  ⏵<phase> — current OODAR phase (auto-detected, see below)
  <color>→N — atoms injected this turn (last route() call)
  ⚠ N stale — atoms with failed liveness today
  ✦ tick N — OODAR ticks completed today (commits since midnight)

The Claude Code statusline calls `dlms statusline` per assistant turn —
read-only, sub-50ms. Reads `.dlms/atoms.sqlite` and a small sidecar
`pulse.json` written by other commands to track ephemeral state (last
class injected, OODAR phase hint).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import liveness, store
from .paths import detect_layout

PULSE_FILE = "pulse.json"

PHASE_GLYPHS = {
    "Observe": "⏵Observe",
    "Orient":  "⏵Orient",
    "Decide":  "⏵Decide",
    "Act":     "⏵Act",
    "Verify":  "⏵Verify",
    "Remember": "⏵Remember",
}

CLASS_COLOR = {
    "navigational":  "🔵",
    "semantic":      "🟢",
    "contradictory": "🔴",
    "implementation": "🟣",
}


@dataclass
class Pulse:
    atoms: int
    phase: str
    last_class: str | None
    last_class_count: int
    stale: int
    ticks_today: int

    def render(self) -> str:
        parts = [f"🧠 {self.atoms}", PHASE_GLYPHS.get(self.phase, f"⏵{self.phase}")]
        if self.last_class:
            color = CLASS_COLOR.get(self.last_class, "·")
            parts.append(f"{color}→{self.last_class_count}")
        if self.stale:
            parts.append(f"⚠ {self.stale} stale")
        parts.append(f"✦ tick {self.ticks_today}")
        return "DLMS │ " + " · ".join(parts)


def _pulse_state(layout) -> dict:
    p = layout.state_dir / PULSE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_pulse(*, phase: str | None = None, last_class: str | None = None,
                last_class_count: int = 0) -> None:
    """Update the sidecar pulse.json. Called from `route`, `digest`, etc."""
    layout = detect_layout()
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    state = _pulse_state(layout)
    if phase is not None:
        state["phase"] = phase
    if last_class is not None:
        state["last_class"] = last_class
        state["last_class_count"] = last_class_count
    (layout.state_dir / PULSE_FILE).write_text(json.dumps(state))


def _ticks_today(root: Path) -> int:
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        out = subprocess.run(
            ["git", "log", f"--since={midnight.isoformat()}", "--oneline"],
            cwd=root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def build_pulse() -> Pulse:
    layout = detect_layout()
    state = _pulse_state(layout)
    atoms = 0
    stale = 0
    if layout.db.exists():
        with store.connect(layout.db) as conn:
            atoms = store.atom_count(conn)
            stale = len(liveness.stale_atoms(conn))
    return Pulse(
        atoms=atoms,
        phase=state.get("phase", "Observe"),
        last_class=state.get("last_class"),
        last_class_count=int(state.get("last_class_count", 0)),
        stale=stale,
        ticks_today=_ticks_today(layout.root),
    )


__all__ = ["CLASS_COLOR", "PHASE_GLYPHS", "Pulse", "build_pulse", "write_pulse"]
