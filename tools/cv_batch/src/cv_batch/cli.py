"""Command line interface: ``./cv`` (interactive) and non-interactive flags.

Interactive input is read through :mod:`cv_batch.tty_input` on a real terminal so
the typed text looks like the one in the ``job`` CLI; pipes, redirects,
``--plain``/``--quiet`` and any prompt_toolkit failure keep the plain reader.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass

from . import __version__
from . import apps as apps_mod
from . import tty_input
from .config import (
    CONFIG_GENERATOR_TEMPLATE,
    DEFAULT_CONTENT_RETRIES,
    DEFAULT_HEADLINE,
    DEFAULT_LAYOUT,
    DEFAULT_MAX_BULLETS_PER_ENTRY,
    DEFAULT_MAX_BULLETS_TOTAL,
    DEFAULT_MAX_HEADLINE,
    DEFAULT_MAX_JOBS,
    DEFAULT_MODEL,
    DEFAULT_MODEL_TIMEOUT,
    DEFAULT_POSTING_CHARS,
    DEFAULT_RENDER_TIMEOUT,
    DEFAULT_TARGET_TEMPLATE,
    DEFAULT_TRIM_ATTEMPTS,
    LANGUAGES,
    Config,
    ConfigError,
    TargetNaming,
    build_paths,
    default_prime_agent,
    find_job_root,
    load_config,
    name_slug,
    normalize_language,
)
from .catalog import CatalogError, backlog_for, baseline_for, build_catalog, load_identity
from .prompt import Limits
from .runner import BatchInterrupted, Runner, run_batch
from .selection import SelectionError, parse_selection
from .sessions import SESSION_MODES, ModelPolicy, SessionPolicy

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130

#: explicit bounds for user-supplied limits (F10).  Defaults stay well inside.
MAX_CONTENT_RETRIES = 3
MAX_TRIM_ATTEMPTS = 32
MAX_TIMEOUT_SECONDS = 3600.0
MAX_POSTING_CHARS = 200_000
MAX_BULLETS_LIMIT = 50
MAX_HEADLINE_LIMIT = 200

MODE_HELP = """Session and throughput modes (documented trade-offs):

  fresh (default)  One new tool-owned session per job, run sequentially.
                   Predictable and isolated; no cross-job history.
  reuse            ONE tool-owned session is created for the first job and
                   resumed (--resume <id> --session-dir <tool dir>) for every
                   later job in the same batch, always sequentially.  Reuse is
                   NOT a token promise: the transcript grows with every job and
                   the provider decides how much of it is cached.  A live
                   session is never shared between parallel jobs.
  none             --no-session: nothing is saved anywhere.

  --jobs N (N>1)   Bounded parallel fresh sessions.  This mainly saves wall
                   time.  Token use stays roughly the same and may grow.
                   Reuse is rejected together with --jobs > 1.

Selection scope:

  --folder NAME        restrict the run to applications/<NAME> (repeatable).
                       The name must be the bare basename of an existing usable
                       folder; an unknown or unusable name is refused instead of
                       falling back to every folder.

Renderer layout:

  --layout NAME        forwarded verbatim to the renderer
                       (cvgen render --layout NAME).  Default: classic, the
                       generator's built-in personal layout.  Any layout the
                       generator - or a third-party package installed for it -
                       provides can be named; the name is never checked here,
                       so an unknown one fails as a render error.  It does not
                       change the output file name.

Model policy:

  deepseek (default)   DeepSeek Flash only, never Pro
                       (deepseek/deepseek-v4-flash).  No fallback: a recognised
                       quota/rate-limit, availability or authentication failure
                       fails the job.
  auto                 Try Astra (openai-codex/gpt-6-astra) first.  Switch to
                       DeepSeek Flash (deepseek/deepseek-v4-flash) only when
                       the call fails with a recognised quota/rate-limit,
                       availability or authentication problem, then keep using
                       Flash for the rest of the batch.  Malformed JSON and
                       validation problems never trigger a switch; they are
                       retried at most --content-retries times with the
                       validator message.
  astra                Astra only.  No fallback.

  There is no separate quota probe and no paid retry loop.  Token counters are
  reported only when the CLI exposes them in its JSON event stream.

Existing CV files:

  An expected CV file that is already a valid PDF is skipped and is never
  overwritten by default (also not by a parallel job: placement is a no-clobber
  link).  --overwrite adds those existing targets to the numbered selection so
  they can be replaced deliberately, and copies each old file into
  <state>/replaced before the atomic replacement.  Each replacement re-checks
  that the target is still the exact file that was inspected and still a PDF, so
  a target that changed in between is refused, not overwritten.  --overwrite
  also covers the safe invalid targets (same as --regenerate-invalid).
  Symlinked and directory targets are never selectable.

Existing-but-invalid targets:

  An expected CV file that exists but is not a valid PDF (bad header, empty,
  directory, symlink) is never touched by default, and the CLI says so instead
  of claiming that every CV is already valid.  --regenerate-invalid adds the
  *safe* invalid targets (regular, non-symlink files) to the numbered selection,
  clearly labelled, and copies the old file into <state>/replaced before the
  atomic replacement.  Symlinked and directory targets are never selectable.

Input bounds (rejected before anything runs):

  --jobs 1..4, --content-retries 0..3, --trim-attempts 1..32; --timeout and
  --render-timeout must be finite, positive and at most 3600 seconds; the
  bullet, headline and posting limits have explicit upper bounds.
"""


@dataclass
class Console:
    plain: bool = False

    def _c(self, code: str, text: str, *, stream=None) -> str:
        """Wrap a whole line in one colour code, unless colouring is off.

        The tty-ness of the stream the text is actually written to decides, so
        ``cv 2>file`` cannot capture escape sequences meant for a terminal.
        """
        target = stream if stream is not None else sys.stdout
        try:
            if self.plain or not target.isatty():
                return text
        except (AttributeError, OSError, ValueError):  # pragma: no cover - closed stream
            return text
        return f"\033[{code}m{text}\033[0m"

    def info(self, text: str = "") -> None:
        print(text)

    def warn(self, text: str) -> None:
        print(self._c("33", f"warn: {text}"))

    def error(self, text: str) -> None:
        print(self._c("31", f"error: {text}", stream=sys.stderr), file=sys.stderr)

    def good(self, text: str) -> None:
        print(self._c("32", text))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cv",
        description=(
            "Tailor one one-page CV per selected job posting and render it with the "
            "existing local CV generator. The model only picks candidate IDs; the "
            "Python pipeline validates and renders."
        ),
        epilog=MODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"cv {__version__}")
    parser.add_argument("--explain-modes", action="store_true", help="print the mode/fallback documentation and exit")

    sel = parser.add_argument_group("selection")
    sel.add_argument("--language", metavar="de|en", help="CV language; blank entry in the prompt means DE")
    sel.add_argument(
        "--folder",
        action="append",
        metavar="NAME",
        default=None,
        help=(
            "restrict the run to this applications/<NAME> folder (repeatable); the name is the "
            "folder's basename and an unknown or unusable name is refused instead of falling "
            "back to every folder"
        ),
    )
    sel.add_argument("--select", metavar="LIST", help="non-interactive selection, e.g. 1,3-5,12")
    sel.add_argument(
        "--all",
        action="store_true",
        help=(
            "select every listed candidate: missing CVs, plus existing ones that "
            "--overwrite or --regenerate-invalid made selectable"
        ),
    )
    sel.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "allow replacing existing CV files: existing valid PDFs (and the safe invalid "
            "ones) become numbered candidates, and each old file is backed up into the tool "
            "state directory before the atomic replacement"
        ),
    )
    sel.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    sel.add_argument("--dry-run", action="store_true", help="scan, select and print the plan; write nothing")

    run = parser.add_argument_group("execution")
    run.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help=(
            f"parallel fresh sessions (1..{DEFAULT_MAX_JOBS}; the upper bound comes from "
            f"cv.toml [limits] max_jobs)"
        ),
    )
    run.add_argument("--session-mode", choices=SESSION_MODES, default="fresh")
    run.add_argument(
        "--model",
        choices=("auto", "astra", "deepseek"),
        default=None,
        help=(
            f"model policy (default from cv.toml 'model', else {DEFAULT_MODEL} = DeepSeek Flash only; "
            "auto = Astra first with DeepSeek Flash fallback; astra = Astra only)"
        ),
    )
    run.add_argument("--thinking", metavar="LEVEL", default=None, help="prime-agent reasoning level")
    run.add_argument("--model-command", metavar="EXE", default=None, help="prime-agent executable (default: prime-agent)")
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"model timeout; default from cv.toml [limits] model_timeout, else {DEFAULT_MODEL_TIMEOUT}",
    )
    run.add_argument(
        "--content-retries",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"paid model retries after a rejected answer (0..{MAX_CONTENT_RETRIES}); default from "
            f"cv.toml [limits] content_retries, else {DEFAULT_CONTENT_RETRIES}"
        ),
    )
    run.add_argument("--no-trim", action="store_true", help="never drop bullets to fit one page (overflow then fails)")
    run.add_argument(
        "--trim-attempts",
        type=int,
        default=None,
        metavar="N",
        help=f"default from cv.toml [limits] trim_attempts, else {DEFAULT_TRIM_ATTEMPTS}",
    )
    run.add_argument(
        "--regenerate-invalid",
        action="store_true",
        help=(
            "explicitly allow replacing invalid target files: safe (regular, non-symlink) "
            "invalid targets are added to the numbered selection and the old file is backed up"
        ),
    )
    run.add_argument("--quiet", action="store_true")
    run.add_argument(
        "--plain",
        action="store_true",
        help="no ANSI colours and plain input (default when stdout is not a TTY)",
    )

    paths = parser.add_argument_group("paths (defaults follow the job root)")
    paths.add_argument("--root", metavar="DIR", help="job root (default: $CV_JOB_ROOT or the current directory)")
    paths.add_argument("--applications-dir", metavar="DIR")
    paths.add_argument("--backlog-de", metavar="FILE")
    paths.add_argument("--backlog-en", metavar="FILE")
    paths.add_argument("--generator-project", metavar="DIR", help="uv project of the existing CV generator")
    paths.add_argument("--generator-exe", metavar="EXE", help="renderer executable override (same CLI contract)")
    # The layout belongs to the renderer contract, so it is documented next to
    # the renderer executable.  The value stays opaque: the generator's layout
    # registry is the single source of truth and may be extended by a
    # third-party package, so no local list is enforced here.
    paths.add_argument(
        "--layout",
        metavar="NAME",
        default=DEFAULT_LAYOUT,
        help=(
            "renderer layout forwarded verbatim to `cvgen render --layout` "
            f"(default: {DEFAULT_LAYOUT}, the built-in personal layout; "
            "an unknown name fails as a render error)"
        ),
    )
    paths.add_argument("--state-dir", metavar="DIR", help="tool-owned private state (sessions, saved inputs)")
    paths.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help=(
            "configuration file (default: $CV_CONFIG, else <job root>/config/cv.toml); "
            "every unset option below falls back to it and then to its built-in default"
        ),
    )

    lim = parser.add_argument_group("limits")
    lim.add_argument("--max-bullets-per-entry", type=int, default=None)
    lim.add_argument("--max-bullets-total", type=int, default=None)
    lim.add_argument("--max-headline", type=int, default=None)
    lim.add_argument("--max-posting-chars", type=int, default=None)
    lim.add_argument("--render-timeout", type=float, default=None, metavar="SECONDS")
    return parser


def _apply_config_defaults(args, config: Config) -> None:
    """Fill every option the user did not set from ``cv.toml``, then its default.

    Precedence everywhere: CLI flag -> environment variable (where one exists)
    -> ``cv.toml`` -> built-in default.  ``build_paths`` applies the same rule to
    the directory options.
    """
    limits = "limits"
    args.model = args.model or config.text("model", DEFAULT_MODEL)
    args.layout = args.layout or config.text(CONFIG_GENERATOR_TEMPLATE, DEFAULT_LAYOUT)
    # ``max_jobs`` is the upper bound for ``--jobs`` (whose own default stays 1),
    # not a default parallelism: a config file must never start a 4-way batch
    # behind the user's back.
    args.max_jobs = config.integer("max_jobs", DEFAULT_MAX_JOBS, section=limits)
    args.timeout = (
        args.timeout
        if args.timeout is not None
        else config.integer("model_timeout", DEFAULT_MODEL_TIMEOUT, section=limits)
    )
    args.render_timeout = (
        args.render_timeout
        if args.render_timeout is not None
        else config.integer("render_timeout", DEFAULT_RENDER_TIMEOUT, section=limits)
    )
    args.content_retries = (
        args.content_retries
        if args.content_retries is not None
        else config.integer("content_retries", DEFAULT_CONTENT_RETRIES, section=limits)
    )
    args.trim_attempts = (
        args.trim_attempts
        if args.trim_attempts is not None
        else config.integer("trim_attempts", DEFAULT_TRIM_ATTEMPTS, section=limits)
    )
    args.max_bullets_per_entry = (
        args.max_bullets_per_entry
        if args.max_bullets_per_entry is not None
        else config.integer("bullets_per_entry", DEFAULT_MAX_BULLETS_PER_ENTRY, section=limits)
    )
    args.max_bullets_total = (
        args.max_bullets_total
        if args.max_bullets_total is not None
        else config.integer("bullets_total", DEFAULT_MAX_BULLETS_TOTAL, section=limits)
    )
    args.max_headline = (
        args.max_headline
        if args.max_headline is not None
        else config.integer("headline_chars", DEFAULT_MAX_HEADLINE, section=limits)
    )
    args.max_posting_chars = (
        args.max_posting_chars
        if args.max_posting_chars is not None
        else config.integer("posting_chars", DEFAULT_POSTING_CHARS, section=limits)
    )


def _config_languages(config: Config) -> tuple[str, ...]:
    """The languages ``--language`` accepts (``cv.toml`` ``languages``)."""
    return tuple(code.lower() for code in config.text_list("languages", LANGUAGES)) or LANGUAGES


def _target_template(config: Config) -> str:
    return config.text("target_template", DEFAULT_TARGET_TEMPLATE) or DEFAULT_TARGET_TEMPLATE


def _headline_override(config: Config) -> str | None:
    """``cv.toml`` ``headline``; empty means "take it from the profile"."""
    value = config.text("headline", DEFAULT_HEADLINE) or DEFAULT_HEADLINE
    return value.strip() or None


def _validate_limits(args) -> list[str]:
    """Return human-readable errors for out-of-range user-supplied limits (F10)."""
    errors: list[str] = []
    for name, value in (("--timeout", args.timeout), ("--render-timeout", args.render_timeout)):
        finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not finite or value <= 0 or value > MAX_TIMEOUT_SECONDS:
            errors.append(
                f"{name} must be a finite number of seconds between 0 (exclusive) and {MAX_TIMEOUT_SECONDS:g}"
            )
    bounds = (
        ("--content-retries", args.content_retries, 0, MAX_CONTENT_RETRIES),
        ("--trim-attempts", args.trim_attempts, 1, MAX_TRIM_ATTEMPTS),
        ("--max-bullets-per-entry", args.max_bullets_per_entry, 1, MAX_BULLETS_LIMIT),
        ("--max-bullets-total", args.max_bullets_total, 1, MAX_BULLETS_LIMIT),
        ("--max-headline", args.max_headline, 1, MAX_HEADLINE_LIMIT),
        ("--max-posting-chars", args.max_posting_chars, 1, MAX_POSTING_CHARS),
    )
    for name, value, low, high in bounds:
        if not isinstance(value, int) or value < low or value > high:
            errors.append(f"{name} must be an integer between {low} and {high}")
    return errors


def _coloured_input_allowed(console: Console) -> bool:
    """Whether the typed text may be rendered with the shared ``answer`` colour.

    Only a real interactive terminal qualifies: a pipe, a redirect, a test
    harness or ``--plain``/``--quiet`` keeps the plain reader (and no ANSI).
    """
    if console.plain:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):  # pragma: no cover - closed stream
        return False


def _ask(prompt: str, console: Console) -> str | None:
    """Read one line; ``None`` means EOF or Ctrl-C (both cancel safely).

    On a real terminal the typed text is painted orange+bold like in the ``job``
    CLI (see :mod:`cv_batch.tty_input`); every other case - pipe, redirect,
    ``--plain``/``--quiet``, or a launcher that cannot start - reads through the
    plain :func:`input`, so behaviour and output stay exactly as before.  The
    returned value is always the raw line, unchanged; nothing is stripped here.
    """
    if _coloured_input_allowed(console):
        try:
            return tty_input.read_coloured_line(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        except Exception:
            # Defensive: a broken prompt_toolkit setup (or a terminal it cannot
            # drive) must never take the CLI down; fall back to the plain reader.
            pass
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _choose_language(args, console: Console, languages: tuple[str, ...] = LANGUAGES) -> int | None:
    """Ask for the CV language, restricted to the configured ``languages``."""
    first = languages[0]
    accepted = " or ".join(f"'{code}'" for code in languages)
    if args.language is not None:
        language = normalize_language(args.language, languages)
        if language is None:
            console.error(f"--language accepts only {accepted}")
            return EXIT_USAGE
        return language
    for _ in range(3):
        answer = _ask(f"Language [{first.upper()}/{', '.join(c for c in languages[1:])}] (blank = {first.upper()}): ", console)
        if answer is None:
            return EXIT_CANCELLED
        language = normalize_language(answer, languages)
        if language is not None:
            return language
        accepted = " or ".join(f"'{code}'" for code in languages)
        console.warn(f"only {accepted} is accepted (blank means {first.upper()})")
    console.error("no valid language after three attempts")
    return EXIT_USAGE


#: One plain-language line per ``--model`` choice, used by the plan and the confirm prompt.
_MODEL_DESCRIPTIONS = {
    "deepseek": "DeepSeek Flash only (deepseek/deepseek-v4-flash; never Pro), no fallback",
    "auto": "Astra first (openai-codex/gpt-6-astra), DeepSeek Flash on a recognised failure",
    "astra": "Astra only (openai-codex/gpt-6-astra), no fallback",
}


def _model_description(choice: str) -> str:
    return _MODEL_DESCRIPTIONS.get(choice, choice)


@dataclass(frozen=True)
class FolderScope:
    """A validated ``--folder`` restriction (the *scope* of one run)."""

    #: requested names, deduplicated, in the order the user passed them.
    names: tuple[str, ...]
    #: usable folders found by the scan *before* the restriction, i.e. the ``m``
    #: of the plan's ``n of m usable folders in scope``.
    total_usable: int


def _has_path_separator(name: str) -> bool:
    """True for anything that is a path (or a drive-relative name), not a basename."""
    return "/" in name or os.sep in name or (os.altsep is not None and os.altsep in name)


def _validate_folder_names(console: Console, scan, requested: list[str], applications_dir) -> tuple[list[str], bool]:
    """Check every requested ``--folder`` NAME against the scan.

    Prints one ``error:`` line per problem and returns ``(names, ok)``; ``names``
    is deduplicated, first occurrence first.  A name is accepted only when the
    scan found a *usable* folder with exactly that basename.  Unknown names and
    names the scan refused (see :func:`cv_batch.apps.scan`) are refused here too,
    with the scan's own reason, so a typo can never widen a run to every folder.
    Every problem is reported before anything else runs.
    """
    reasons = {entry.folder: entry.reason for entry in (*scan.skipped, *scan.unusable)}
    usable = {app.folder for app in scan.applications}
    names: list[str] = []
    seen: set[str] = set()
    ok = True
    for name in requested:
        if name in seen:
            continue
        seen.add(name)
        if not name.strip() or name in {".", ".."} or name.startswith(".") or _has_path_separator(name):
            console.error(f"--folder must be a bare folder name below {applications_dir} (got '{name}')")
            ok = False
        elif name in usable:
            names.append(name)
        elif name in reasons:
            console.error(f"--folder {name}: {reasons[name]}")
            ok = False
        else:
            console.error(f"--folder {name}: no such folder under {applications_dir}")
            ok = False
    return names, ok


def _restrict_scan(scan, names: list[str]) -> apps_mod.ScanResult:
    """Restrict a scan to the requested folder basenames, keeping the scan order.

    Only the requested scope is kept, including its ignored/unusable counts, so
    the plan and the warnings describe this run and not unrelated folders.
    """
    wanted = set(names)
    return apps_mod.ScanResult(
        applications=[app for app in scan.applications if app.folder in wanted],
        skipped=[entry for entry in scan.skipped if entry.folder in wanted],
        unusable=[entry for entry in scan.unusable if entry.folder in wanted],
    )


def _print_plan(
    console: Console,
    args,
    paths,
    language: str,
    scan,
    missing,
    invalid,
    valid,
    *,
    allow_replace: bool,
    allow_overwrite: bool,
    scope: FolderScope | None = None,
) -> None:
    console.info(f"Job root       : {paths.job_root}")
    console.info(f"Applications   : {paths.applications}")
    console.info(f"Profile        : {paths.profile_dir}")
    console.info(f"Generator      : {paths.generator_project}")
    console.info(f"State (private): {paths.state_dir}")
    console.info(f"Language       : {language.upper()}")
    suffix = " (default)" if args.model == DEFAULT_MODEL else ""
    console.info(f"Model          : {args.model}{suffix} - {_model_description(args.model)}")
    console.info(f"Session mode   : {args.session_mode}   parallel jobs: {args.jobs}")
    layout_suffix = " (default)" if args.layout == DEFAULT_LAYOUT else ""
    console.info(f"Layout         : {args.layout}{layout_suffix}   (renderer --layout)")
    console.info(
        f"Posting folders: {len(scan.applications)} usable, {len(scan.skipped)} ignored, "
        f"{len(scan.unusable)} unusable"
    )
    if scope is not None:
        console.info(
            f"Folder filter  : {', '.join(scope.names)}   "
            f"({len(scan.applications)} of {scope.total_usable} usable folders in scope)"
        )
    console.info(f"Missing CVs    : {len(missing)}")
    if valid:
        console.info(
            f"Existing CVs   : {len(valid)} valid "
            + (
                "[--overwrite: selectable for replacement; each old file is backed up first]"
                if allow_overwrite
                else "[not numbered; pass --overwrite to replace an existing CV]"
            )
        )
    if invalid:
        replaceable = sum(1 for _, state in invalid if state.replaceable)
        console.info(
            f"Invalid targets: {len(invalid)} ({replaceable} replaceable) "
            + (
                "[--regenerate-invalid: the safe ones are selectable for replacement]"
                if allow_replace
                else "[not numbered; never overwritten silently]"
            )
        )


def _render_candidates(console: Console, candidates, invalid, *, allow_replace: bool, allow_overwrite: bool) -> None:
    """Print the numbered list.

    ``candidates`` is ``[(app, kind, state|None), ...]``; existing targets appear
    only when an explicit opt-in added them: invalid ones with
    ``--regenerate-invalid`` (or ``--overwrite``), valid ones with ``--overwrite``.
    Each section prints the same index as the selection the user types.
    """
    numbered = list(enumerate(candidates, start=1))
    console.info("")
    console.info("Missing tailored CVs:")
    if not any(kind == "missing" for _, (_app, kind, _state) in numbered):
        console.info("  (none)")
    for index, (app, kind, _state) in numbered:
        if kind == "missing":
            console.info(f"  {index:>3}. {app.folder}")
    if allow_replace or allow_overwrite:
        replaceable = [
            (index, app, state) for index, (app, kind, state) in numbered
            if kind == "invalid" and state is not None and state.replaceable
        ]
        if replaceable:
            console.info("")
            console.info(
                "Replaceable invalid targets (explicit opt-in; the old file is backed up first):"
            )
            for index, app, state in replaceable:
                console.info(f"  {index:>3}. {app.folder}  [REPLACE: {state.reason}]")
    if allow_overwrite:
        existing = [(index, app) for index, (app, kind, _state) in numbered if kind == "valid"]
        if existing:
            console.info("")
            console.info(
                "Existing CVs (explicit --overwrite; each old file is backed up into the "
                "tool state directory first):"
            )
            for index, app in existing:
                console.info(f"  {index:>3}. {app.folder}  [OVERWRITE: valid CV already exists]")
    if invalid:
        blocked = [(app, state) for app, state in invalid if not state.replaceable]
        if blocked:
            console.info("")
            console.info("Never replaceable (symlink or directory target; not selectable at all):")
            for app, state in blocked:
                console.info(f"       {app.folder}: {state.reason}")
        if not (allow_replace or allow_overwrite):
            console.info("")
            console.info("Existing target files that are not valid PDFs (not numbered; nothing is replaced):")
            for app, state in invalid:
                console.info(f"       {app.folder}: {state.reason}")
            console.info(
                "       (pass --regenerate-invalid to make the safe ones selectable for replacement)"
            )


def _select_interactive(console: Console, candidates) -> list[int] | None:
    for _ in range(3):
        answer = _ask(
            "Select (e.g. 1,3-5,12; 'all' selects every listed CV; blank cancels): ",
            console,
        )
        if answer is None:
            return None
        try:
            picked = parse_selection(answer, len(candidates))
        except SelectionError as exc:
            console.error(str(exc))
            continue
        if not picked:
            console.info("cancelled: nothing selected")
            return None
        return picked
    console.error("no valid selection after three attempts")
    return None


def _confirm(
    console: Console, count: int, language: str, args, *, replacing: int = 0, overwriting: int = 0
) -> bool | None:
    console.info("")
    console.info(f"Ready to tailor {count} CV(s) in {language.upper()}.")
    if replacing:
        console.info(
            f"This replaces {replacing} existing invalid target file(s); "
            f"each old file is backed up under the tool state directory first."
        )
    if overwriting:
        console.info(
            f"This overwrites {overwriting} existing CV file(s); "
            f"each old file is backed up under the tool state directory first."
        )
    console.info(
        f"Model: {args.model} - {_model_description(args.model)}   "
        f"Session mode: {args.session_mode}   Parallel jobs: {args.jobs}   "
        f"Layout: {args.layout}"
    )
    answer = _ask("Proceed? [y/N]: ", console)
    if answer is None:
        return None
    return answer.strip().lower() in {"y", "yes", "j", "ja"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(plain=args.plain or args.quiet)

    if args.explain_modes:
        print(MODE_HELP)
        return EXIT_OK

    # The configuration file comes first: its values fill every option the user
    # did not set, including the directory options ``build_paths`` resolves.
    try:
        config = load_config(find_job_root(args.root), args.config)
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    try:
        _apply_config_defaults(args, config)
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    languages = _config_languages(config)

    if args.jobs < 1 or args.jobs > args.max_jobs:
        console.error(f"--jobs must be between 1 and {args.max_jobs}")
        return EXIT_USAGE
    if args.jobs > 1 and args.session_mode == "reuse":
        console.error("--session-mode reuse cannot be combined with --jobs > 1 (a live session is never shared)")
        return EXIT_USAGE
    limit_errors = _validate_limits(args)
    if limit_errors:
        for message in limit_errors:
            console.error(message)
        return EXIT_USAGE

    try:
        paths = build_paths(
            job_root=args.root,
            applications=args.applications_dir,
            backlog_de=args.backlog_de,
            backlog_en=args.backlog_en,
            generator_project=args.generator_project,
            state_dir=args.state_dir,
            config=config,
        )
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except Exception as exc:  # pragma: no cover - defensive
        console.error(f"cannot resolve paths: {exc}")
        return EXIT_USAGE

    # ``--folder`` is validated before anything else can happen - including the
    # language prompt - against the full, read-only scan: an unknown or unusable
    # name must be refused, never silently widened to every folder.  Only this
    # new option moves the scan ahead of the prompt; without it nothing changes.
    scan = None
    scope: FolderScope | None = None
    if args.folder:
        scan = apps_mod.scan(paths.applications)
        names, ok = _validate_folder_names(console, scan, args.folder, paths.applications)
        if not ok:
            return EXIT_USAGE
        scope = FolderScope(tuple(names), len(scan.applications))
        scan = _restrict_scan(scan, names)

    language = _choose_language(args, console, languages)
    if isinstance(language, int):
        if language == EXIT_CANCELLED:
            console.info("cancelled")
        return language
    assert isinstance(language, str)

    if scan is None:
        scan = apps_mod.scan(paths.applications)
    if not scan.applications:
        if scope is not None:
            console.error(
                f"no usable posting folders in scope for --folder {', '.join(scope.names)} "
                f"under {paths.applications} "
                f"(a usable folder is a real directory with a non-empty posting.txt)"
            )
        else:
            console.error(
                f"no usable posting folders found under {paths.applications} "
                f"(a usable folder is a real directory with a non-empty posting.txt)"
            )
        return EXIT_USAGE

    # The profile gives the identity once, here: the loop below names the CV
    # files of every folder, and their ``{name}`` part comes from these values,
    # so it cannot be derived per job or lazily while reading CV data.
    headline_override = _headline_override(config)
    try:
        identity = load_identity(
            language=language,
            baseline_path=baseline_for(language, paths.profile_dir),
            headline_override=headline_override,
        )
    except CatalogError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    naming = TargetNaming(
        name=name_slug(identity.first_name, identity.last_name),
        template=_target_template(config),
    )
    try:
        naming.filename("probe", language)  # a bad template field fails here, not mid-batch
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE

    missing = []
    invalid = []
    valid = []
    for app in scan.applications:
        state = apps_mod.classify_target(naming.path(app.path, language))
        if state.state == apps_mod.MISSING:
            missing.append(app)
        elif state.state == apps_mod.INVALID:
            invalid.append((app, state))
        else:
            valid.append((app, state))

    # F1/overwrite: existing targets become numbered candidates only with an
    # explicit opt-in, and never when the target shape is unsafe
    # (symlink/directory).  ``--regenerate-invalid`` adds the safe invalid ones;
    # ``--overwrite`` adds them too and also the existing valid CVs.
    candidates: list[tuple[object, str, object]] = [(app, "missing", None) for app in missing]
    replaceable_invalid = [(app, state) for app, state in invalid if state.replaceable]
    if args.regenerate_invalid or args.overwrite:
        candidates += [(app, "invalid", state) for app, state in replaceable_invalid]
    if args.overwrite:
        candidates += [(app, "valid", state) for app, state in valid]

    if not args.quiet:
        _print_plan(
            console, args, paths, language, scan, missing, invalid, valid,
            allow_replace=args.regenerate_invalid,
            allow_overwrite=args.overwrite,
            scope=scope,
        )
        if scan.unusable:
            for skip in scan.unusable:
                console.warn(f"{skip.folder}: {skip.reason}")
        if (args.regenerate_invalid or args.overwrite) and not replaceable_invalid and invalid:
            console.warn(
                "no invalid target is replaceable: every one is a symlink or a directory"
            )

    if not candidates:
        if invalid:
            if replaceable_invalid:
                console.info(
                    f"nothing to do by default: no missing {language.upper()} CV, but "
                    f"{len(invalid)} existing target file(s) are invalid "
                    f"({len(replaceable_invalid)} safely replaceable). "
                    f"Pass --regenerate-invalid to select and replace them."
                )
            else:
                console.info(
                    f"nothing to do: no missing {language.upper()} CV, and the {len(invalid)} invalid "
                    f"target(s) cannot be replaced safely (symlink or directory)."
                )
        else:
            hint = (
                " Pass --overwrite to replace an existing CV."
                if valid
                else ""
            )
            console.info(
                f"nothing to do: every usable posting folder already has a valid "
                f"{language.upper()} CV.{hint}"
            )
        return EXIT_OK

    _render_candidates(
        console,
        candidates,
        invalid,
        allow_replace=args.regenerate_invalid,
        allow_overwrite=args.overwrite,
    )

    if args.all:
        picked = list(range(1, len(candidates) + 1))
    elif args.select is not None:
        try:
            picked = parse_selection(args.select, len(candidates))
        except SelectionError as exc:
            console.error(str(exc))
            return EXIT_USAGE
        if not picked:
            console.info("cancelled: nothing selected")
            return EXIT_OK
    else:
        if not sys.stdin.isatty():
            console.error("no selection given and stdin is not a terminal; use --select or --all")
            return EXIT_USAGE
        picked = _select_interactive(console, candidates)
        if picked is None:
            return EXIT_OK

    jobs = [_job(candidates[index - 1][0], language, naming) for index in picked]
    replacing = sum(1 for index in picked if candidates[index - 1][1] == "invalid")
    overwriting = sum(1 for index in picked if candidates[index - 1][1] == "valid")

    if args.dry_run:
        console.info("")
        console.info(f"dry run: {len(jobs)} job(s) would run; nothing was written")
        for index in picked:
            app, kind, _state = candidates[index - 1]
            marker = {
                "invalid": " [REPLACING INVALID TARGET]",
                "valid": " [OVERWRITING EXISTING CV]",
            }.get(kind, "")
            console.info(f"  {app.folder} -> {naming.filename(app.path.name, language)}{marker}")
        try:
            catalog = build_catalog(
                language=language,
                baseline_path=baseline_for(language, paths.profile_dir),
                backlog_path=backlog_for(language, paths.backlog_de, paths.backlog_en),
                headline_override=headline_override,
            )
            console.info(
                f"catalog: {len(catalog.entries)} entries, {catalog.candidate_count()} candidates "
                f"from {catalog.baseline_path.name}"
            )
            for note in catalog.notes:
                console.info(f"note: {note}")
        except CatalogError as exc:
            console.error(f"catalog unavailable: {exc}")
            return EXIT_FAILURES
        return EXIT_OK

    if not args.yes:
        if not sys.stdin.isatty():
            console.error("refusing to start without --yes on a non-interactive stdin")
            return EXIT_USAGE
        confirmed = _confirm(
            console, len(jobs), language, args, replacing=replacing, overwriting=overwriting
        )
        if not confirmed:
            console.info("cancelled")
            return EXIT_OK

    paths.ensure()
    model_policy = ModelPolicy(args.model)
    session_policy = SessionPolicy(args.session_mode, paths.sessions)
    runner = Runner(
        paths=paths,
        language=language,
        model_policy=model_policy,
        session_policy=session_policy,
        exe=args.model_command or default_prime_agent(),
        generator_exe=args.generator_exe,
        layout=args.layout,
        limits=Limits(
            max_bullets_per_entry=args.max_bullets_per_entry,
            max_bullets_total=args.max_bullets_total,
            max_headline=args.max_headline,
        ),
        posting_chars=args.max_posting_chars,
        model_timeout=args.timeout,
        render_timeout=args.render_timeout,
        trimming=not args.no_trim,
        trim_attempts=args.trim_attempts,
        content_retries=args.content_retries,
        allow_replace_invalid=args.regenerate_invalid,
        allow_overwrite=args.overwrite,
        thinking=args.thinking,
        log=console.info,
    )

    summary = None
    try:
        summary = run_batch(runner, jobs, parallel=args.jobs, log=console.info)
    except BatchInterrupted as exc:
        runner.cancel_all()
        partial = exc.summary
        console.warn(
            f"interrupted: remaining jobs were cancelled; partial batch: ok {len(partial.ok)}, "
            f"skipped {len(partial.skipped)}, failed {len(partial.failed)} of {len(jobs)}"
        )
        for result in partial.ok:
            console.info(f"  finished: {result.job.target.name}")
        return EXIT_CANCELLED

    console.info("")
    console.info(
        f"summary: ok {len(summary.ok)}, skipped {len(summary.skipped)}, failed {len(summary.failed)} "
        f"of {len(summary.results)}"
    )
    usage = summary.usage()
    if not usage.empty:
        console.info(usage.describe())
    if model_policy.astra_exhausted:
        console.info(
            f"Astra was unusable ({model_policy.switch_reason}); the batch continued on "
            f"{model_policy.current().selector}"
        )
    for result in summary.failed:
        console.error(f"{result.job.folder}: {result.message}")
    if summary.ok:
        console.good("complete: " + ", ".join(r.job.target.name for r in summary.ok))
    return EXIT_FAILURES if summary.failed else EXIT_OK


def _job(app, language, naming):
    """Build a job that carries the identity recorded during the scan (F8)."""
    from .runner import JobSpec

    return JobSpec.from_application(app, language, naming)


__all__ = ["main", "build_parser"]
