"""F7 regression tests: the migration runner must never misreport durable state.

Each fixture lives in a temporary migration directory; the shipped migrations are
copied, never edited, and every fixture adds the version after the shipped ones.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from job_cli import db as dbmod
from job_cli import migrations

GOOD_004_ADD_LABEL = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "ALTER TABLE applications ADD COLUMN employment_type TEXT;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'add_label');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

GOOD_004_WITH_TRIGGER = (
    "-- a trigger body contains semicolons; the runner must not split on them\n"
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE audit_marker (id INTEGER PRIMARY KEY, note TEXT);\n"
    "CREATE TRIGGER trg_audit AFTER UPDATE ON applications BEGIN\n"
    "  INSERT INTO audit_marker(id, note) VALUES (new.id, 'changed; audit');\n"
    "END;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'with_trigger');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

POST_COMMIT_STATEMENT = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE oops (x INTEGER);\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'post_commit');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
    "UPDATE schema_migrations SET name = 'bang' WHERE version = 4;\n"
)

SECOND_COMMIT_AND_MORE = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'two_commits');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

WRONG_VERSION_AFTER_COMMIT = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE marker (x INTEGER);\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'wrong_version');\n"
    "PRAGMA user_version = 3;\n"
    "COMMIT;\n"
)

WRONG_LEDGER_NAME = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'not_the_file_name');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

NO_LEDGER_ROW = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

MISSING_COMMIT = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'missing_commit');\n"
    "PRAGMA user_version = 4;\n"
)

NO_BEGIN = (
    "PRAGMA foreign_keys = ON;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'no_begin');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

FAILS_INSIDE_TRANSACTION = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE oops (x INTEGER);\n"
    "SELECT this_is_not_sql;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (4, 'broken');\n"
    "PRAGMA user_version = 4;\n"
    "COMMIT;\n"
)

FAILS_INSIDE_TRANSACTION_5 = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE oops_five (x INTEGER);\n"
    "SELECT this_is_not_sql;\n"
    "INSERT INTO schema_migrations(version, name) VALUES (5, 'broken_five');\n"
    "PRAGMA user_version = 5;\n"
    "COMMIT;\n"
)


def temp_migrations(tmp_path: Path, project_root: Path, scripts: dict[str, str]) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir(parents=True)
    for path in sorted((project_root / "sql" / "migrations").glob("*.sql")):
        shutil.copy2(path, directory / path.name)
    for name, body in scripts.items():
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def bootstrap(tmp_path: Path, project_root: Path):
    db_path = tmp_path / "job.sqlite3"
    conn = dbmod.connect(db_path)
    good_dir = project_root / "sql" / "migrations"
    migrations.apply_pending(conn, migrations.inspect(db_path, good_dir))
    return db_path, conn


def pending_state(conn, db_path: Path, project_root: Path, directory: Path, name: str,
                  version: int = 4):
    state = migrations.inspect(db_path, project_root / "sql" / "migrations")
    state.pending = [migrations.MigrationFile(version, name, directory / f"{version:03d}_{name}.sql")]
    return state


def ledger(conn) -> dict[int, str]:
    return {int(row[0]): str(row[1])
            for row in conn.execute("SELECT version, name FROM schema_migrations")}


def table_exists(conn, name: str) -> bool:
    return dbmod.scalar(
        conn, "SELECT count(*) FROM sqlite_master WHERE name = ?", (name,)) == 1


# --------------------------------------------------------------------------
# pre-execution validation: COMMIT must be the final executable statement
# --------------------------------------------------------------------------
def test_post_commit_statement_is_rejected_before_anything_runs(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_post_commit.sql": POST_COMMIT_STATEMENT})
    db_path = tmp_path / "job.sqlite3"
    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.inspect(db_path, directory)
    message = str(excinfo.value)
    assert "final executable statement" in message
    assert "004_post_commit" in message
    assert not db_path.exists()  # validation happens before the database is touched


def test_apply_pending_rejects_post_commit_statement_and_keeps_version(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_post_commit.sql": POST_COMMIT_STATEMENT})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(
                conn, pending_state(conn, db_path, project_root, directory, "post_commit"))
        assert "final executable statement" in str(excinfo.value)
        assert dbmod.scalar(conn, "PRAGMA user_version") == 3
        assert ledger(conn) == {1: "initial_schema", 2: "reporting_views",
                                3: "intake_folder"}
        assert not table_exists(conn, "oops")
    finally:
        conn.close()


def test_multiple_commit_statements_are_rejected(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_two_commits.sql": SECOND_COMMIT_AND_MORE})
    db_path = tmp_path / "job.sqlite3"
    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.inspect(db_path, directory)
    assert "COMMIT statements" in str(excinfo.value)


def test_script_without_commit_or_begin_is_rejected(tmp_path, project_root):
    directory = temp_migrations(tmp_path, project_root, {
        "004_missing_commit.sql": MISSING_COMMIT,
        "005_no_begin.sql": NO_BEGIN,
    })
    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.discover_migrations(directory)
    assert "never commits" in str(excinfo.value)
    only_begin_missing = temp_migrations(
        tmp_path / "second", project_root, {"004_no_begin.sql": NO_BEGIN})
    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.discover_migrations(only_begin_missing)
    assert "never opens a transaction" in str(excinfo.value)
    assert not db_path_touched(tmp_path)


def db_path_touched(tmp_path: Path) -> bool:
    return any(path.suffix == ".sqlite3" for path in tmp_path.rglob("*.sqlite3"))


# --------------------------------------------------------------------------
# post-COMMIT mismatch: report the real durable state, never a fake rollback
# --------------------------------------------------------------------------
def test_wrong_user_version_after_commit_reports_durable_state(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_wrong_version.sql": WRONG_VERSION_AFTER_COMMIT})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(
                conn, pending_state(conn, db_path, project_root, directory, "wrong_version"))
        message = str(excinfo.value)
        assert "committed but the database does not match" in message
        assert "PRAGMA user_version=3 instead of 4" in message
        assert "user_version=3" in message          # the durable version is reported
        assert "were not rolled back" in message    # no false rollback claim
        assert "stays at version" not in message
        # the commit really is durable
        assert dbmod.scalar(conn, "PRAGMA user_version") == 3
        assert ledger(conn)[4] == "wrong_version"
        assert table_exists(conn, "marker")
    finally:
        conn.close()
    # a later status check surfaces the disagreement instead of calling it clean
    with pytest.raises(migrations.MigrationError) as excinfo:
        migrations.inspect(db_path, project_root / "sql" / "migrations")
    assert "disagrees" in str(excinfo.value)


def test_wrong_ledger_name_after_commit_reports_durable_state(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_wrong_ledger.sql": WRONG_LEDGER_NAME})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(
                conn, pending_state(conn, db_path, project_root, directory, "wrong_ledger"))
        message = str(excinfo.value)
        assert "records version 4 as 'not_the_file_name' instead of 'wrong_ledger'" in message
        assert "user_version=4" in message
        assert ledger(conn)[4] == "not_the_file_name"
    finally:
        conn.close()


def test_missing_ledger_row_after_commit_reports_durable_state(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_no_ledger.sql": NO_LEDGER_ROW})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(
                conn, pending_state(conn, db_path, project_root, directory, "no_ledger"))
        message = str(excinfo.value)
        assert "schema_migrations has no row for version 4" in message
        assert 4 not in ledger(conn)
        assert dbmod.scalar(conn, "PRAGMA user_version") == 4
    finally:
        conn.close()


# --------------------------------------------------------------------------
# failures inside the transaction are rolled back and described truthfully
# --------------------------------------------------------------------------
def test_failure_inside_transaction_rolls_back_and_says_so(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_broken.sql": FAILS_INSIDE_TRANSACTION})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(
                conn, pending_state(conn, db_path, project_root, directory, "broken"))
        message = str(excinfo.value)
        assert "004_broken" in message
        assert "rolled back" in message
        assert "user_version=3" in message
        assert dbmod.scalar(conn, "PRAGMA user_version") == 3
        assert ledger(conn) == {1: "initial_schema", 2: "reporting_views",
                                3: "intake_folder"}
        assert not table_exists(conn, "oops")
    finally:
        conn.close()


def test_failure_after_a_committed_migration_reports_its_real_version(tmp_path, project_root):
    directory = temp_migrations(tmp_path, project_root, {
        "004_add_label.sql": GOOD_004_ADD_LABEL,
        "005_broken_five.sql": FAILS_INSIDE_TRANSACTION_5,
    })
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        state = migrations.inspect(db_path, project_root / "sql" / "migrations")
        state.pending = [
            migrations.MigrationFile(4, "add_label", directory / "004_add_label.sql"),
            migrations.MigrationFile(5, "broken_five", directory / "005_broken_five.sql"),
        ]
        with pytest.raises(migrations.MigrationError) as excinfo:
            migrations.apply_pending(conn, state)
        message = str(excinfo.value)
        assert "005_broken_five" in message
        assert "rolled back" in message
        assert "user_version=4" in message  # 004 stayed applied; no stale "version 3" claim
        assert "stays at version 3" not in message
        assert dbmod.scalar(conn, "PRAGMA user_version") == 4
        assert ledger(conn) == {1: "initial_schema", 2: "reporting_views", 3: "intake_folder",
                                4: "add_label"}
        assert table_exists(conn, "oops_five") is False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the statement splitter itself
# --------------------------------------------------------------------------
def test_statement_splitter_handles_triggers_strings_and_comments():
    script = (
        "-- leading comment\n"
        "PRAGMA foreign_keys = ON;\n"
        "BEGIN IMMEDIATE;\n"
        "CREATE TRIGGER trg AFTER UPDATE ON applications BEGIN\n"
        "  UPDATE applications SET role = 'x;y' WHERE id = new.id;\n"
        "END;\n"
        "COMMIT;\n"
        "-- trailing comment\n"
    )
    statements = migrations.executable_statements(script)
    assert len(statements) == 4
    assert migrations._is_commit(statements[-1])
    assert migrations._is_begin(statements[1])
    assert "x;y" in statements[2]
    for statement in statements:
        assert "comment" not in statement


def test_good_migration_with_a_trigger_body_applies(tmp_path, project_root):
    directory = temp_migrations(
        tmp_path, project_root, {"004_with_trigger.sql": GOOD_004_WITH_TRIGGER})
    db_path, conn = bootstrap(tmp_path, project_root)
    try:
        state = migrations.inspect(db_path, project_root / "sql" / "migrations")
        state.pending = [migrations.MigrationFile(4, "with_trigger",
                                                 directory / "004_with_trigger.sql")]
        applied = migrations.apply_pending(conn, state)
        assert [migration.display for migration in applied] == ["004_with_trigger"]
        assert dbmod.scalar(conn, "PRAGMA user_version") == 4
        assert ledger(conn)[4] == "with_trigger"
        assert table_exists(conn, "audit_marker")
        assert migrations.verify(conn) == []
    finally:
        conn.close()


def test_shipped_migrations_pass_current_validation(project_root):
    files = migrations.discover_migrations(project_root / "sql" / "migrations")
    assert [f.name for f in files] == ["initial_schema", "reporting_views", "intake_folder"]
    for migration in files:
        body = migration.path.read_text(encoding="utf-8")
        statements = migrations.executable_statements(body)
        assert migrations._is_commit(statements[-1])
        assert not any(migrations._is_commit(s) for s in statements[:-1])
        migrations.validate_migration_script(migration, body)


def test_db_migrate_refuses_a_post_commit_script_without_touching_the_database(
        run, project_root):
    assert run("db", "status").ok
    before = run.db.read_bytes()
    (project_root / "sql" / "migrations" / "004_post_commit.sql").write_text(
        POST_COMMIT_STATEMENT, encoding="utf-8")
    dry = run("db", "migrate", "--dry-run")
    assert dry.exit_code == 2
    assert "final executable statement" in dry.output
    real = run("db", "migrate")
    assert real.exit_code == 2
    assert "final executable statement" in real.output
    assert run.db.read_bytes() == before
    conn = dbmod.connect(run.db)
    try:
        assert dbmod.scalar(conn, "PRAGMA user_version") == 3
        assert 4 not in ledger(conn)
    finally:
        conn.close()
