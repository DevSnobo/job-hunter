"""Migrations, bootstrap, backups, and database-state rejection."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from job_cli import db as dbmod
from job_cli import migrations

from helpers import payload, rows


def test_bootstrap_creates_schema_and_is_idempotent(run, project_root):
    result = run("db", "status", "--json")
    assert result.ok, result.output
    info = payload(run, "db", "status", "--json")
    assert info["user_version"] == 3
    assert info["applied_migrations"] == {
        "001": "initial_schema", "002": "reporting_views", "003": "intake_folder",
    }
    assert info["pending_migrations"] == []
    assert info["row_counts"]["applications"] == 0

    before = run.db.read_bytes()
    again = run("db", "status")
    assert again.ok
    assert "initialized" not in again.output
    assert "applied 001" not in again.output
    assert run.db.read_bytes() == before  # no rerun, no file churn


def test_migrations_are_not_silently_idempotent(project_root):
    conn = dbmod.connect(project_root / "fresh.sqlite3")
    try:
        first = migrations.inspect(project_root / "fresh.sqlite3", project_root / "sql" / "migrations")
        migrations.apply_pending(conn, first)
        sql = (project_root / "sql" / "migrations" / "001_initial_schema.sql").read_text()
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            assert "schema_migrations" in str(exc) or "already exists" in str(exc)
        else:  # pragma: no cover - the rerun must fail
            raise AssertionError("rerunning 001 must fail instead of hiding drift")
    finally:
        conn.close()


def test_pending_migration_requires_explicit_approval(run, project_root):
    assert run("db", "status").ok
    (project_root / "sql" / "migrations" / "004_add_label.sql").write_text(
        "PRAGMA foreign_keys = ON;\nBEGIN IMMEDIATE;\n"
        "ALTER TABLE applications ADD COLUMN employment_type TEXT;\n"
        "INSERT INTO schema_migrations(version, name) VALUES (4, 'add_label');\n"
        "PRAGMA user_version = 4;\nCOMMIT;\n"
    )
    refused = run("list")
    assert refused.exit_code == 2
    assert "pending migration" in refused.output
    assert "db migrate" in refused.output

    dry = run("db", "migrate", "--dry-run")
    assert dry.ok and "004_add_label" in dry.output

    applied = run("db", "migrate")
    assert applied.ok, applied.output
    assert "004_add_label" in applied.output
    assert "backup" in applied.output
    backups = sorted((project_root / "data" / "backups").glob("*.sqlite3"))
    assert len(backups) == 1

    info = payload(run, "db", "status", "--json")
    assert info["user_version"] == 4
    assert info["applied_migrations"]["004"] == "add_label"

    conn = dbmod.connect(run.db)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(applications)")]
    finally:
        conn.close()
    assert "employment_type" in columns
    assert migrations.verify(dbmod.connect(run.db)) == []


def test_failed_migration_keeps_previous_version(run, project_root):
    assert run("db", "status").ok
    (project_root / "sql" / "migrations" / "004_broken.sql").write_text(
        "PRAGMA foreign_keys = ON;\nBEGIN IMMEDIATE;\n"
        "CREATE TABLE oops (x INTEGER);\n"
        "SELECT this_is_not_sql;\n"
        "INSERT INTO schema_migrations(version, name) VALUES (4, 'broken');\n"
        "PRAGMA user_version = 4;\nCOMMIT;\n"
    )
    result = run("db", "migrate")
    assert result.exit_code == 2
    assert "004_broken" in result.output
    conn = dbmod.connect(run.db)
    try:
        assert dbmod.scalar(conn, "PRAGMA user_version") == 3
        assert dbmod.scalar(
            conn, "SELECT count(*) FROM sqlite_master WHERE name = 'oops'") == 0
    finally:
        conn.close()


def test_newer_database_is_rejected(run, project_root):
    assert run("db", "status").ok
    conn = dbmod.connect(run.db)
    try:
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES (4, 'from_the_future')")
        conn.execute("PRAGMA user_version = 4")
    finally:
        conn.close()
    result = run("list")
    assert result.exit_code == 2
    assert "newer tooling" in result.output or "missing" in result.output


def test_unrelated_database_is_rejected(run, project_root, tmp_path):
    other = tmp_path / "other.sqlite3"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something (x INTEGER)")
    conn.commit()
    conn.close()
    result = run("list", db=other)
    assert result.exit_code == 2
    assert "not a job database" in result.output


def test_user_version_disagreement_is_rejected(run, project_root):
    assert run("db", "status").ok
    conn = dbmod.connect(run.db)
    try:
        conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()
    result = run("list")
    assert result.exit_code == 2
    assert "disagrees" in result.output


def test_backup_and_restore_round_trip(run, project_root):
    make = run("add", "--company", "Acme", "--role", "Dev", "--submitted-on", "2026-01-05")
    assert make.ok, make.output
    backup = run("db", "backup")
    assert backup.ok
    path = Path(backup.output.split()[-1])
    assert path.exists()

    assert run("add", "--company", "Second", "--role", "Dev",
               "--submitted-on", "2026-01-06").ok
    assert len(rows(run, "list", "--json")) == 2

    restored = run("db", "restore", str(path), "--yes")
    assert restored.ok, restored.output
    assert len(rows(run, "list", "--json")) == 1


def test_backups_never_overwrite_each_other(run, project_root):
    assert run("db", "backup").ok
    assert run("db", "backup").ok
    backups = list((project_root / "data" / "backups").glob("*.sqlite3"))
    assert len(backups) == 2
    assert backups[0].read_bytes() != b""


def test_doctor_reports_missing_and_unregistered_files(run, project_root, new_app, tmp_path):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("hello")
    assert run("attach", "Acme GmbH", str(source), "--type", "job_posting").ok
    registered = next((project_root / "applications" / app_id).iterdir())
    registered.rename(registered.with_suffix(".moved"))

    orphan = project_root / "applications" / app_id / "extra.txt"
    orphan.write_text("stray")
    intake = project_root / "applications" / "Globex_155461"
    intake.mkdir(parents=True, exist_ok=True)
    (intake / "posting.txt").write_text("pre-submission intake")

    report = payload(run, "db", "doctor", "--json")
    assert report["integrity"] == []
    assert any("posting" in path for path in report["missing_files"])
    assert any(path.endswith("extra.txt") for path in report["unregistered_files"])
    assert any("Globex_155461" in path for path in report["untracked_folders"])
    assert not any("Globex" in path for path in report["unregistered_files"])


def test_schema_smoke_sql_passes_on_a_temp_database(project_root, tmp_path):
    db_path = tmp_path / "smoke.sqlite3"
    conn = dbmod.connect(db_path)
    try:
        state = migrations.inspect(db_path, project_root / "sql" / "migrations")
        migrations.apply_pending(conn, state)
    finally:
        conn.close()
    smoke = (project_root / "sql" / "tests" / "schema_smoke.sql").read_text()
    script = smoke.replace("PRAGMA foreign_keys = ON;", "PRAGMA foreign_keys = ON;\n")
    conn = dbmod.connect(db_path)
    try:
        conn.executescript(script)
        remaining = conn.execute("SELECT count(*) FROM applications").fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0  # the smoke script rolls back
