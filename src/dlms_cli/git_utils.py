"""Thin wrappers around git. All silent on non-repo paths."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def head_sha(cwd: Path) -> str | None:
    return _run(["rev-parse", "HEAD"], cwd)


def current_branch(cwd: Path) -> str | None:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def is_repo(cwd: Path) -> bool:
    return _run(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def changed_files_since(cwd: Path, since_sha: str | None) -> set[str] | None:
    """Repo-relative paths changed since `since_sha`, for incremental ingest.

    Returns the union of committed changes (`git diff <since>..HEAD`) and the
    current working-tree changes (`git status --porcelain`, incl. untracked) so
    edits picked up between commits are re-ingested too. Returns ``None`` when
    `since_sha` is falsy or git is unavailable — the caller treats ``None`` as
    "do a full scan" (first-run semantics), distinct from an empty set ("repo
    unchanged, nothing to do").
    """
    if not since_sha:
        return None
    files: set[str] = set()
    # `-z` gives NUL-separated, *unquoted* paths — git otherwise quotes/escapes
    # names with spaces or non-ASCII bytes (core.quotepath), which would never
    # match a real path and silently drop those files from incremental ingest.
    committed = _run(["diff", "--name-only", "-z", since_sha, "HEAD"], cwd)
    if committed is None:
        return None  # git failed / bad sha — fall back to full scan
    files.update(p for p in committed.split("\0") if p)
    dirty = _run(["status", "--porcelain", "-z"], cwd)
    if dirty is None:
        return None  # status failed — full scan beats trusting a partial set
    # porcelain -z: each record is "XY <path>"; rename/copy adds a second
    # NUL-separated field (the OLD path) which we skip — we want the new path.
    tokens = [t for t in dirty.split("\0") if t]
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        files.add(entry[3:])  # 2-char status + space, then the path
        if entry[0] in ("R", "C"):  # rename/copy: the next token is the old path
            i += 1
    return files
