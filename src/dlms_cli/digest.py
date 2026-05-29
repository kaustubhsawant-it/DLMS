"""SessionStart digest — bootstraps Claude Code with substrate context.

Per SPEC §6 + §7, the digest delivers ≤4K tokens at session start:
  - branch + dirty files (orientation)
  - last 3 commits (recent history)
  - top-5 atoms by weight (invariants + CLOSED decisions)
  - halt contract (the literal instruction Claude is bound by)

Emitted as JSON on stdout — Claude Code's SessionStart hook wraps it
into the bootstrap context. The shell wrapper handles file-not-found
gracefully (so `dlms init` was never run → empty injection).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import atoms as atoms_mod
from . import store
from .paths import detect_layout

# Halt contract is delivered verbatim — the literal instructions Claude is
# bound by during the session. Kept short to stay within the digest budget.
HALT_CONTRACT = (
    "At ≥85% context capacity, agentic skills enter safe-halt: write a handoff "
    "with `dlms handoff`, then stop. Read-only operations may continue but no "
    "new atom drafts. The halt is non-negotiable unless --force-continue is "
    "explicitly passed."
)


@dataclass
class Digest:
    workspace: str
    branch: str | None
    dirty_files: list[str]
    recent_commits: list[dict[str, str]]
    top_atoms: list[dict[str, Any]]
    halt_contract: str
    schema_version: int
    atom_count: int

    def to_json(self, *, pretty: bool = False) -> str:
        return json.dumps(self.__dict__, indent=2 if pretty else None)


def _git_dirty(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line[3:].strip() for line in out.stdout.splitlines() if line.strip()][:20]


def _git_branch(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root, check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_recent(root: Path, n: int = 3) -> list[dict[str, str]]:
    try:
        out = subprocess.run(
            ["git", "log", f"-n{n}", "--pretty=format:%h%x09%s%x09%an"],
            cwd=root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"sha": parts[0], "subject": parts[1], "author": parts[2]})
    return rows


def build_digest(top_k: int = 5) -> Digest:
    """Assemble the SessionStart digest."""
    layout = detect_layout()
    branch = _git_branch(layout.root)
    dirty = _git_dirty(layout.root)
    recent = _git_recent(layout.root)

    top_atoms: list[dict[str, Any]] = []
    schema_v = 0
    atom_count = 0
    if layout.db.exists():
        with store.connect(layout.db) as conn:
            schema_v = store.schema_version(conn)
            atom_count = store.atom_count(conn)
            # Headline atoms: invariants + CLOSED decisions, pinned first.
            for t in ("invariant", "decision"):
                rows = atoms_mod.query_atoms(
                    conn,
                    type=t,  # type: ignore[arg-type]
                    decision_status=("CLOSED" if t == "decision" else None),
                    order_by="confidence DESC",
                    limit=top_k,
                )
                for a in rows:
                    view = atoms_mod.get_atom(conn, a.id)
                    if view:
                        top_atoms.append({
                            "id": a.id,
                            "type": a.type,
                            "topic_key": a.topic_key,
                            "summary": view.summaries.get(50, ""),
                            "pinned": a.pinned,
                        })
                if len(top_atoms) >= top_k:
                    break
        top_atoms = sorted(top_atoms, key=lambda r: not r["pinned"])[:top_k]

    return Digest(
        workspace=str(layout.root),
        branch=branch,
        dirty_files=dirty,
        recent_commits=recent,
        top_atoms=top_atoms,
        halt_contract=HALT_CONTRACT,
        schema_version=schema_v,
        atom_count=atom_count,
    )


__all__ = ["Digest", "HALT_CONTRACT", "build_digest"]
