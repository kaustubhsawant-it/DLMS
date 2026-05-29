"""Filesystem layout helpers — workspace root discovery + canonical paths.

DLMS lives in `<workspace_root>/.dlms/` and is configured by
`<workspace_root>/dlms.toml`. The workspace root is the git toplevel when
available; otherwise the current working directory at `init` time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    root: Path           # workspace root (git toplevel or init cwd)
    config: Path         # <root>/dlms.toml
    state_dir: Path      # <root>/.dlms/
    db: Path             # <root>/.dlms/atoms.sqlite
    handoffs: Path       # <root>/.dlms/handoffs/
    jobs_log: Path       # <root>/.dlms/jobs.ndjson


def git_toplevel(start: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(out.stdout.strip())


def layout_for(root: Path) -> Layout:
    root = root.resolve()
    state = root / ".dlms"
    return Layout(
        root=root,
        config=root / "dlms.toml",
        state_dir=state,
        db=state / "atoms.sqlite",
        handoffs=state / "handoffs",
        jobs_log=state / "jobs.ndjson",
    )


def detect_layout(start: Path | None = None) -> Layout:
    """Detect the layout from CWD or `start`. Prefer git toplevel."""
    start = (start or Path.cwd()).resolve()
    root = git_toplevel(start) or start
    return layout_for(root)


def schema_sql_text() -> str:
    """Return the bundled schema.sql contents.

    Resolution order:
      1. Packaged resource (`dlms_cli/schema.sql`, when installed).
      2. Source-tree sibling (`dlms/schema.sql`, when running from a checkout).
    """
    try:
        with resources.files("dlms_cli").joinpath("schema.sql").open("r") as fh:
            return fh.read()
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    # Dev checkout: src/dlms_cli/paths.py -> parents[2] == dlms/
    fallback = Path(__file__).resolve().parents[2] / "schema.sql"
    if fallback.exists():
        return fallback.read_text()
    raise RuntimeError("schema.sql not found in package data or source tree")
