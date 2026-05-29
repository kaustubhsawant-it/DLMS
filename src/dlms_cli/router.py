"""UserPromptSubmit hook — classify prompt, retrieve atoms, cap injection.

Heuristic v1 classifier (SPEC §13 color codes):

  navigational  — "where is X", "show me Y", "what file"
  semantic      — "why does", "is it safe", "how does X interact"
  contradictory — "but we said", "didn't we decide", "isn't X already"
  implementation — "add", "fix", "refactor", "remove", "rename"

The class drives retrieval shape:
  navigational  → ≤ 3 atoms, summaries at 10w resolution
  semantic      → ≤ 5 atoms, summaries at 50w
  contradictory → ≤ 7 atoms, prefer atoms in `history()` that share topic
  implementation → ≤ 5 atoms + 2-hop MIRRORS neighborhood (SPEC §11 seed)

Output is a JSON block written to stdout in the SessionStart-style format
Claude Code's UserPromptSubmit hook ingests. Token cap is enforced —
SPEC §7 requires ≤3K tokens per turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from . import atoms as atoms_mod
from . import retrieval, store
from .paths import detect_layout

TURN_TOKEN_CAP = 3000

_NAV_RE = re.compile(
    r"\b(where (is|do|does)|show me|which file|find|locate|grep|"
    r"path to|file for)\b", re.IGNORECASE,
)
_SEM_RE = re.compile(
    r"\b(why (does|is|do|are)|how does|is it safe|what happens|"
    r"is .* allowed|explain)\b", re.IGNORECASE,
)
_CONTRA_RE = re.compile(
    r"\b(but (we|i) (said|decided)|didn'?t we|isn'?t .* already|"
    r"thought (we|that)|haven'?t we)\b", re.IGNORECASE,
)
_IMPL_RE = re.compile(
    r"\b(add|fix|refactor|remove|rename|implement|build|wire|"
    r"replace|migrate|update|patch|tweak)\b", re.IGNORECASE,
)

CLASSES = ("navigational", "semantic", "contradictory", "implementation")


@dataclass
class Routed:
    classified_as: str
    confidence: float
    atoms: list[dict[str, Any]]
    injected_tokens: int

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


def classify(prompt: str) -> tuple[str, float]:
    """Return (class, confidence). Defaults to 'semantic' at 0.3."""
    # Priority: contradictory > implementation > navigational > semantic.
    # Contradictory takes precedence because if Claude is about to argue
    # with a prior decision, surfacing the closed atom is highest-value.
    if _CONTRA_RE.search(prompt):
        return "contradictory", 0.9
    if _IMPL_RE.search(prompt):
        return "implementation", 0.7
    if _NAV_RE.search(prompt):
        return "navigational", 0.8
    if _SEM_RE.search(prompt):
        return "semantic", 0.6
    return "semantic", 0.3


def _atom_payload(view: atoms_mod.AtomView, resolution: int) -> dict[str, Any]:
    return {
        "id": view.atom.id,
        "type": view.atom.type,
        "topic_key": view.atom.topic_key,
        "decision_status": view.atom.decision_status,
        "summary": view.summaries.get(resolution) or view.summaries.get(50, ""),
        "source_ref": view.atom.source_ref,
    }


def _approx_tokens(s: str) -> int:
    return max(1, (len(s) + 3) // 4)


def route(prompt: str, file_context: list[str] | None = None) -> Routed:
    """Classify the prompt, retrieve atoms, return token-capped payload."""
    cls, conf = classify(prompt)
    layout = detect_layout()

    n, resolution = {
        "navigational":   (3, 10),
        "semantic":       (5, 50),
        "contradictory":  (7, 50),
        "implementation": (5, 50),
    }[cls]

    if not layout.db.exists():
        return Routed(classified_as=cls, confidence=conf, atoms=[], injected_tokens=0)

    atoms_out: list[dict[str, Any]] = []
    tokens = 0
    with store.connect(layout.db) as conn:
        hits = retrieval.retrieve(
            conn, query=prompt, file_context=file_context, top_n=n,
        )
        for h in hits:
            view = atoms_mod.get_atom(conn, h.atom_id)
            if not view:
                continue
            payload = _atom_payload(view, resolution)
            payload["trail"] = h.trail()
            row_tokens = _approx_tokens(payload["summary"])
            if tokens + row_tokens > TURN_TOKEN_CAP:
                break
            tokens += row_tokens
            atoms_out.append(payload)

        # Audit log so we can train a real classifier later (SPEC §9).
        conn.execute(
            """INSERT INTO retrieval_log (query, classified_as, atoms_returned, injected_tokens)
               VALUES (?, ?, ?, ?)""",
            (
                prompt[:500],
                cls,
                json.dumps([a["id"] for a in atoms_out]),
                tokens,
            ),
        )
        conn.commit()

    return Routed(classified_as=cls, confidence=conf, atoms=atoms_out, injected_tokens=tokens)


__all__ = ["CLASSES", "Routed", "TURN_TOKEN_CAP", "classify", "route"]
