"""Liveness predicate runner (SPEC §3).

Every atom may carry a liveness predicate: a small machine-checkable assertion
that the world *still* matches the claim. When the predicate fails, the atom
is "stale" — surfaced in the Memory Pulse statusline (`⚠ N stale`).

Task #5 scope: `regex` (file-content grep) and `none` (trivially passes).
Future tasks add:
  - `ast`   — tree-sitter query against parsed file
  - `sql`   — query against the local sqlite store (for self-referential facts)

On a passing check, `liveness_last_ok` is bumped to now. Failures don't write;
freshness is inferred from absence (predicate ran today, last_ok < today → stale).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LivenessResult:
    atom_id: str
    kind: str
    ok: bool
    reason: str         # human-readable
    checked_at: int


def check_atom(
    conn: sqlite3.Connection,
    atom_id: str,
    *,
    repo_root: Path,
) -> LivenessResult:
    """Run the predicate attached to `atom_id`. Updates `liveness_last_ok` on
    pass. Raises `KeyError` if the atom doesn't exist."""
    row = conn.execute(
        """SELECT liveness_kind, liveness_target, liveness_pattern
             FROM atoms WHERE id = ?""",
        (atom_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"atom not found: {atom_id}")

    kind = row["liveness_kind"]
    target = row["liveness_target"]
    pattern = row["liveness_pattern"]
    now = int(time.time())

    if kind is None or kind == "none":
        # No predicate — trivially "live". Don't bump last_ok (nothing checked).
        return LivenessResult(atom_id, "none", True, "no predicate", now)

    if kind == "regex":
        ok, reason = _check_regex(repo_root, target, pattern)
    elif kind == "ast":
        raise NotImplementedError("ast liveness — see future task (tree-sitter)")
    elif kind == "sql":
        raise NotImplementedError("sql liveness — see future task (self-query)")
    else:
        raise ValueError(f"unknown liveness kind: {kind}")

    if ok:
        with conn:
            conn.execute(
                "UPDATE atoms SET liveness_last_ok = ? WHERE id = ?",
                (now, atom_id),
            )
    return LivenessResult(atom_id, kind, ok, reason, now)


def _check_regex(
    repo_root: Path, target: str | None, pattern: str | None
) -> tuple[bool, str]:
    if not target or not pattern:
        return False, "regex liveness missing target or pattern"

    # Resolve target relative to repo_root, with traversal protection.
    full = (repo_root / target).resolve()
    try:
        full.relative_to(repo_root.resolve())
    except ValueError:
        return False, f"target {target!r} escapes repo root"

    if not full.exists():
        return False, f"target file missing: {target}"
    try:
        text = full.read_text(errors="replace")
    except OSError as e:
        return False, f"cannot read {target}: {e}"

    try:
        if re.search(pattern, text, re.MULTILINE):
            return True, "regex matched"
        return False, "regex did not match"
    except re.error as e:
        return False, f"invalid regex {pattern!r}: {e}"


@dataclass(frozen=True)
class SweepResult:
    checked: int           # predicates actually re-run this pass
    passed: int            # still live
    failed: list[str]      # atom_ids whose predicate failed (or errored)


def revalidate_sweep(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    limit: int = 50,
    threshold_seconds: int = 86_400,
) -> SweepResult:
    """Background incremental revalidation (SPEC §14.5).

    Re-checks only the `limit` *stalest* atoms (oldest `liveness_last_ok` first)
    whose predicate hasn't passed inside the threshold window — never an eager
    global scan over the whole store. `check_atom` bumps `liveness_last_ok` on
    pass, so repeated sweeps walk forward through the staleness frontier. An
    un-runnable predicate (ast/sql not yet implemented) counts as failed for
    this pass so it surfaces rather than silently lingering."""
    candidates = stale_atoms(
        conn, threshold_seconds=threshold_seconds, limit=limit
    )
    passed = 0
    failed: list[str] = []
    for row in candidates:
        try:
            result = check_atom(conn, row["id"], repo_root=repo_root)
        except (KeyError, NotImplementedError, ValueError):
            failed.append(row["id"])
            continue
        if result.ok:
            passed += 1
        else:
            failed.append(row["id"])
    return SweepResult(checked=len(candidates), passed=passed, failed=failed)


def stale_atoms(
    conn: sqlite3.Connection,
    *,
    threshold_seconds: int = 86_400,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Atoms whose liveness predicate exists but hasn't passed in the threshold
    window (default 24h). Powers the Pulse `⚠ N stale` indicator. When `limit`
    is set the bound is pushed into SQL (stalest first), so a background sweep
    never materializes the whole staleness set — no eager global scan."""
    cutoff = int(time.time()) - threshold_seconds
    sql = """SELECT id, type, topic_key, liveness_kind, liveness_last_ok
               FROM live_atoms
              WHERE liveness_kind IS NOT NULL
                AND liveness_kind != 'none'
                AND (liveness_last_ok IS NULL OR liveness_last_ok < ?)
              ORDER BY COALESCE(liveness_last_ok, 0) ASC"""
    params: tuple = (cutoff,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (cutoff, limit)
    return list(conn.execute(sql, params))
