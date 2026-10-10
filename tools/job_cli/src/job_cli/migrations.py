"""Numbered SQL migrations, backups, and database state inspection.

Migrations live in sql/migrations/NNN_name.sql. Each file owns its
BEGIN IMMEDIATE/COMMIT and its own schema_migrations row plus user_version,
so the runner never wraps executescript() in an outer transaction. The runner
checks before running a script that the single COMMIT is its final executable
statement, and after each script that the committed user_version and the
schema_migrations row really match the file. Failure reports always describe the
durable state that was actually observed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import db as dbmod
from .errors import DatabaseError, MigrationError

FILENAME_RE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[A-Za-z0-9_.-]+)\.sql$")
BEGIN_RE = re.compile(r"^BEGIN(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?$", re.IGNORECASE)
COMMIT_RE = re.compile(r"^(COMMIT|END)(\s+TRANSACTION)?$", re.IGNORECASE)


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    path: Path

    @property
    def display(self) -> str:
        return f"{self.version:03d}_{self.name}"


def _strip_comments(sql: str) -> str:
    """Drop SQL comments that are outside string or quoted-identifier literals."""
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"', "`"):
            end = index + 1
            while end < length:
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            out.append(sql[index:end])
            index = end
        elif char == "[":
            end = sql.find("]", index + 1)
            end = length if end < 0 else end + 1
            out.append(sql[index:end])
            index = end
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end < 0 else end
            out.append(" ")
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append(" ")
            index = end
        else:
            out.append(char)
            index += 1
    return "".join(out)


def executable_statements(sql: str) -> list[str]:
    """Split a script into complete statements, character by character.

    Splitting on ";" would tear a trigger body (BEGIN ... END;) apart, so this uses
    sqlite3.complete_statement, which knows about strings, comments, and trigger
    bodies. Comments and blank fragments are dropped.
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            fragment = _strip_comments(buffer).strip()
            if fragment:
                statements.append(fragment)
            buffer = ""
    tail = _strip_comments(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _terminal(statement: str) -> str:
    return statement.strip().rstrip(";").strip()


def _is_commit(statement: str) -> bool:
    return bool(COMMIT_RE.match(_terminal(statement)))


def _is_begin(statement: str) -> bool:
    return bool(BEGIN_RE.match(_terminal(statement)))


def _shorten(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def validate_migration_script(migration: MigrationFile, sql: str) -> None:
    """Require one transaction whose COMMIT is the final executable statement.

    A statement after COMMIT would run outside the transaction that the runner can
    roll back, so a failure there would leave a durable, half-applied migration.
    """
    statements = executable_statements(sql)
    if not statements:
        raise MigrationError(f"{migration.display} contains no SQL statements")
    commits = [index for index, statement in enumerate(statements) if _is_commit(statement)]
    if not commits:
        raise MigrationError(
            f"{migration.display} never commits; end it with COMMIT so a failed script"
            " can be rolled back"
        )
    if len(commits) > 1:
        raise MigrationError(
            f"{migration.display} has {len(commits)} COMMIT statements; exactly one final"
            " COMMIT is allowed so a failure cannot leave a partly applied migration"
        )
    last = len(statements) - 1
    if commits[0] != last:
        raise MigrationError(
            f"{migration.display} has a statement after its COMMIT"
            f" ({_shorten(statements[commits[0] + 1])!r}); the transaction COMMIT must be"
            " the final executable statement. Move that work before the COMMIT or split"
            " it into the next migration."
        )
    begins = [index for index, statement in enumerate(statements) if _is_begin(statement)]
    if not begins:
        raise MigrationError(
            f"{migration.display} never opens a transaction; start it with BEGIN IMMEDIATE"
            " so a failed script can be rolled back"
        )
    if begins[-1] > commits[0]:
        raise MigrationError(
            f"{migration.display} opens a transaction after its COMMIT; keep one"
            " BEGIN IMMEDIATE ... COMMIT block"
        )


def discover_migrations(migrations_dir: str | Path) -> list[MigrationFile]:
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise MigrationError(f"migration directory not found: {directory}")
    found: list[MigrationFile] = []
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".sql" or not entry.is_file():
            continue
        match = FILENAME_RE.match(entry.name)
        if not match:
            raise MigrationError(
                f"unexpected migration filename: {entry.name}"
                " (expected NNN_descriptive_name.sql)"
            )
        found.append(MigrationFile(int(match.group("version")), match.group("name"), entry))
    if not found:
        raise MigrationError(f"no migration files in {directory}")
    versions = [m.version for m in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            "migration files must be numbered 1..N without gaps; found " + ", ".join(map(str, versions))
        )
    for migration in found:
        body = migration.path.read_text(encoding="utf-8")
        if "schema_migrations" not in body or "user_version" not in body:
            raise MigrationError(
                f"{migration.display} must insert its schema_migrations row and set user_version"
            )
        validate_migration_script(migration, body)
    return found


@dataclass
class DbState:
    path: Path
    exists: bool
    is_empty: bool
    user_version: int
    applied: dict[int, str]
    files: list[MigrationFile]
    pending: list[MigrationFile] = field(default_factory=list)

    @property
    def initialized(self) -> bool:
        return bool(self.applied)

    @property
    def current_version(self) -> int:
        return max(self.applied) if self.applied else 0

    @property
    def has_data_tables(self) -> bool:
        return not self.is_empty


def inspect(path: str | Path, migrations_dir: str | Path,
            conn: sqlite3.Connection | None = None) -> DbState:
    """Read schema_migrations + user_version and compare with the files on disk."""
    target = Path(path)
    files = discover_migrations(migrations_dir)
    disk_versions = {m.version for m in files}
    owns = conn is None
    if conn is None:
        conn = dbmod.connect(target)
    try:
        tables = dbmod.user_tables(conn)
        user_version = int(dbmod.scalar(conn, "PRAGMA user_version") or 0)
        is_empty = not tables
        if "schema_migrations" not in tables:
            if is_empty and user_version == 0:
                return DbState(target, target.exists(), True, 0, {}, files, list(files))
            raise DatabaseError(
                f"{target} contains tables but no schema_migrations table;"
                " it is not a job database"
            )
        rows = dbmod.all_rows(conn, "SELECT version, name FROM schema_migrations ORDER BY version")
        applied = {int(r[0]): str(r[1]) for r in rows}
        if not applied:
            raise DatabaseError(f"{target} has an empty schema_migrations table")
        applied_versions = sorted(applied)
        if applied_versions != list(range(1, len(applied_versions) + 1)):
            raise MigrationError(
                "schema_migrations has gaps: " + ", ".join(map(str, applied_versions))
            )
        if user_version != applied_versions[-1]:
            raise MigrationError(
                f"PRAGMA user_version={user_version} disagrees with highest applied"
                f" migration {applied_versions[-1]}"
            )
        unknown = sorted(set(applied_versions) - disk_versions)
        if unknown:
            raise MigrationError(
                "database was created by newer tooling: migration(s) "
                + ", ".join(f"{v:03d}" for v in unknown)
                + " are missing from " + str(Path(migrations_dir))
            )
        for version, name in applied.items():
            expected = next(m for m in files if m.version == version)
            if expected.name != name:
                raise MigrationError(
                    f"migration {version:03d} is named '{name}' in the database but"
                    f" '{expected.name}' on disk; files must not be edited or renamed"
                )
        pending = [m for m in files if m.version > applied_versions[-1]]
        return DbState(target, target.exists(), is_empty, user_version, applied, files, pending)
    finally:
        if owns:
            conn.close()


def durable_state(conn: sqlite3.Connection) -> dict:
    """The committed state actually visible on this connection right now."""
    state: dict = {"user_version": None, "applied": None, "user_tables": None}
    try:
        state["user_version"] = int(dbmod.scalar(conn, "PRAGMA user_version") or 0)
        tables = dbmod.user_tables(conn)
        state["user_tables"] = len(tables)
        if "schema_migrations" in tables:
            rows = dbmod.all_rows(
                conn, "SELECT version, name FROM schema_migrations ORDER BY version"
            )
            state["applied"] = {int(row[0]): str(row[1]) for row in rows}
    except sqlite3.Error as exc:
        state["error"] = str(exc)
    return state


def _durable_summary(state: dict) -> str:
    if state.get("error"):
        return f"unreadable database state ({state['error']})"
    applied = state.get("applied")
    ledger = "missing schema_migrations table" if applied is None else (
        "{" + ", ".join(f"{v:03d}: {name!r}" for v, name in sorted(applied.items())) + "}"
    )
    return (f"user_version={state.get('user_version')}, schema_migrations={ledger},"
            f" user tables={state.get('user_tables')}")


def _ledger_problems(conn: sqlite3.Connection, migration: MigrationFile) -> list[str]:
    """Compare the committed ledger and user_version with one migration file."""
    state = durable_state(conn)
    problems: list[str] = []
    if state.get("error"):
        problems.append("cannot read the database state: " + state["error"])
        return problems
    if state["user_version"] != migration.version:
        problems.append(
            f"PRAGMA user_version={state['user_version']} instead of {migration.version}"
        )
    applied = state["applied"]
    if applied is None:
        problems.append("the schema_migrations table is missing")
        return problems
    recorded = applied.get(migration.version)
    if recorded is None:
        problems.append(f"schema_migrations has no row for version {migration.version}")
    elif recorded != migration.name:
        problems.append(
            f"schema_migrations records version {migration.version} as {recorded!r}"
            f" instead of {migration.name!r}"
        )
    versions = sorted(applied)
    if versions != list(range(1, versions[-1] + 1)):
        problems.append("schema_migrations has gaps: " + ", ".join(map(str, versions)))
    if versions and versions[-1] != migration.version:
        problems.append(
            f"the highest applied version is {versions[-1]} instead of {migration.version}"
        )
    return problems


def _failure_message(conn: sqlite3.Connection, migration: MigrationFile,
                     exc: sqlite3.Error) -> str:
    state = durable_state(conn)
    message = [
        f"migration {migration.display} failed: {exc}",
        "actual database state after the failure: " + _durable_summary(state),
    ]
    if state.get("user_version") == migration.version:
        message.append(
            "the script had already committed, so its changes are permanent and were not"
            " rolled back; fix forward with a new migration or restore the pre-migration"
            " backup"
        )
    else:
        message.append(
            "no further migrations were applied and the failed transaction was rolled back"
        )
    return ". ".join(message) + "."


def _post_commit_message(conn: sqlite3.Connection, migration: MigrationFile,
                         problems: list[str]) -> str:
    state = durable_state(conn)
    return (
        f"migration {migration.display} committed but the database does not match the file: "
        + "; ".join(problems)
        + ". Actual durable state: " + _durable_summary(state)
        + ". The commit already happened, so these changes were not rolled back; fix"
        " forward with a new migration or restore the pre-migration backup. No further"
        " migrations were applied."
    )


def apply_pending(conn: sqlite3.Connection, state: DbState) -> list[MigrationFile]:
    """Apply pending migrations one by one. Returns the applied files.

    Each script is validated first (one transaction, COMMIT last), then re-checked
    after it runs: the committed user_version and the schema_migrations row must
    match the file. Error messages report the durable state that was observed.
    """
    applied: list[MigrationFile] = []
    for migration in state.pending:
        sql = migration.path.read_text(encoding="utf-8")
        validate_migration_script(migration, sql)
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise MigrationError(_failure_message(conn, migration, exc)) from exc
        problems = _ledger_problems(conn, migration)
        if problems:
            _rollback_quietly(conn)
            raise MigrationError(_post_commit_message(conn, migration, problems))
        applied.append(migration)
    return applied


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Roll back the failed script's transaction when one is still open."""
    if not conn.in_transaction:
        return
    with contextlib.suppress(sqlite3.Error):
        conn.execute("ROLLBACK")


def verify(conn: sqlite3.Connection) -> list[str]:
    """Return human-readable problems found by integrity and FK checks."""
    problems = [f"integrity_check: {p}" for p in dbmod.integrity_check(conn)]
    for row in dbmod.foreign_key_check(conn):
        problems.append(
            f"foreign_key_check: table={row[0]} rowid={row[1]} parent={row[2]} fkid={row[3]}"
        )
    return problems


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")


def backup(conn: sqlite3.Connection, backups_dir: str | Path,
           label: str = "job") -> Path:
    """Copy the live database with the SQLite backup API. Never overwrites."""
    directory = Path(backups_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{label}-{timestamp()}"
    target = directory / f"{stem}.sqlite3"
    counter = 1
    while target.exists():
        counter += 1
        target = directory / f"{stem}-{counter}.sqlite3"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def backup_file(src: str | Path, backups_dir: str | Path, label: str = "job") -> Path:
    """Back up a database file that is not currently open (used before restore)."""
    source = Path(src)
    if not source.exists():
        raise DatabaseError(f"cannot back up missing database {source}")
    conn = dbmod.connect(source, must_exist=True)
    try:
        return backup(conn, backups_dir, label=label)
    finally:
        conn.close()


def restore(src: str | Path, dest: str | Path) -> None:
    """Replace the database file with a backup, via the SQLite backup API."""
    source = Path(src)
    if not source.exists():
        raise DatabaseError(f"backup file not found: {source}")
    if source.resolve() == Path(dest).resolve():
        raise DatabaseError("backup and target are the same file")
    src_conn = dbmod.connect(source, read_only=True)
    try:
        src_conn.execute("PRAGMA query_only = ON")
        dest_conn = dbmod.connect(dest)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def copy_atomic(src: str | Path, dest: str | Path) -> None:
    """Copy a file with a staged temp file and atomic rename."""
    source = Path(src)
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-copy-")
    temp = Path(temp_name)
    try:
        with open(handle, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        temp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
