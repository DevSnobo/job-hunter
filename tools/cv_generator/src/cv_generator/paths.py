"""Path resolution shared by the library and CLI.

The generator lives inside the ``job-hunter`` repository (``tools/cv_generator``)
and stays installable on its own.  Every default is anchored to the repository
and to the *private profile*, never to the current working directory, so the
generator behaves identically from any CWD.

Resolution order per value (see :mod:`cv_generator.config`):

``profile``
    ``--profile-dir`` (the CLI flag) -> ``$CVGEN_PROFILE_DIR`` ->
    ``cvgen.toml`` ``profile_dir`` -> ``<repo>/profile``.
``photos``
    ``<profile>/photos`` (personal photos never enter the versioned tree).
``inputs``
    ``<profile>/inputs`` (the reviewed CV JSON inputs).
``output``
    ``$CVGEN_OUTPUT_DIR`` -> ``cvgen.toml`` ``output_dir`` ->
    ``<repo>/exports/generator`` (private, gitignored); ``--output`` on the CLI
    wins over all of them.
``templates``
    ``<project>/templates`` (shipped) plus every ``template_dirs`` entry of
    ``cvgen.toml``, personal directories first (see
    :func:`cv_generator.layouts.template_search_dirs`).

``PROJECT_ROOT`` keeps its old meaning - this project's own directory
(``tools/cv_generator``) - because that is where the shipped resources
(``templates/``) live.

An unusable ``cvgen.toml`` never breaks an import: the error is recorded (see
:func:`config_error`) and the CLI reports it as a usage error (exit 2).
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config as config_module

#: This project's own directory (``<repo>/tools/cv_generator``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Environment variable that points at the private profile directory.
PROFILE_ENV = config_module.ENV_PROFILE_DIR

#: How many directory levels the repository search looks at (bounded on purpose).
MAX_ROOT_WALK = 6


def _looks_like_repo(path: Path) -> bool:
    """The repository carries the migration marker or the uv workspace table."""
    if (path / "sql" / "migrations").is_dir():
        return True
    pyproject = path / "pyproject.toml"
    try:
        return "[tool.uv.workspace]" in pyproject.read_text(encoding="utf-8")
    except OSError:
        return False


def find_repo_root(start: Path | None = None) -> Path:
    """The repository that owns this project.

    Walks upwards from this project's directory and returns the first directory
    that carries the repository marker.  A standalone copy of the generator
    (no marker above it) falls back to the project directory itself, so the
    generator still resolves *some* stable place instead of guessing from the
    current working directory.
    """
    here = (start or PROJECT_ROOT).resolve()
    for candidate in (here, *here.parents[:MAX_ROOT_WALK]):
        if _looks_like_repo(candidate):
            return candidate
    return here


REPO_ROOT = find_repo_root()


def _load_config() -> config_module.Config:
    """Load ``cvgen.toml`` once; an unusable file is recorded, never raised."""
    global CONFIG_ERROR
    try:
        return config_module.load(REPO_ROOT)
    except config_module.ConfigError as exc:
        CONFIG_ERROR = str(exc)
        return config_module.Config()


#: Set when ``cvgen.toml`` exists but cannot be used; the CLI turns it into exit 2.
CONFIG_ERROR: str | None = None
#: The parsed configuration file (empty when there is none).
CONFIG = _load_config()


def config_error() -> str | None:
    """The message for an unusable ``cvgen.toml``, or ``None``."""
    return CONFIG_ERROR


def _configured(value: str | None, base: Path) -> Path | None:
    return config_module.resolve_dir(value, base) if value else None


def _profile_dir() -> Path:
    """The private profile: CLI override, ``$CVGEN_PROFILE_DIR``, config, ``<repo>/profile``."""
    env = os.environ.get(PROFILE_ENV)
    if env:
        return Path(env).expanduser()
    configured = _configured(CONFIG.text("profile_dir"), REPO_ROOT)
    if configured is not None:
        return configured
    return REPO_ROOT / "profile"


def profile_dir() -> Path:
    """The private profile directory (see :data:`PROFILE_DIR`)."""
    return PROFILE_DIR


def use_config(explicit: str | os.PathLike[str]) -> "config_module.Config":
    """Adopt an explicitly named configuration file (the ``--config`` CLI flag).

    The flag wins over the environment and over the default location, so it is
    applied before anything is rendered; every derived default follows.
    Raises :class:`cv_generator.config.ConfigError` for an unusable file.
    """
    global CONFIG, CONFIG_ERROR
    CONFIG = config_module.load(REPO_ROOT, explicit)
    CONFIG_ERROR = None
    _refresh_derived()
    return CONFIG


def _refresh_derived() -> None:
    """Recompute the values derived from the current configuration file."""
    global PROFILE_DIR, PROFILE_PIC_DIR, INPUTS_DIR, DEFAULT_OUTPUT_DIR
    PROFILE_DIR = _profile_dir()
    PROFILE_PIC_DIR = PROFILE_DIR / "photos"
    INPUTS_DIR = PROFILE_DIR / "inputs"
    DEFAULT_OUTPUT_DIR = _output_dir()


def use_profile_dir(path: str | os.PathLike[str]) -> Path:
    """Adopt an explicit profile directory (the ``--profile-dir`` CLI flag).

    The CLI flag wins over every other source, so it is applied before anything
    is rendered; the derived directories follow.
    """
    global PROFILE_DIR, PROFILE_PIC_DIR, INPUTS_DIR
    PROFILE_DIR = Path(path).expanduser()
    PROFILE_PIC_DIR = PROFILE_DIR / "photos"
    INPUTS_DIR = PROFILE_DIR / "inputs"
    return PROFILE_DIR


def default_input_path(language: str) -> Path:
    """``<profile>/inputs/current_<language>.json`` (the ``--language`` shortcut)."""
    return INPUTS_DIR / f"current_{language}.json"


def configured_input() -> Path | None:
    """``cvgen.toml`` ``input`` (relative to the profile directory), if set."""
    value = CONFIG.text("input")
    return _configured(value, PROFILE_DIR) if value else None


def configured_template_dirs() -> list[Path]:
    """``cvgen.toml`` ``template_dirs`` as paths, in file order (personal first)."""
    return [config_module.resolve_dir(value, REPO_ROOT) for value in CONFIG.text_list("template_dirs")]


def configured_template() -> str | None:
    """``cvgen.toml`` ``[template] name`` (the active template when several exist)."""
    value = CONFIG.table("template").get("name")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise config_module.ConfigError(
            f"{CONFIG.path}: [template] name must be a non-empty string"
        )
    return value.strip()


def _output_dir() -> Path:
    env = os.environ.get(config_module.ENV_OUTPUT_DIR)
    if env:
        return Path(env).expanduser()
    configured = _configured(CONFIG.text("output_dir"), REPO_ROOT)
    if configured is not None:
        return configured
    return REPO_ROOT / "exports" / "generator"


PROFILE_DIR = _profile_dir()
PROFILE_PIC_DIR = PROFILE_DIR / "photos"
INPUTS_DIR = PROFILE_DIR / "inputs"
DEFAULT_OUTPUT_DIR = _output_dir()

def _shipped_templates_dir() -> Path:
    """The shipped templates: the source tree's ``templates/``, else the wheel's copy.

    In a source checkout they live next to the project (``tools/cv_generator/
    templates``); an installed wheel carries them inside the package
    (``cv_generator/templates``, see the hatch ``force-include``).
    """
    source = PROJECT_ROOT / "templates"
    if source.is_dir():
        return source
    return Path(__file__).resolve().parent / "templates"


#: The shipped templates: always scanned, always last (personal dirs win).
TEMPLATES_DIR = _shipped_templates_dir()
#: Sentinel photos named by the shipped fixtures (see ``LayoutOptions.photo_for``).
SHIPPED_FIXTURES_DIR = PROJECT_ROOT / "fixtures"

__all__ = [
    "CONFIG",
    "CONFIG_ERROR",
    "DEFAULT_OUTPUT_DIR",
    "INPUTS_DIR",
    "MAX_ROOT_WALK",
    "PROFILE_DIR",
    "PROFILE_ENV",
    "PROFILE_PIC_DIR",
    "PROJECT_ROOT",
    "REPO_ROOT",
    "SHIPPED_FIXTURES_DIR",
    "TEMPLATES_DIR",
    "config_error",
    "configured_input",
    "configured_template",
    "configured_template_dirs",
    "default_input_path",
    "find_repo_root",
    "profile_dir",
    "use_profile_dir",
]
