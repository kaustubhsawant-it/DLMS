"""End-to-end tests for ingester adapters.

Each test builds a tiny throwaway workspace in `tmp_path`, runs one adapter
against an in-memory atom store, and asserts the atoms it produced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from dlms_cli import atoms as atoms_mod
from dlms_cli import ingesters
from dlms_cli.ingesters.base import IngestContext


def _ctx(conn, root: Path, *, last_sha: str | None = None) -> IngestContext:
    return IngestContext(
        conn=conn,
        root=root,
        repo_id="repo_test",
        workspace_id="default",
        last_indexed_sha=last_sha,
        exclude=["node_modules", ".venv", "build", "dist"],
        max_file_kb=512,
    )


# ---------------------------------------------------------------------------
# readme
# ---------------------------------------------------------------------------

def test_readme_emits_glossary_atom(conn, tmp_path):
    (tmp_path / "README.md").write_text(
        "# Acme Toolkit\n\nA tiny CLI that does very specific things.\n"
        "Multi-line intro continues here.\n\n## Install\n\nrun stuff\n"
    )
    res = ingesters.readme.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 1
    rows = atoms_mod.query_atoms(conn, type="glossary")
    assert len(rows) == 1
    view = atoms_mod.get_atom(conn, rows[0].id)
    assert "Acme Toolkit" in view.summaries[10]
    assert view.atom.liveness_kind == "regex"


def test_readme_picks_up_multiple_docs(conn, tmp_path):
    (tmp_path / "README.md").write_text("# A\n\nfirst doc.\n")
    (tmp_path / "CLAUDE.md").write_text("# B\n\nsecond doc.\n")
    res = ingesters.readme.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 2


def test_readme_handles_missing(conn, tmp_path):
    res = ingesters.readme.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 0
    assert any("no root docs" in n for n in res.notes)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def test_manifest_pyproject_emits_runtime_and_deps(conn, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "x"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["typer>=0.12", "rich"]\n'
        '[project.scripts]\n'
        'x = "x.cli:main"\n'
    )
    res = ingesters.manifest.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted >= 4  # runtime + 2 deps + 1 script
    deps = atoms_mod.query_atoms(conn, type="dependency")
    names = {d.topic_key for d in deps}
    assert "dep:pypi:typer" in names
    assert "dep:pypi:rich" in names
    runtime = atoms_mod.query_atoms(conn, type="runtime")
    assert any(r.topic_key == "runtime:python" for r in runtime)
    recipes = atoms_mod.query_atoms(conn, type="build_recipe")
    assert any(r.topic_key == "script:pypi:x" for r in recipes)


def test_manifest_package_json(conn, tmp_path):
    (tmp_path / "package.json").write_text(
        '{"engines":{"node":">=18"},'
        '"dependencies":{"react":"^18.0.0"},'
        '"devDependencies":{"vitest":"^1.0"},'
        '"scripts":{"build":"vite build"}}'
    )
    res = ingesters.manifest.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted >= 4
    names = {a.topic_key for a in atoms_mod.query_atoms(conn, type="dependency")}
    assert "dep:npm:react" in names
    assert "dep:npm:vitest" in names


def test_manifest_no_files(conn, tmp_path):
    res = ingesters.manifest.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 0
    assert any("no manifests" in n for n in res.notes)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_schema_extracts_create_table(conn, tmp_path):
    (tmp_path / "schema.sql").write_text(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY,
          email TEXT NOT NULL,
          created_at INTEGER
        );

        CREATE TABLE posts (
          id INTEGER PRIMARY KEY,
          author_id INTEGER REFERENCES users(id),
          body TEXT
        );
        """
    )
    res = ingesters.schema_sql.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 2
    facts = atoms_mod.query_atoms(conn, type="schema_fact")
    topics = {f.topic_key for f in facts}
    assert topics == {"table:users", "table:posts"}


def test_schema_skips_nonexistent(conn, tmp_path):
    res = ingesters.schema_sql.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 0
    assert any("no .sql" in n for n in res.notes)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git_init_with_commits(root: Path, n: int = 2) -> list[str]:
    def g(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    shas: list[str] = []
    for i in range(n):
        (root / f"f{i}.txt").write_text(f"hello {i}\n")
        g("add", "-A")
        g("commit", "-q", "-m", f"add f{i}")
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        )
        shas.append(out.stdout.strip())
    return shas


def test_git_emits_decision_per_commit(conn, tmp_path):
    _git_init_with_commits(tmp_path, n=3)
    res = ingesters.git_commits.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 3
    decisions = atoms_mod.query_atoms(conn, type="decision")
    assert len(decisions) == 3
    assert all(d.decision_status == "CLOSED" for d in decisions)


def test_git_incremental_with_last_sha(conn, tmp_path):
    shas = _git_init_with_commits(tmp_path, n=3)
    # Pretend we already indexed up to first commit — expect 2 new atoms.
    res = ingesters.git_commits.run(_ctx(conn, tmp_path, last_sha=shas[0]))
    assert res.atoms_inserted == 2


def test_git_skips_non_repo(conn, tmp_path):
    res = ingesters.git_commits.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 0
    assert any("not a git repository" in n for n in res.notes)


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------

def test_symbols_python(conn, tmp_path):
    (tmp_path / "mod.py").write_text(
        "def foo():\n    return 1\n\n"
        "class Bar:\n    pass\n\n"
        "async def baz():\n    return 2\n"
    )
    res = ingesters.symbols.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 3
    names = {a.topic_key.split(":")[-1] for a in atoms_mod.query_atoms(conn, type="glossary")}
    assert names == {"foo", "Bar", "baz"}


def test_symbols_multilanguage(conn, tmp_path):
    (tmp_path / "main.go").write_text("package m\n\nfunc DoThing() {}\n\ntype Widget struct{}\n")
    (tmp_path / "a.dart").write_text("class Alpha {}\nmixin Beta {}\n")
    res = ingesters.symbols.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 4


def test_symbols_respects_exclude(conn, tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("export function inner() {}\n")
    (tmp_path / "app.js").write_text("export function outer() {}\n")
    res = ingesters.symbols.run(_ctx(conn, tmp_path))
    assert res.atoms_inserted == 1
    rows = atoms_mod.query_atoms(conn, type="glossary")
    assert "outer" in rows[0].topic_key


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def test_run_orchestrator_handles_unknown_name(conn, tmp_path):
    res = ingesters.run(_ctx(conn, tmp_path), enabled=["nope"])
    assert "nope" in res
    assert res["nope"].skipped and "unknown" in res["nope"].skipped


def test_run_orchestrator_full_sweep(conn, tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n\nshort intro.\n")
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id INTEGER);\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nrequires-python=">=3.11"\ndependencies=["typer"]\n'
    )
    (tmp_path / "main.py").write_text("def hi():\n    return 1\n")
    res = ingesters.run(_ctx(conn, tmp_path), enabled=["readme", "manifest", "schema", "symbols"])
    assert res["readme"].ok and res["readme"].atoms_inserted == 1
    assert res["schema"].ok and res["schema"].atoms_inserted == 1
    assert res["manifest"].ok and res["manifest"].atoms_inserted >= 2
    assert res["symbols"].ok and res["symbols"].atoms_inserted == 1
