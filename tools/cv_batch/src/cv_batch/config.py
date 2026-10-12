"""Path, policy and configuration defaults for the ``cv`` tool.

Every location is configurable.  Defaults are derived from the *job root* (the
directory that holds ``sql/migrations``) which the ``cv`` launcher exports as
``CV_JOB_ROOT``.  No user-specific absolute path is hard-coded, so tests can
point the tool at a temporary project tree.

Configuration file
------------------
Optional ``<job root>/config/cv.toml`` (see ``config/cv.toml.example``), found
through ``--config``, ``$CV_CONFIG`` or the default location.  Precedence for
every value:

    CLI flag  ->  environment variable  ->  cv.toml  ->  built-in default

Environment variables exist for the job root (``CV_JOB_ROOT``), the agent
command (``CV_AGENT_COMMAND``), the agent answer format (``CV_AGENT_FORMAT``)
and the configuration file itself (``CV_CONFIG``); the other values have no
environment variable and fall straight from the CLI flag to ``cv.toml`` and then
to the built-in default.

The agent is never hard-coded: ``[agent] command`` (see below) is the only way
the tool learns how to invoke a CLI agent, and a run without one is refused.

A missing config file is normal and silent: every run works without one.  A file
that exists but cannot be used (invalid TOML, a value of the wrong type) is a
usage error naming the file and the problem - never a silent fallback.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_JOB_ROOT = "CV_JOB_ROOT"
ENV_AGENT_COMMAND = "CV_AGENT_COMMAND"
ENV_AGENT_FORMAT = "CV_AGENT_FORMAT"
ENV_CONFIG = "CV_CONFIG"

#: File name of this tool's configuration file (below ``<root>/config``).
CONFIG_FILENAME = "cv.toml"

#: The ``[agent]`` table: how the CLI agent is invoked and how its answer is
#: read.  ``command`` is the full argument list (``{prompt}`` required,
#: ``{system_prompt}`` optional); ``format`` is ``text`` (the answer is stdout,
#: the default) or ``prime-agent-json`` (the answer is in prime-agent's JSON
#: event stream).
CONFIG_AGENT_TABLE = "agent"
CONFIG_AGENT_COMMAND = "command"
CONFIG_AGENT_FORMAT = "format"

#: Renderer layout forwarded verbatim to ``cvgen render --layout``.  ``classic``
#: is the generator's shipped reference template; naming it explicitly and
#: omitting the flag must behave identically.  The name is deliberately opaque
#: here: the generator's template registry is the single source of truth, and a
#: personal or third-party template may add further names, so this tool never
#: validates a layout name against a local list.
DEFAULT_LAYOUT = "classic"

#: ``cv.toml`` key holding the renderer template (same meaning as ``--layout``).
CONFIG_GENERATOR_TEMPLATE = "generator_template"

#: The headline is a statement about the applicant, not a per-vacancy choice:
#: it is taken from the profile baseline (``profile/inputs/current_<lang>.json``)
#: and the model never changes it, so no posting can produce a title that
#: overflows or misstates the role.  ``cv.toml`` ``headline`` overrides it.
DEFAULT_HEADLINE = ""

#: The CV file name of one folder.  ``{name}`` is the slug built from the
#: profile's first and last name (see :func:`name_slug`), so the tool never
#: hard-codes the applicant's identity.
DEFAULT_TARGET_TEMPLATE = "CV_{name}_{language}_{folder}.pdf"

DEFAULT_MAX_BULLETS_PER_ENTRY = 5
DEFAULT_MAX_BULLETS_TOTAL = 13
DEFAULT_MAX_HEADLINE = 60
DEFAULT_MODEL_TIMEOUT = 240
DEFAULT_RENDER_TIMEOUT = 150
DEFAULT_POSTING_CHARS = 6000
DEFAULT_MAX_JOBS = 4
DEFAULT_TRIM_ATTEMPTS = 8
DEFAULT_CONTENT_RETRIES = 1


class ConfigError(RuntimeError):
    """Raised when a config file exists but cannot be used."""


@dataclass(frozen=True)
class Config:
    """A parsed ``cv.toml`` (or the empty default when there is none)."""

    path: Path | None = None
    root: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.path is not None

    def table(self, name: str) -> dict[str, Any]:
        """One ``[table]``; an empty dict when the table is absent."""
        value = self.data.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self._fail(f"[{name}] must be a table, not {type(value).__name__}")
        return value

    def text(self, key: str, default: str | None = None, *, section: str | None = None) -> str | None:
        """A string value from the top level or from ``[section]``."""
        table = self.data if section is None else self.table(section)
        label = key if section is None else f"[{section}] {key}"
        value = table.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            self._fail(f"{label} must be a string, not {type(value).__name__}")
        return value

    def integer(self, key: str, default: int, *, section: str | None = None) -> int:
        """An integer value from the top level or from ``[section]``."""
        table = self.data if section is None else self.table(section)
        label = key if section is None else f"[{section}] {key}"
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail(f"{label} must be an integer, not {value!r}")
        return value

    def text_list(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """A top-level list of strings (``languages``)."""
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self._fail(f"{key} must be a list of strings, not {value!r}")
        return tuple(value)

    def string_list(self, key: str, *, section: str | None = None) -> tuple[str, ...] | None:
        """A list of strings (top level or ``[section]``); ``None`` when absent."""
        table = self.data if section is None else self.table(section)
        label = key if section is None else f"[{section}] {key}"
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self._fail(f"{label} must be a list of strings, not {value!r}")
        return tuple(value)

    def path_value(self, key: str, *, section: str | None = None) -> Path | None:
        """A path value (top level or ``[section]``); relative to the job root."""
        text = self.text(key, None, section=section)
        return resolve_path(text, self.root) if text else None

    def _fail(self, problem: str) -> None:
        where = self.path if self.path is not None else "(no config file)"
        raise ConfigError(f"{where}: {problem}")


def config_path(root: str | os.PathLike[str], explicit: str | os.PathLike[str] | None = None) -> Path:
    """The configuration file: ``--config`` -> ``$CV_CONFIG`` -> ``<root>/config/cv.toml``."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    return Path(root) / "config" / CONFIG_FILENAME


def load_config(
    root: str | os.PathLike[str], explicit: str | os.PathLike[str] | None = None
) -> Config:
    """Load ``cv.toml``; an empty :class:`Config` when there is no file.

    Raises :class:`ConfigError` when the file exists but cannot be read or
    parsed.  Paths inside the file are resolved against ``root``.
    """
    root_path = Path(root).expanduser()
    path = config_path(root_path, explicit)
    if not path.is_file():
        return Config(root=root_path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib always returns a table
        raise ConfigError(f"{path}: top-level TOML value must be a table")
    return Config(path=path, root=root_path, data=data)


def resolve_path(value: str, root: Path | None) -> Path:
    """A path from the config file: absolute as given, else against the job root."""
    path = Path(value).expanduser()
    if path.is_absolute() or root is None:
        return path
    return Path(root) / path


#: Default locations below the job root (overridable by ``cv.toml``).
DEFAULT_PROFILE_DIR = "profile"
DEFAULT_APPLICATIONS_DIR = "applications"
DEFAULT_STATE_DIR = "state"
DEFAULT_GENERATOR_PROJECT = "tools/cv_generator"
DEFAULT_BACKLOG_TEMPLATE = "backlogs/career-knowledge-backlog-{language}.md"


@dataclass(frozen=True)
class TargetNaming:
    """How the CV file of one folder is named.

    ``name`` is the profile slug (``name_slug`` of first and last name) and
    ``template`` is the ``cv.toml`` ``target_template``.  The whole object is
    built once at start-up and passed into the folder scan, because the scan
    names targets *before* any CV data is read.
    """

    name: str
    template: str = DEFAULT_TARGET_TEMPLATE

    def filename(self, folder: str, language: str) -> str:
        """Exact PDF basename for a folder basename (spaces/Unicode preserved)."""
        try:
            return self.template.format(
                name=self.name, language=language.upper(), folder=folder
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                f"target_template {self.template!r} cannot be filled in: {exc}; "
                f"it may use {{name}}, {{language}} and {{folder}}"
            ) from exc

    def path(self, folder_dir: Path, language: str) -> Path:
        return folder_dir / self.filename(folder_dir.name, language)


def name_slug(first_name: str, last_name: str) -> str:
    """The ``{name}`` slug: first name + last name with all whitespace removed.

    Unicode and hyphens are preserved on purpose (``Ünïcode Ø`` stays readable),
    only whitespace disappears: ``("Ada", "Lovelace") -> "AdaLovelace"``.  Your
    own names live in the profile, never in the code, and produce your CV file
    names exactly (``{name}`` in the task's ``target_template``).
    """
    return "".join(f"{first_name}{last_name}".split())


def find_job_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the job root: explicit argument, environment, else the CWD."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_JOB_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


@dataclass(frozen=True)
class Paths:
    """Resolved input/output locations for one invocation."""

    job_root: Path
    applications: Path
    profile_dir: Path
    backlog_de: Path
    backlog_en: Path
    generator_project: Path
    state_dir: Path

    @property
    def profile_inputs(self) -> Path:
        """The private CV inputs that supply the immutable baseline facts."""
        return self.profile_dir / "inputs"

    @property
    def inputs(self) -> Path:
        return self.state_dir / "inputs"

    @property
    def runs(self) -> Path:
        return self.state_dir / "runs"

    @property
    def staging(self) -> Path:
        return self.state_dir / "staging"

    @property
    def replaced(self) -> Path:
        return self.state_dir / "replaced"

    def ensure(self) -> None:
        for path in (self.inputs, self.runs, self.staging, self.replaced):
            path.mkdir(parents=True, exist_ok=True)


def build_paths(
    *,
    job_root: str | os.PathLike[str] | None = None,
    applications: str | os.PathLike[str] | None = None,
    backlog_de: str | os.PathLike[str] | None = None,
    backlog_en: str | os.PathLike[str] | None = None,
    profile_dir: str | os.PathLike[str] | None = None,
    generator_project: str | os.PathLike[str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    config: Config | None = None,
) -> Paths:
    """Resolve every location: explicit argument -> ``cv.toml`` -> default."""
    root = find_job_root(job_root)
    if config is None:
        config = load_config(root)
    elif config.root is None:  # a Config built by hand in a test
        config = Config(path=config.path, root=root, data=config.data)

    def chosen(explicit, from_config: Path | None, default: Path) -> Path:
        if explicit:
            return Path(explicit).expanduser().resolve()
        if from_config is not None:
            return from_config.expanduser().resolve()
        return default

    profile = chosen(
        profile_dir,
        config.path_value("profile_dir", section="paths"),
        root / DEFAULT_PROFILE_DIR,
    )
    backlog_default = lambda language: profile / "backlogs" / f"career-knowledge-backlog-{language}.md"
    return Paths(
        job_root=root,
        applications=chosen(
            applications,
            config.path_value("applications_dir", section="paths"),
            root / DEFAULT_APPLICATIONS_DIR,
        ),
        profile_dir=profile,
        backlog_de=chosen(
            backlog_de, config.path_value("backlog_de", section="paths"), backlog_default("DE")
        ),
        backlog_en=chosen(
            backlog_en, config.path_value("backlog_en", section="paths"), backlog_default("EN")
        ),
        generator_project=chosen(
            generator_project,
            config.path_value("generator_project"),
            root / DEFAULT_GENERATOR_PROJECT,
        ),
        state_dir=chosen(state_dir, config.path_value("state_dir", section="paths"), root / DEFAULT_STATE_DIR),
    )


def agent_command_from_env() -> list[str] | None:
    """``$CV_AGENT_COMMAND`` split like a shell command; ``None`` when unset.

    The value is the command of a CLI agent (the ``[agent] command`` key as one
    string).  A malformed value is a usage error.
    """
    raw = os.environ.get(ENV_AGENT_COMMAND)
    if raw is None or not raw.strip():
        return None
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ConfigError(f"${ENV_AGENT_COMMAND} is not a valid command: {exc}") from exc


def agent_format_from_env() -> str | None:
    """``$CV_AGENT_FORMAT``; ``None`` when unset (the config or the default wins)."""
    raw = os.environ.get(ENV_AGENT_FORMAT)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


LANGUAGES = ("de", "en")


def normalize_language(value: str | None, allowed: tuple[str, ...] = LANGUAGES) -> str | None:
    """Normalize a language code against ``allowed``.

    ``None`` -> the first allowed language; a blank string -> the first allowed
    language (the prompt's "blank means DE"); a known code -> lowercase it;
    anything else -> ``None``.  ``allowed`` comes from ``cv.toml`` ``languages``
    (default ``("de", "en")``).
    """
    codes = tuple(code.lower() for code in allowed) or LANGUAGES
    if value is None:
        return codes[0]
    text = value.strip().lower()
    if text == "":
        return codes[0]
    return text if text in codes else None
