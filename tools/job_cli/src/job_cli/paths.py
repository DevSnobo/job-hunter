"""Locate the job project root and its data/reporting files.

No personal absolute path is hardcoded: the root comes from an explicit
argument, the ``JOB_ROOT`` environment variable (set by the ``./job``
launcher), or a bounded search upwards from this file and from the current
directory.  A root that was named explicitly or through ``$JOB_ROOT`` must
carry the project marker; a wrong value is an error, never a silent fallback.

Configuration layer
-------------------
Paths and the tracker's language can come from a TOML config file.  The rules
are:

* Location: ``$JOB_CONFIG`` when it is set (a relative value is resolved
  against the current directory), else ``<job root>/config/job.toml``.  The
  ``job`` CLI deliberately has no ``--config`` flag: the environment variable
  plus the default path keep the option surface small.
* Precedence for every value: **CLI flag -> environment variable -> config
  file -> built-in default**.
* A missing config file is normal and completely silent: every command runs
  with no config at all.
* An unreadable or invalid config file is a usage error: it raises
  :class:`~job_cli.errors.DatabaseError` (``main()`` turns every ``JobError``
  into exit 2) with a message that names the file and the problem (bad TOML,
  wrong type), and nothing falls back silently.
* A relative path inside the file resolves against the job root, never against
  the current working directory.
* Unknown keys are ignored on purpose: the four tools share ``config/`` and
  each one only reads its own keys.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import DatabaseError

DB_FILENAME = "job.sqlite3"
MIGRATIONS_DIR = ("sql", "migrations")
QUERIES_DIR = ("sql", "queries")

#: How many directory levels the upward search looks at (bounded on purpose:
#: a marker far above the checkout must not turn an unrelated directory into
#: the project root).
MAX_ROOT_WALK = 6

# --------------------------------------------------------------------------
# built-in defaults (the values a missing config file leaves in place)
# --------------------------------------------------------------------------
#: Directory of the config files, below the job root.
CONFIG_DIR = "config"
#: The tracker's config file name, below ``config/``.
CONFIG_FILENAME = "job.toml"
#: Environment variable that names the config file explicitly.
ENV_CONFIG = "JOB_CONFIG"
#: Environment variable that names the database explicitly.
ENV_DB = "JOB_DB"
#: ``<job root>/applications`` - the application and intake folders.
DEFAULT_APPLICATIONS_DIR = "applications"
#: ``<job root>/exports``
DEFAULT_EXPORTS_DIR = "exports"
#: ``<job root>/data/backups`` - backups live next to the database.
DEFAULT_BACKUPS_DIR = ("data", "backups")
#: Reserved: the language the tracker assumes for generated CVs.  No command
#: consumes it yet (see :class:`Layout`); "de" keeps the German tracker default.
DEFAULT_LANGUAGE = "de"


def _looks_like_root(path: Path) -> bool:
    """The single root marker: ``<root>/sql/migrations`` is a directory."""
    return (path / "sql" / "migrations").is_dir()


def _find_root_upwards(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` that carries the marker."""
    for candidate in (start, *start.parents[:MAX_ROOT_WALK]):
        if _looks_like_root(candidate):
            return candidate
    return None


def find_job_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not _looks_like_root(root):
            raise DatabaseError(
                f"{root} does not look like the job project (no sql/migrations directory)"
            )
        return root

    env = os.environ.get("JOB_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not _looks_like_root(root):
            raise DatabaseError(
                f"$JOB_ROOT points at {root}, which does not look like the job project "
                f"(no sql/migrations directory)"
            )
        return root

    # Source checkout: <root>/tools/job_cli/src/job_cli/paths.py
    found = _find_root_upwards(Path(__file__).resolve().parent)
    if found is not None:
        return found

    found = _find_root_upwards(Path.cwd().resolve())
    if found is not None:
        return found

    raise DatabaseError(
        "cannot locate the job project root; pass --root or set JOB_ROOT"
    )


def config_file(root: Path) -> Path:
    """The config file that belongs to ``root`` (it need not exist).

    ``$JOB_CONFIG`` names the file explicitly and wins over the default path;
    a relative value there is resolved against the current directory.
    """
    env = os.environ.get(ENV_CONFIG)
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path
    return root / CONFIG_DIR / CONFIG_FILENAME


def _read_config(path: Path) -> dict:
    """Parse the config file. A missing file is ``{}``; anything else fails.

    An unreadable or invalid file is a usage error: it names the file and the
    problem instead of silently pretending there is no config.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DatabaseError(f"cannot read config file {path}: {exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatabaseError(f"config file {path} is not valid UTF-8: {exc}") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DatabaseError(f"config file {path} is not valid TOML: {exc}") from None


def _text_key(data: dict, key: str, path: Path) -> str | None:
    """One string value of the config file, or ``None`` when it is absent."""
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise DatabaseError(
            f"config file {path}: {key} must be a string, got {type(value).__name__} ({value!r})"
        )
    if not value.strip():
        raise DatabaseError(f"config file {path}: {key} must not be empty")
    return value


def _resolve_relative(value: str | None, root: Path) -> Path | None:
    """Resolve a config path against the job root (absolute stays absolute)."""
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


@dataclass(frozen=True)
class JobConfig:
    """The values of ``config/job.toml`` that this tool understands.

    Every path is already resolved (relative values against the job root), and
    ``None`` means "the file does not set this key".
    """

    db_path: Path | None = None
    applications_dir: Path | None = None
    exports_dir: Path | None = None
    backups_dir: Path | None = None
    default_language: str = DEFAULT_LANGUAGE


def load_config(root: Path) -> JobConfig:
    """Read the config file of ``root`` (see the module docstring for the rules)."""
    path = config_file(root)
    data = _read_config(path)
    return JobConfig(
        db_path=_resolve_relative(_text_key(data, "db", path), root),
        applications_dir=_resolve_relative(_text_key(data, "applications_dir", path), root),
        exports_dir=_resolve_relative(_text_key(data, "exports_dir", path), root),
        backups_dir=_resolve_relative(_text_key(data, "backups_dir", path), root),
        default_language=_text_key(data, "default_language", path) or DEFAULT_LANGUAGE,
    )


@dataclass(frozen=True)
class Layout:
    """Concrete paths derived from the project root and its config file."""

    root: Path
    db_path: Path
    applications_dir: Path
    exports_dir: Path
    backups_dir: Path
    #: Reserved: the language the tracker assumes for generated CVs.  Exposed
    #: for the tools that generate CVs; no command in this package reads it
    #: yet, so it cannot change any output.
    default_language: str = DEFAULT_LANGUAGE

    @property
    def migrations_dir(self) -> Path:
        return self.root.joinpath(*MIGRATIONS_DIR)

    @property
    def queries_dir(self) -> Path:
        return self.root.joinpath(*QUERIES_DIR)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    def query_file(self, name: str) -> Path:
        return self.queries_dir / name


def build_layout(db: str | os.PathLike[str] | None = None,
                 root: str | os.PathLike[str] | None = None) -> Layout:
    """Resolve every path of one invocation.

    Precedence: ``--db`` (``db``) -> ``$JOB_DB`` -> ``config/job.toml: db`` ->
    ``<root>/data/job.sqlite3``; the directories and the language follow the
    same rule with "config file -> built-in default" as the last two steps.
    """
    project_root = find_job_root(root)
    config = load_config(project_root)
    env_db = os.environ.get(ENV_DB)
    if db:
        db_path = Path(db).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
    elif env_db:
        db_path = Path(env_db).expanduser().resolve()
    elif config.db_path is not None:
        db_path = config.db_path
    else:
        db_path = project_root / "data" / DB_FILENAME
    return Layout(
        root=project_root,
        db_path=db_path,
        applications_dir=config.applications_dir or project_root / DEFAULT_APPLICATIONS_DIR,
        exports_dir=config.exports_dir or project_root / DEFAULT_EXPORTS_DIR,
        backups_dir=config.backups_dir or project_root.joinpath(*DEFAULT_BACKUPS_DIR),
        default_language=config.default_language,
    )
