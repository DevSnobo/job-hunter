"""The ``config/posting.toml`` layer of the posting capture.

The rules are identical to the ``job`` tracker's config layer:

* Location: ``$POSTING_CONFIG`` when it is set (a relative value is resolved
  against the current directory), else ``<job root>/config/posting.toml``.  The
  capture flow has no ``--config`` flag on purpose: it asks questions, so the
  environment variable plus the default path are enough.
* Precedence for every value: **CLI flag -> environment variable -> config
  file -> built-in default**.
* A missing config file is normal and completely silent: the tool runs with no
  config at all.
* An unreadable or invalid config file is a *usage* error: :class:`ConfigError`
  names the file and the problem (bad TOML, wrong type) and ``cli.main`` turns
  it into exit 2.  Nothing falls back silently.
* A relative path inside the file resolves against the job root, never against
  the current working directory.
* Unknown keys are ignored on purpose: the four tools share ``config/`` and
  each one only reads its own keys.

Deliberately stdlib-only (``tomllib``): importing this module must never import
prompt_toolkit, which stays a lazily imported dependency of
:mod:`posting_entry.ui`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import cv

#: Directory of the config files, below the job root.
CONFIG_DIR = "config"
#: The capture tool's config file name, below ``config/``.
CONFIG_FILENAME = "posting.toml"
#: Environment variable that names the config file explicitly.
ENV_CONFIG = "POSTING_CONFIG"
#: Applications directory below the job root: ``<job root>/applications``.
DEFAULT_APPLICATIONS_DIR = "applications"
#: Offer the tailored CVs after a saved posting.
DEFAULT_OFFER_CV = True
#: Read the posting text from the clipboard when no source flag was given.
DEFAULT_SOURCE_FROM_CLIPBOARD = False


class ConfigError(Exception):
    """The config file exists but is unreadable or invalid (a usage error)."""


def job_root(applications_dir: Path | None = None) -> Path:
    """The job root that owns ``applications_dir``.

    ``$JOB_ROOT`` wins; otherwise the nearest ancestor of the applications
    directory (of ``<cwd>/applications`` when it is not given) that carries the
    root marker is used - the same resolution as the applications directory
    itself, so the config file of a checkout is found from inside it.
    """
    if applications_dir is None:
        applications_dir = Path.cwd() / DEFAULT_APPLICATIONS_DIR
    return cv.job_root_for(Path(applications_dir))


def config_file(root: Path) -> Path:
    """The config file that belongs to ``root`` (it need not exist).

    ``$POSTING_CONFIG`` names the file explicitly and wins over the default
    path; a relative value there is resolved against the current directory.
    """
    env = os.environ.get(ENV_CONFIG)
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path
    return root / CONFIG_DIR / CONFIG_FILENAME


def _read(path: Path) -> dict:
    """Parse the config file. A missing file is ``{}``; anything else fails."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid UTF-8: {exc}") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None


def _string_key(data: dict, key: str, path: Path) -> str | None:
    """One string value of the config file, or ``None`` when it is absent."""
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(
            f"config file {path}: {key} must be a string, got {type(value).__name__} ({value!r})"
        )
    if not value.strip():
        raise ConfigError(f"config file {path}: {key} must not be empty")
    return value


def _bool_key(data: dict, key: str, path: Path, default: bool) -> bool:
    """One boolean value of the config file, or ``default`` when it is absent."""
    if key not in data:
        return default
    value = data[key]
    # A TOML integer is not a boolean, and ``isinstance(1, int)`` would let it
    # through - so check the exact type.
    if not isinstance(value, bool):
        raise ConfigError(
            f"config file {path}: {key} must be true or false, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


@dataclass(frozen=True)
class PostingConfig:
    """The values of ``config/posting.toml``.

    ``applications_dir`` is already resolved (a relative value against the job
    root); ``None`` means "the file does not set this key".
    """

    applications_dir: Path | None = None
    offer_cv: bool = DEFAULT_OFFER_CV
    default_source_from_clipboard: bool = DEFAULT_SOURCE_FROM_CLIPBOARD


def load(applications_dir: Path | None = None) -> PostingConfig:
    """Read the config file of this machine (see the module docstring).

    ``applications_dir`` only locates the job root, i.e. the default config
    file; it is not a config value.
    """
    root = job_root(applications_dir)
    path = config_file(root)
    data = _read(path)
    configured = _string_key(data, "applications_dir", path)
    resolved = None
    if configured is not None:
        candidate = Path(configured).expanduser()
        resolved = candidate if candidate.is_absolute() else root / candidate
    return PostingConfig(
        applications_dir=resolved,
        offer_cv=_bool_key(data, "offer_cv", path, DEFAULT_OFFER_CV),
        default_source_from_clipboard=_bool_key(
            data, "default_source_from_clipboard", path, DEFAULT_SOURCE_FROM_CLIPBOARD
        ),
    )
