"""Symbol ingester — top-level fn/class declarations → glossary atoms.

The real implementation will use tree-sitter (SPEC §12). For the
dependency-free v1 we use per-language regex over a small set of suffixes —
imprecise but deterministic and cheap. The cap (``MAX_ATOMS``) keeps the
first ingest run from blowing past its budget on large monorepos.

Liveness predicate: regex against the source file. When a symbol is
renamed or moved, the next liveness pass flips ``⚠ stale`` (SPEC §13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult, iter_files

MAX_ATOMS = 200  # per ingest run


@dataclass(frozen=True)
class _Lang:
    name: str
    suffixes: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]


_LANGUAGES: tuple[_Lang, ...] = (
    _Lang(
        name="python",
        suffixes=(".py",),
        patterns=(
            re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE),
            re.compile(r"^class\s+([A-Za-z_]\w*)\b", re.MULTILINE),
        ),
    ),
    _Lang(
        name="dart",
        suffixes=(".dart",),
        patterns=(
            re.compile(r"^(?:abstract\s+|sealed\s+)?class\s+([A-Za-z_]\w*)\b", re.MULTILINE),
            re.compile(r"^(?:mixin|enum)\s+([A-Za-z_]\w*)\b", re.MULTILINE),
        ),
    ),
    _Lang(
        name="javascript",
        suffixes=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        patterns=(
            re.compile(
                r"^export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                re.MULTILINE,
            ),
            re.compile(r"^export\s+(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
            re.compile(r"^export\s+const\s+([A-Za-z_$][\w$]*)\s*[=:]", re.MULTILINE),
        ),
    ),
    _Lang(
        name="go",
        suffixes=(".go",),
        patterns=(
            re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Z][A-Za-z0-9_]*)\s*\(", re.MULTILINE),
            re.compile(r"^type\s+([A-Z][A-Za-z0-9_]*)\s+", re.MULTILINE),
        ),
    ),
    _Lang(
        name="rust",
        suffixes=(".rs",),
        patterns=(
            re.compile(r"^pub\s+(?:async\s+)?fn\s+([a-zA-Z_]\w*)", re.MULTILINE),
            re.compile(r"^pub\s+(?:struct|enum|trait)\s+([A-Z]\w*)", re.MULTILINE),
        ),
    ),
)

_ALL_SUFFIXES: tuple[str, ...] = tuple(s for lang in _LANGUAGES for s in lang.suffixes)


def _lang_for(suffix: str) -> _Lang | None:
    s = suffix.lower()
    for lang in _LANGUAGES:
        if s in lang.suffixes:
            return lang
    return None


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="symbols")
    files = iter_files(
        ctx.root,
        suffixes=_ALL_SUFFIXES,
        exclude=ctx.exclude,
        max_kb=ctx.max_file_kb,
        changed=ctx.changed_files,
    )
    if not files:
        res.notes.append("no source files matched")
        return res

    for path in files:
        if res.atoms_inserted >= MAX_ATOMS:
            res.notes.append(f"capped at {MAX_ATOMS} atoms; remaining files skipped")
            res.truncated = True  # don't let the indexed sha advance past skipped files
            break
        lang = _lang_for(path.suffix)
        if not lang:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            res.atoms_skipped += 1
            continue
        rel = str(path.relative_to(ctx.root))
        seen: set[str] = set()
        for pat in lang.patterns:
            for m in pat.finditer(text):
                name = m.group(1)
                if name in seen:
                    continue
                seen.add(name)
                summary_10 = f"{lang.name} {name} in {Path(rel).name}"
                summary_50 = (
                    f"{lang.name} symbol `{name}` declared in {rel}. Public "
                    f"surface — referenced from {Path(rel).parent or 'root'}."
                )
                assert_fact(
                    ctx.conn,
                    type="glossary",
                    topic_key=f"symbol:{rel}:{name}",
                    summary_10w=summary_10,
                    summary_50w=summary_50,
                    source_kind="manifest",
                    source_ref=f"{rel}:0",
                    liveness=Liveness(
                        kind="regex",
                        target=rel,
                        pattern=rf"\b{re.escape(name)}\b",
                    ),
                    repo_id=ctx.repo_id,
                    workspace_id=ctx.workspace_id,
                )
                res.atoms_inserted += 1
                if res.atoms_inserted >= MAX_ATOMS:
                    break
            if res.atoms_inserted >= MAX_ATOMS:
                break
    return res
