"""SQLite connection handling: parameterized access only, no ORM.

Every connection enables foreign keys and a busy timeout and verifies the two
PRAGMAs actually took effect, because PRAGMA foreign_keys is connection-local.
"""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

from .errors import DatabaseError

MIN_SQLITE = (3, 37, 0)  # STRICT tables
BUSY_TIMEOUT_MS = 5000


def sqlite_runtime_version() -> str:
    return sqlite3.sqlite_version


def check_runtime() -> None:
    current = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
    if current < MIN_SQLITE:
        raise DatabaseError(
            "SQLite runtime " + sqlite3.sqlite_version
            + " is too old; the schema needs STRICT tables (>= 3.37.0)"
        )


def connect(path: str | Path, *, read_only: bool = False,
            must_exist: bool = False) -> sqlite3.Connection:
    """Open a connection with the required PRAGMAs verified."""
    check_runtime()
    target = Path(path)
    if must_exist and not target.exists():
        raise DatabaseError(f"database does not exist: {target}")
    if read_only and not target.exists():
        raise DatabaseError(f"database does not exist: {target}")
    if not read_only:
        target.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        uri = f"file:{target.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(str(target), isolation_level=None)

    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    _verify(conn)
    return conn


def _verify(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DatabaseError("could not enable foreign key enforcement on this connection")
    if not conn.execute("PRAGMA busy_timeout").fetchone()[0]:
        raise DatabaseError("could not set busy_timeout on this connection")


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE / COMMIT / ROLLBACK around a short write unit.

    Nested use joins the outer transaction instead of failing, so helpers can be
    composed safely.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextlib.contextmanager
def open_db(path: str | Path, *, read_only: bool = False,
            must_exist: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(path, read_only=read_only, must_exist=must_exist)
    try:
        yield conn
    finally:
        conn.close()


def integrity_check(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    problems = [row[0] for row in rows if str(row[0]).lower() != "ok"]
    return problems


def foreign_key_check(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("PRAGMA foreign_key_check").fetchall()


def one(conn: sqlite3.Connection, sql: str, params: Sequence | dict = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Sequence | dict = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence | dict = ()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def read_query(path: str | Path) -> str:
    sql = Path(path).read_text(encoding="utf-8")
    if not sql.strip():
        raise DatabaseError(f"empty SQL query file: {path}")
    return sql
