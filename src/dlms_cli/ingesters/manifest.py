"""Manifest ingester — language-specific package manifests → atoms.

Emits three flavors of atom:

* ``dependency`` — one per declared package (topic_key ``dep:<ecosystem>:<name>``)
* ``runtime``    — language version pin from the manifest (e.g. requires-python)
* ``build_recipe`` — declared scripts (npm scripts, pyproject.scripts)

Stdlib-only: parsing is intentionally minimal so the toolchain stays small.
We trade some precision (e.g. we don't resolve workspace-style monorepos)
for portability across projects.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="manifest")
    handlers: list[tuple[str, Callable[[IngestContext, Path, IngestResult], None]]] = [
        ("pyproject.toml", _pyproject),
        ("package.json", _package_json),
        ("pubspec.yaml", _pubspec_yaml),
        ("Cargo.toml", _cargo_toml),
        ("requirements.txt", _requirements_txt),
        ("go.mod", _go_mod),
    ]
    found = False
    for name, fn in handlers:
        path = ctx.root / name
        if not path.is_file():
            continue
        found = True
        if not ctx.is_changed(name):  # incremental: skip manifests that didn't change
            continue
        try:
            fn(ctx, path, res)
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"{name}: {type(exc).__name__}: {exc}")
    if not found:
        res.notes.append("no manifests at root")
    return res


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _emit_dep(
    ctx: IngestContext, *, ecosystem: str, name: str, spec: str, manifest_rel: str
) -> None:
    assert_fact(
        ctx.conn,
        type="dependency",
        topic_key=f"dep:{ecosystem}:{name}",
        summary_10w=f"{ecosystem} dep {name}",
        summary_50w=f"{ecosystem} dependency {name} pinned at {spec} (from {manifest_rel}).",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=rf"\b{re.escape(name)}\b"
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )


def _emit_runtime(
    ctx: IngestContext, *, lang: str, version: str, manifest_rel: str
) -> None:
    assert_fact(
        ctx.conn,
        type="runtime",
        topic_key=f"runtime:{lang}",
        summary_10w=f"{lang} {version}",
        summary_50w=f"{lang} runtime constraint: {version} (declared in {manifest_rel}).",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=re.escape(version.strip(" \"'"))
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )


def _emit_script(
    ctx: IngestContext, *, ecosystem: str, name: str, body: str, manifest_rel: str
) -> None:
    assert_fact(
        ctx.conn,
        type="build_recipe",
        topic_key=f"script:{ecosystem}:{name}",
        summary_10w=f"{ecosystem} script {name}",
        summary_50w=f"{ecosystem} script `{name}` runs: {body}",
        source_kind="manifest",
        source_ref=manifest_rel,
        liveness=Liveness(
            kind="regex", target=manifest_rel, pattern=rf"\b{re.escape(name)}\b"
        ),
        repo_id=ctx.repo_id,
        workspace_id=ctx.workspace_id,
    )


# ---------------------------------------------------------------------------
# per-format parsers
# ---------------------------------------------------------------------------

def _pyproject(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    project = data.get("project", {}) or {}
    py = project.get("requires-python")
    if py:
        _emit_runtime(ctx, lang="python", version=str(py), manifest_rel=rel)
        res.atoms_inserted += 1
    for dep in project.get("dependencies", []) or []:
        name, spec = _split_pep508(str(dep))
        _emit_dep(ctx, ecosystem="pypi", name=name, spec=spec or "*", manifest_rel=rel)
        res.atoms_inserted += 1
    optional = project.get("optional-dependencies", {}) or {}
    for extra, deps in optional.items():
        for dep in deps:
            name, spec = _split_pep508(str(dep))
            _emit_dep(
                ctx, ecosystem="pypi", name=name,
                spec=f"{spec or '*'} (extra:{extra})", manifest_rel=rel,
            )
            res.atoms_inserted += 1
    for script, target in (project.get("scripts", {}) or {}).items():
        _emit_script(
            ctx, ecosystem="pypi", name=script, body=str(target), manifest_rel=rel,
        )
        res.atoms_inserted += 1


_PEP508_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(.*)$")


def _split_pep508(spec: str) -> tuple[str, str]:
    m = _PEP508_RE.match(spec)
    if not m:
        return spec, ""
    name = m.group(1)
    rest = m.group(2).strip()
    return name, rest


def _package_json(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    engines = (data.get("engines") or {})
    node_ver = engines.get("node")
    if node_ver:
        _emit_runtime(ctx, lang="node", version=str(node_ver), manifest_rel=rel)
        res.atoms_inserted += 1
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            label = f"{spec}" if section == "dependencies" else f"{spec} ({section})"
            _emit_dep(ctx, ecosystem="npm", name=name, spec=label, manifest_rel=rel)
            res.atoms_inserted += 1
    for name, body in (data.get("scripts") or {}).items():
        _emit_script(
            ctx, ecosystem="npm", name=name, body=str(body), manifest_rel=rel,
        )
        res.atoms_inserted += 1


_YAML_DEP_RE = re.compile(r"^\s{2}([A-Za-z0-9_]+):\s*(.+)$")


def _pubspec_yaml(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    text = path.read_text(encoding="utf-8", errors="replace")
    # Very narrow YAML: only catches `name: spec` lines under `dependencies:`/
    # `dev_dependencies:` sections. Nested map specs (git/path) are kept as
    # the literal text of the first line — good enough for liveness pinning.
    section: str | None = None
    for line in text.splitlines():
        s = line.rstrip()
        if not s or s.startswith("#"):
            continue
        if not line.startswith(" "):
            head = s.split(":", 1)[0]
            section = head if head in {"dependencies", "dev_dependencies"} else None
            if head == "environment":
                section = "environment"
            continue
        if section in {"dependencies", "dev_dependencies"}:
            m = _YAML_DEP_RE.match(line)
            if m:
                _emit_dep(
                    ctx, ecosystem="pub", name=m.group(1),
                    spec=m.group(2).strip() or "*", manifest_rel=rel,
                )
                res.atoms_inserted += 1
        elif section == "environment":
            m = _YAML_DEP_RE.match(line)
            if m and m.group(1) in {"sdk", "flutter"}:
                _emit_runtime(
                    ctx, lang=("dart" if m.group(1) == "sdk" else "flutter"),
                    version=m.group(2).strip(), manifest_rel=rel,
                )
                res.atoms_inserted += 1


def _cargo_toml(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    pkg = data.get("package", {}) or {}
    edition = pkg.get("edition") or pkg.get("rust-version")
    if edition:
        _emit_runtime(ctx, lang="rust", version=str(edition), manifest_rel=rel)
        res.atoms_inserted += 1
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (data.get(section) or {}).items():
            label = spec if isinstance(spec, str) else json.dumps(spec, sort_keys=True)
            if section != "dependencies":
                label = f"{label} ({section})"
            _emit_dep(ctx, ecosystem="cargo", name=name, spec=str(label), manifest_rel=rel)
            res.atoms_inserted += 1


_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(.*)$")


def _requirements_txt(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        _emit_dep(
            ctx, ecosystem="pypi", name=m.group(1),
            spec=m.group(2).strip() or "*", manifest_rel=rel,
        )
        res.atoms_inserted += 1


_GO_DEP_RE = re.compile(r"^\s*([\w./\-]+)\s+(v\S+)")


def _go_mod(ctx: IngestContext, path: Path, res: IngestResult) -> None:
    rel = str(path.relative_to(ctx.root))
    in_require = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith("go "):
            _emit_runtime(ctx, lang="go", version=s[3:].strip(), manifest_rel=rel)
            res.atoms_inserted += 1
            continue
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require and s == ")":
            in_require = False
            continue
        target = s if in_require else (s[len("require "):] if s.startswith("require ") else "")
        if not target:
            continue
        m = _GO_DEP_RE.match(target)
        if m:
            _emit_dep(
                ctx, ecosystem="gomod", name=m.group(1),
                spec=m.group(2), manifest_rel=rel,
            )
            res.atoms_inserted += 1
