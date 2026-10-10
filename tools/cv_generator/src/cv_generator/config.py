"""The optional ``cvgen.toml`` configuration file.

Every value the generator can be configured with has the same resolution
order:

    CLI flag  ->  environment variable  ->  config file  ->  built-in default

The file is *optional*: without it the generator uses the built-in defaults and
stays silent.  A file that exists but cannot be used (missing permissions,
invalid TOML, a value of the wrong type) is a usage error: :class:`ConfigError`
names the file and the problem, and the CLI reports it and exits 2 instead of
falling back silently.

Where the file is looked up
---------------------------
1. ``--config PATH`` (the CLI flag), else
2. ``$CVGEN_CONFIG``, else
3. ``<repo>/config/cvgen.toml``.

Keys (all optional)
-------------------
``profile_dir``
    The private profile directory.  Environment: ``CVGEN_PROFILE_DIR``.
    A relative path is resolved against the repository root.
``output_dir``
    Where ``cvgen render`` writes by default.  Environment:
    ``CVGEN_OUTPUT_DIR``.  Relative paths resolve against the repository root.
``input``
    Default input file for ``cvgen render`` when no input argument is given.
    A relative path resolves against the profile directory.
``template``
    The active template name when more than one template is discovered
    (``[template] name = "..."``).  ``--layout`` always wins over it.
``template_dirs``
    Extra template search directories, personal ones first; the shipped
    ``<repo>/tools/cv_generator/templates`` directory is always scanned after
    them, so a personal template can replace a shipped one of the same name.
    Relative paths resolve against the repository root.

An unknown key is ignored (forward compatibility); a key of the wrong type is
an error, because it silently would not do what the user wrote.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: File name of the generator's configuration file.
CONFIG_FILENAME = "cvgen.toml"

#: Environment variable that names the configuration file.
ENV_CONFIG = "CVGEN_CONFIG"
#: Environment variable that overrides ``profile_dir``.
ENV_PROFILE_DIR = "CVGEN_PROFILE_DIR"
#: Environment variable that overrides ``output_dir``.
ENV_OUTPUT_DIR = "CVGEN_OUTPUT_DIR"


class ConfigError(RuntimeError):
    """Raised when a config file exists but cannot be used."""


def config_path(root: str | os.PathLike[str], explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the configuration file: ``--config`` -> ``$CVGEN_CONFIG`` -> ``<root>/config``."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    return Path(root) / "config" / CONFIG_FILENAME


@dataclass(frozen=True)
class Config:
    """A parsed configuration file (or the empty default when there is none)."""

    path: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.path is not None

    def table(self, name: str) -> dict[str, Any]:
        """Return one ``[table]``; an empty dict when the table is absent."""
        value = self.data.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self._fail(f"[{name}] must be a table, not {type(value).__name__}")
        return value

    def text(self, key: str, default: str | None = None) -> str | None:
        """A top-level string value (``profile_dir``, ``input``, ``output_dir``)."""
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            self._fail(f"{key} must be a string, not {type(value).__name__}")
        return value

    def integer(self, key: str, default: int) -> int:
        value = self.data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail(f"{key} must be an integer, not {value!r}")
        return value

    def text_list(self, key: str) -> list[str]:
        """A top-level list of strings (``template_dirs``)."""
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self._fail(f"{key} must be a list of strings, not {value!r}")
        return list(value)

    def _fail(self, problem: str) -> None:
        where = self.path if self.path is not None else "(no config file)"
        raise ConfigError(f"{where}: {problem}")


def load(root: str | os.PathLike[str], explicit: str | os.PathLike[str] | None = None) -> Config:
    """Load the configuration file; ``Config(path=None)`` when there is none.

    Raises :class:`ConfigError` when the file exists but cannot be read or
    parsed.  A missing file is normal and never an error.
    """
    path = config_path(root, explicit)
    if not path.is_file():
        return Config()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib always returns a table
        raise ConfigError(f"{path}: top-level TOML value must be a table")
    return Config(path=path, data=data)


def resolve_dir(value: str, base: Path) -> Path:
    """A path from the config file: absolute as given, relative against ``base``."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (Path(base) / path)


__all__ = [
    "CONFIG_FILENAME",
    "ENV_CONFIG",
    "ENV_OUTPUT_DIR",
    "ENV_PROFILE_DIR",
    "Config",
    "ConfigError",
    "config_path",
    "load",
    "resolve_dir",
]
