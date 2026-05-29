"""Shared types for ingester adapters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IngestContext:
    """Per-run input handed to each ingester.

    `conn` is the open atom store. `root` is the workspace root to scan.
    `repo_id` and `workspace_id` flow through to atom rows for scoping.
    `last_indexed_sha` lets stateful adapters (git) read incrementally.
    `exclude` is the directory-name blocklist from dlms.toml.
    `max_file_kb` skips oversized files (per SPEC §scan).
    """

    conn: sqlite3.Connection
    root: Path
    repo_id: str
    workspace_id: str = "default"
    last_indexed_sha: str | None = None
    exclude: list[str] = field(default_factory=list)
    max_file_kb: int = 512
    # Incremental ingest (SPEC §14.2): repo-relative paths changed since the
    # last index. None = full scan (first run); a set = only these files.
    changed_files: set[str] | None = None

    def is_changed(self, rel_path: str) -> bool:
        """True if `rel_path` should be (re)ingested this run.

        Full-scan runs (changed_files is None) accept everything; incremental
        runs accept only paths in the changed set.
        """
        return self.changed_files is None or rel_path in self.changed_files


@dataclass
class IngestResult:
    """Per-adapter outcome surfaced in the CLI table."""

    name: str
    atoms_inserted: int = 0
    atoms_skipped: int = 0
    notes: list[str] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None
    # True when the adapter did NOT process everything it should have (budget
    # ran out, or an internal cap was hit). Used to gate the indexed-sha
    # advance so a truncated run is retried, not silently orphaned (§14.2).
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None

    def status_label(self) -> str:
        if self.error:
            return f"err: {self.error}"
        if self.skipped:
            return f"skip: {self.skipped}"
        return "ok"


def iter_files(
    root: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    names: tuple[str, ...] | None = None,
    exclude: list[str],
    max_kb: int,
    changed: set[str] | None = None,
) -> list[Path]:
    """Walk `root` collecting files by suffix or basename, honoring exclude dirs.

    `exclude` matches any path component literally (e.g., 'node_modules').
    Files larger than `max_kb` kilobytes are skipped to keep ingest cheap.
    When `changed` is given (incremental ingest, SPEC §14.2) we iterate exactly
    those repo-relative paths instead of walking the whole tree — keeping cost
    O(changed) rather than O(repo).
    """
    excl = set(exclude)
    cap = max_kb * 1024
    suffix_set = {s.lower() for s in suffixes} if suffixes else None
    name_set = set(names) if names else None
    candidates: Iterable[Path] = (
        (root / rel for rel in sorted(changed))
        if changed is not None
        else root.rglob("*")
    )
    out: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in excl for part in path.parts):
            continue
        matches_suffix = suffix_set is not None and path.suffix.lower() in suffix_set
        matches_name = name_set is not None and path.name in name_set
        if suffix_set is None and name_set is None:
            pass  # accept everything
        elif not (matches_suffix or matches_name):
            continue
        try:
            if path.stat().st_size > cap:
                continue
        except OSError:
            continue
        out.append(path)
    return out
