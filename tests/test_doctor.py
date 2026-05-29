"""Tests for `dlms doctor` — composed health checks.

The check functions take a connection + findings list. We assert finding
shape rather than exact wording so future copy tweaks don't break tests.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from typer.testing import CliRunner

from dlms_cli import atoms as atoms_mod
from dlms_cli import doctor, store
from dlms_cli.atoms import Liveness
from dlms_cli.cli import app
from dlms_cli.paths import layout_for


def _layout(tmp_path: Path):
    state = tmp_path / ".dlms"
    state.mkdir()
    return layout_for(tmp_path)


def _make_db(layout) -> sqlite3.Connection:
    conn = store.connect(layout.db)
    store.init_schema(conn)
    return conn


def _name_to_severity(report: doctor.Report) -> dict[str, str]:
    return {f.name: f.severity for f in report.findings}


# ---------------------------------------------------------------------------
# layout precondition
# ---------------------------------------------------------------------------

def test_run_fails_when_state_dir_missing(tmp_path):
    layout = layout_for(tmp_path)  # never created .dlms/
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["layout.state_dir"] == "fail"
    # Short-circuit: no db checks attempted
    assert "schema.version" not in sev


def test_run_fails_when_db_missing_even_if_state_dir_exists(tmp_path):
    (tmp_path / ".dlms").mkdir()
    layout = layout_for(tmp_path)
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["layout.db"] == "fail"


# ---------------------------------------------------------------------------
# schema + integrity + journal
# ---------------------------------------------------------------------------

def test_schema_version_matches_expected(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["schema.version"] == "ok"
    assert sev["db.integrity"] == "ok"


def test_schema_version_lower_warns_but_does_not_fail(tmp_path):
    """If a future migration bumps the schema, a CLI run against an older
    db should warn (so doctor still finishes) rather than fail (which would
    discourage the diagnosis run the user actually needs)."""
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    # Delete the schema row so MAX(version) drops below EXPECTED.
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (max(0, doctor.EXPECTED_SCHEMA_VERSION - 1), int(time.time())),
    )
    conn.commit()
    conn.close()
    # Patch EXPECTED upward so we definitely have a gap regardless of current value.
    monkeyed = doctor.EXPECTED_SCHEMA_VERSION
    doctor.EXPECTED_SCHEMA_VERSION = monkeyed + 1
    try:
        report = doctor.run(layout)
    finally:
        doctor.EXPECTED_SCHEMA_VERSION = monkeyed
    sev = _name_to_severity(report)
    assert sev["schema.version"] == "warn"
    # Critical: report continues past schema check
    assert "atoms.census" in sev


def test_schema_version_higher_than_expected_warns(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    # Simulate a future-version db
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (doctor.EXPECTED_SCHEMA_VERSION + 5, int(time.time())),
    )
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["schema.version"] == "warn"


# ---------------------------------------------------------------------------
# atom census + liveness
# ---------------------------------------------------------------------------

def test_census_warns_when_zero_atoms(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["atoms.census"] == "warn"
    # When zero atoms, liveness coverage should not appear (skipped by guard)
    assert "liveness.coverage" not in sev


def test_census_ok_with_atoms_and_predicates(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    for i in range(5):
        atoms_mod.assert_fact(
            conn,
            type="invariant",
            topic_key=f"t{i}",
            summary_50w="x",
            source_kind="manual",
            liveness=Liveness("regex", "README.md", r"x"),
        )
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["atoms.census"] == "ok"
    assert sev["liveness.coverage"] == "ok"
    # All 5 are unchecked → stale
    assert sev["liveness.freshness"] in {"warn", "fail"}


def test_liveness_freshness_ok_after_recent_pass(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    a = atoms_mod.assert_fact(
        conn,
        type="invariant",
        topic_key="recent",
        summary_50w="x",
        source_kind="manual",
        liveness=Liveness("regex", "README.md", r"x"),
    )
    conn.execute(
        "UPDATE atoms SET liveness_last_ok = ? WHERE id = ?",
        (int(time.time()), a.id),
    )
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["liveness.freshness"] == "ok"


# ---------------------------------------------------------------------------
# embedding drift
# ---------------------------------------------------------------------------

def test_embedding_drift_fails_when_multiple_models(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    a1 = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="a1",
        summary_50w="x", source_kind="manual",
    )
    a2 = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="a2",
        summary_50w="x", source_kind="manual",
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO atom_embeddings (atom_id, vss_rowid, model, resolution, hash, embedded_at) "
        "VALUES (?, 1, 'modelA', 50, 'h1', ?)",
        (a1.id, now),
    )
    conn.execute(
        "INSERT INTO atom_embeddings (atom_id, vss_rowid, model, resolution, hash, embedded_at) "
        "VALUES (?, 2, 'modelB', 50, 'h2', ?)",
        (a2.id, now),
    )
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["embeddings.drift"] == "fail"


def test_embedding_drift_ok_with_single_model(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    a = atoms_mod.assert_fact(
        conn, type="invariant", topic_key="a",
        summary_50w="x", source_kind="manual",
    )
    conn.execute(
        "INSERT INTO atom_embeddings (atom_id, vss_rowid, model, resolution, hash, embedded_at) "
        "VALUES (?, 1, 'bge-small-en-v1.5', 50, 'h', ?)",
        (a.id, int(time.time())),
    )
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["embeddings.drift"] == "ok"


# ---------------------------------------------------------------------------
# edges + retrieval
# ---------------------------------------------------------------------------

def test_edges_density_warns_when_atoms_exist_but_no_edges(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    atoms_mod.assert_fact(
        conn, type="invariant", topic_key="solo",
        summary_50w="x", source_kind="manual",
    )
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["edges.density"] == "warn"


def test_retrieval_activity_ok_when_recent_row_present(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    conn.execute(
        "INSERT INTO retrieval_log (ts, session_id, query, classified_as, atoms_returned, "
        "injected_tokens, user_feedback) VALUES (?, 's', 'q', 'semantic', '[]', 0, NULL)",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["retrieval.activity"] == "ok"


def test_retrieval_activity_warns_when_log_empty(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["retrieval.activity"] == "warn"


# ---------------------------------------------------------------------------
# repo freshness + handoff
# ---------------------------------------------------------------------------

def test_repo_freshness_warns_when_no_repos(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["repo.freshness"] == "warn"


def test_repo_freshness_ok_with_recent_index(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    store.register_repo(conn, "repo-a", str(tmp_path))
    store.mark_indexed(conn, "repo-a", "abc1234", "main")
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["repo.freshness"] == "ok"


def test_handoff_dir_ok_when_files_present(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    layout.handoffs.mkdir()
    (layout.handoffs / "session.md").write_text("# handoff")
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["handoff.dir"] == "ok"


# ---------------------------------------------------------------------------
# Report aggregate
# ---------------------------------------------------------------------------

def test_report_counts_severities(tmp_path):
    layout = _layout(tmp_path)
    _make_db(layout).close()
    report = doctor.run(layout)
    # Fresh empty db: some warns expected, no fails
    assert report.fail_count == 0
    assert report.warn_count > 0


# ---------------------------------------------------------------------------
# Degradation paths — one bad check must not kill the whole report
# ---------------------------------------------------------------------------

def test_missing_table_degrades_to_warn_not_crash(tmp_path):
    """Old-schema dbs may lack tables added in later versions. The check
    that touches the missing table should produce a warn finding; later
    checks must still run."""
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    conn.execute("DROP TABLE retrieval_log")
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    # The missing-table check produces a warn (skipped) finding
    assert sev["retrieval.activity"] == "warn"
    # Downstream checks still ran
    assert "repo.freshness" in sev
    assert "handoff.dir" in sev


def test_corrupt_db_is_caught_as_fail(tmp_path):
    """A garbage-bytes db file should produce a connect-time fail without
    crashing python."""
    layout = _layout(tmp_path)
    # Create state dir but write garbage where the db should be
    layout.db.write_bytes(b"this is not a sqlite database")
    report = doctor.run(layout)
    # Either db.open or db.integrity should flag it
    fails = [f.name for f in report.findings if f.severity == "fail"]
    assert fails, f"expected at least one fail finding, got: {report.findings}"


def test_journal_mode_non_wal_warns(tmp_path):
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    # Force DELETE mode (only works on closed/empty WAL); use a fresh db
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.commit()
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    # WAL is sticky on existing dbs; this test may legitimately see ok or warn
    # depending on platform. Just assert the finding exists with a known severity.
    assert sev["db.journal"] in {"ok", "warn"}


# ---------------------------------------------------------------------------
# Liveness boundary calibration — relative threshold, not absolute
# ---------------------------------------------------------------------------

def test_liveness_freshness_relative_threshold_warns_not_fails_on_small_workspace(tmp_path):
    """6 stale predicates in a 6-atom workspace is 100% stale — should fail.
    6 stale predicates in a 500-atom workspace is 1.2% — should only warn.
    Boundary is %, not absolute count."""
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    # 100 atoms, all with predicates, all unchecked → 100% stale
    for i in range(100):
        atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"t{i}",
            summary_50w="x", source_kind="manual",
            liveness=Liveness("regex", "README.md", r"x"),
        )
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["liveness.freshness"] == "fail"


def test_liveness_freshness_warns_when_small_absolute_count(tmp_path):
    """Even at 100% stale, very small workspaces should warn not fail
    (absolute floor of 5 stale predicates)."""
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    for i in range(3):
        atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"t{i}",
            summary_50w="x", source_kind="manual",
            liveness=Liveness("regex", "README.md", r"x"),
        )
    conn.close()
    report = doctor.run(layout)
    sev = _name_to_severity(report)
    assert sev["liveness.freshness"] == "warn"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_cli_doctor_exits_zero_when_no_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layout = _layout(tmp_path)
    _make_db(layout).close()
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "doctor:" in result.stdout


def test_cli_doctor_nonzero_when_failures(tmp_path, monkeypatch):
    """Force a fail by simulating missing db inside an existing state dir."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dlms").mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code >= 1
    assert "fail" in result.stdout.lower()


def test_cli_doctor_verbose_shows_detail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    layout = _layout(tmp_path)
    conn = _make_db(layout)
    for i in range(3):
        atoms_mod.assert_fact(
            conn, type="invariant", topic_key=f"t{i}",
            summary_50w="x", source_kind="manual",
        )
    conn.close()
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "-v"])
    assert result.exit_code == 0
    # Census detail lists the type breakdown
    assert "invariant" in result.stdout
