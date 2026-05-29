"""PreCompact handoff generator — anti-hallucination resumption (SPEC §9).

Triggers:
  - PreCompact hook (≥80% context)
  - Skill clean exit (`tick_complete`)
  - User invokes `/dlms:handoff`
  - Halt-safety (≥85% capacity)
  - Stop hook with dirty files and no handoff this session

Storage:
  `.dlms/handoffs/<UTC-ts>-<session_id>.md` — committed (tribal knowledge)
  `.dlms/handoffs/LATEST.md`                 — symlink, gitignored

Schema: YAML frontmatter (SPEC §9) + narrative section, capped at 400
tokens. The narrative lints out anything regenerable from `git log -p`
or an atom ID — keeping context-window cost down on resumption.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import detect_layout

NARRATIVE_TOKEN_CAP = 400


@dataclass
class HandoffInput:
    """Caller-supplied fields. Everything else is auto-detected."""
    current_tick_id: str | None = None
    current_tick_title: str | None = None
    current_tick_status: str = "in_progress"
    current_tick_phase: str = "Act"
    context_pct_at_end: float | None = None
    ended_reason: str = "user_handoff"
    atoms_consulted: list[str] = field(default_factory=list)
    atoms_drafted: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_action: str = ""
    failed_assumptions: list[str] = field(default_factory=list)
    do_not_redo: list[str] = field(default_factory=list)
    edge_cases_pending: list[str] = field(default_factory=list)
    narrative: str = ""
    session_id: str | None = None


def _git_state(root: Path) -> dict[str, Any]:
    def g(*args) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args], cwd=root, check=True, capture_output=True, text=True
            )
            return r.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    branch = g("rev-parse", "--abbrev-ref", "HEAD")
    sha = g("rev-parse", "HEAD")
    status = g("status", "--porcelain") or ""
    dirty = [line[3:].strip() for line in status.splitlines() if line.strip()][:30]
    return {"branch": branch, "last_commit_sha": sha, "dirty_files": dirty}


def _approx_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _truncate_narrative(text: str, cap: int = NARRATIVE_TOKEN_CAP) -> str:
    """Crude truncation: cut at last sentence boundary before the cap."""
    if _approx_tokens(text) <= cap:
        return text
    target_chars = cap * 4
    head = text[:target_chars]
    cut = max(head.rfind(". "), head.rfind("\n\n"))
    return head[: cut + 1] if cut > 0 else head


def _format_yaml_value(value: Any, indent: int = 0) -> str:
    """Tiny YAML emitter — only what the schema needs (no anchors, no flow)."""
    pad = "  " * indent
    if isinstance(value, str):
        if value and ("\n" in value or value.startswith(" ") or value.endswith(" ")):
            return "|\n" + "\n".join(f"{pad}  {line}" for line in value.splitlines())
        if value == "" or re.search(r"[:#&*\[\]{},]", value):
            return json.dumps(value)
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        out = []
        for item in value:
            if isinstance(item, dict):
                lines = []
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    lines.append(f"{prefix}{k}: {_format_yaml_value(v, indent + 1)}")
                    first = False
                out.append("\n".join(lines))
            else:
                out.append(f"{pad}- {_format_yaml_value(item, indent + 1)}")
        return "\n" + "\n".join(out)
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            lines.append(f"{pad}{k}: {_format_yaml_value(v, indent + 1)}")
        return "\n" + "\n".join(lines)
    return json.dumps(value)


def render(handoff: HandoffInput, *, started_at: dt.datetime | None = None) -> str:
    """Render frontmatter + narrative. Pure function — does no I/O."""
    layout = detect_layout()
    git = _git_state(layout.root)
    now = dt.datetime.now(dt.UTC)
    started = started_at or now
    session_id = handoff.session_id or f"sess-{uuid.uuid4().hex[:8]}"

    fields: dict[str, Any] = {
        "session_id": session_id,
        "started_at": started.strftime("%Y-%m-%dT%H:%MZ"),
        "ended_at":   now.strftime("%Y-%m-%dT%H:%MZ"),
        "ended_reason": handoff.ended_reason,
        "context_pct_at_end": handoff.context_pct_at_end,
        "branch": git["branch"],
        "last_commit_sha": git["last_commit_sha"][:7] if git["last_commit_sha"] else None,
        "dirty_files": git["dirty_files"],
        "current_tick": {
            "id": handoff.current_tick_id or session_id,
            "title": handoff.current_tick_title or "(untitled)",
            "status": handoff.current_tick_status,
            "phase": handoff.current_tick_phase,
        },
        "atoms_consulted": handoff.atoms_consulted,
        "atoms_drafted": handoff.atoms_drafted,
        "open_questions": handoff.open_questions,
        "next_action": handoff.next_action or "(no next action specified)",
        "failed_assumptions": handoff.failed_assumptions,
        "do_not_redo": handoff.do_not_redo,
        "edge_cases_pending": handoff.edge_cases_pending,
    }
    yaml_body = "\n".join(
        f"{k}: {_format_yaml_value(v)}" for k, v in fields.items()
    )
    narrative = _truncate_narrative(handoff.narrative or "(no narrative supplied)")
    return f"---\n{yaml_body}\n---\n\n## Narrative\n\n{narrative}\n"


def write(handoff: HandoffInput, *, started_at: dt.datetime | None = None) -> Path:
    """Render + write the handoff. Returns the file path. Refreshes LATEST.md."""
    layout = detect_layout()
    layout.handoffs.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    session_id = handoff.session_id or f"sess-{uuid.uuid4().hex[:8]}"
    handoff.session_id = session_id
    filename = f"{now.strftime('%Y-%m-%dT%H%MZ')}-{session_id}.md"
    path = layout.handoffs / filename
    path.write_text(render(handoff, started_at=started_at))

    latest = layout.handoffs / "LATEST.md"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    try:
        os.symlink(filename, latest)
    except OSError:
        # Symlinks unavailable (Windows without privilege) — fall back to copy.
        latest.write_text(path.read_text())
    return path


__all__ = ["HandoffInput", "NARRATIVE_TOKEN_CAP", "render", "write"]
