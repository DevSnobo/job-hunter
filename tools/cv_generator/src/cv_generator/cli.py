"""Command line interface for the CV generator.

Usage (from any working directory)::

    cvgen render [input.json] [--layout classic] [--language de] [--output out.pdf]
                 [--overwrite] [--png] [--config FILE] [--profile-dir DIR]
    cvgen layouts [--json]

The input may be given as a path, with ``--language de|en`` (the shortcut for
``<profile>/inputs/current_<lang>.json``), or not at all - then ``cvgen.toml``
``input`` is used.

Default output directory is the private ``exports/generator`` directory of the
repository (``<repo>/exports/generator``, see :mod:`cv_generator.config`), never
the current working directory.  An explicit ``--output`` name is confined to
that directory (absolute paths, ``..`` and other escapes are rejected) unless
``--allow-outside-output`` is given.  Output files are written atomically (temp
file + rename) and existing files are only replaced with an explicit
``--overwrite``.

Template selection (see :func:`select_layout`): ``--layout`` always wins; with
exactly one template discovered it is used automatically; with several, the
active one comes from ``cvgen.toml`` ``[template] name``.  Several templates and
no name is a usage error (exit 2) that lists them and prints the exact config
line to add.

``cvgen layouts`` lists every registered layout with its name, origin,
description and - for a template - the directory it was loaded from, including
layouts provided by third-party packages through the
``cv_generator.layouts`` entry-point group.  An unknown name is a usage error
(exit 2) that names the known layouts; it never prints a traceback.

Every value follows ``CLI flag -> environment variable -> cvgen.toml ->
built-in default``; an unusable ``cvgen.toml`` is a usage error (exit 2) that
names the file and the problem.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pathlib import Path

from . import config as config_module
from . import paths
from .layouts import (
    DEFAULT_LAYOUT,
    ENTRY_POINT_GROUP,
    LayoutError,
    LayoutOptions,
    UnknownLayoutError,
    available_layouts,
    discovery_warnings,
    get_layout_info,
    list_templates,
    render_layout,
)
from .photo import PhotoError
from .schema import CVData

EXIT_USAGE = 2

#: Templates that may be selected without ``--layout`` when no config names one.
SINGLE_TEMPLATE = 1


class OutputPathError(ValueError):
    """Raised when an explicit output path escapes the output directory."""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _render_png_preview(pdf_path: Path, png_path: Path) -> None:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError(
            "PNG preview requires poppler-utils (pdftoppm), which was not found "
            "on PATH."
        )
    subprocess.run(
        [pdftoppm, "-png", "-r", "150", "-singlefile", str(pdf_path), str(png_path.with_suffix(""))],
        check=True,
        capture_output=True,
        text=True,
    )
    produced = png_path.with_suffix(".png")
    if produced != png_path and produced.exists():
        os.replace(produced, png_path)


def resolve_output_path(
    output: str | None, input_path: str, allow_outside: bool = False
) -> Path:
    """Resolve ``--output`` to a path, confined to the output directory.

    - Without ``--output``: ``<output dir>/<input-stem>.pdf``.
    - With ``--output`` and no ``--allow-outside-output``: the name is placed
      under the output directory (relative names are relative to it); absolute
      paths and any ``..``/symlink escape are rejected.
    - With ``--allow-outside-output``: the path is used as given (relative
      paths resolve against the current working directory).
    """
    output_dir = paths.DEFAULT_OUTPUT_DIR.resolve()
    if not output:
        return output_dir / (Path(input_path).stem + ".pdf")

    p = Path(output).expanduser()
    if allow_outside:
        if not p.is_absolute():
            p = Path.cwd() / p
        return p

    out = p.resolve() if p.is_absolute() else (output_dir / p).resolve()
    if not out.is_relative_to(output_dir):
        raise OutputPathError(
            f"output path escapes the output directory: {out} is not inside "
            f"{output_dir}. Generated files must stay inside the output directory; pass "
            f"--allow-outside-output to write elsewhere."
        )
    return out


def resolve_input(args: argparse.Namespace) -> tuple[Path | None, str | None]:
    """The input file for ``render``: argument, ``--language``, or ``cvgen.toml``.

    Returns ``(path, error)``; ``path`` is ``None`` when nothing names an input,
    in which case ``error`` explains the three ways to name one.
    """
    if args.input:
        return Path(args.input).expanduser(), None
    if args.language:
        return paths.default_input_path(args.language), None
    configured = paths.configured_input()
    if configured is not None:
        return configured, None
    return None, (
        "no input given: pass a JSON file, or --language de|en for "
        f"{paths.INPUTS_DIR}/current_<lang>.json, or set 'input' in "
        f"{config_module.config_path(paths.REPO_ROOT)}"
    )


def select_layout(requested: str | None) -> tuple[str | None, str | None]:
    """Resolve the template/layout to render with; returns ``(name, error)``.

    ``--layout`` (``requested``) always wins and is validated later, so an
    unknown name keeps its own error message.  Otherwise exactly one discovered
    template is used automatically; with several, the active one comes from
    ``cvgen.toml`` ``[template] name``; with several and no name, the returned
    error lists them and shows the exact line to add.
    """
    if requested:
        return requested, None

    templates = list_templates()
    if len(templates) == SINGLE_TEMPLATE:
        return templates[0][0], None

    try:
        configured = paths.configured_template()
    except config_module.ConfigError as exc:
        return None, str(exc)
    if configured:
        return configured, None

    config_file = config_module.config_path(paths.REPO_ROOT)
    if not templates:
        return None, (
            f"no template found in {paths.TEMPLATES_DIR} and cvgen.toml names none; "
            f"add a template directory or pass --layout NAME"
        )
    listing = "\n".join(
        f"  {name}  ({origin})  {path}" for name, _description, origin, path in templates
    )
    return None, (
        f"{len(templates)} templates are available, so the active one must be named.\n"
        f"{listing}\n"
        f"add this to {config_file} and run again (--layout NAME works too):\n"
        f"\n"
        f"    [template]\n"
        f'    name = "{templates[0][0]}"'
    )


def render_command(args: argparse.Namespace) -> int:
    layout = args.layout
    if layout is None:  # pragma: no cover - main() resolves it before dispatch
        layout, problem = select_layout(None)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
    input_path, input_problem = resolve_input(args)
    if input_path is None:
        print(f"error: {input_problem}", file=sys.stderr)
        return EXIT_USAGE
    inp = input_path
    if not inp.exists():
        print(f"error: input file not found: {inp}", file=sys.stderr)
        return 2
    if not inp.is_file():
        print(f"error: input is not a file: {inp}", file=sys.stderr)
        return 2

    # Fail fast on an unknown layout, before parsing or rendering anything.
    try:
        get_layout_info(layout)
    except UnknownLayoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_discovery_warnings()
        return 2
    _print_discovery_warnings()

    try:
        cv = CVData.from_json_file(inp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        pdf_bytes = render_layout(layout, cv, LayoutOptions())
    # LayoutOverflowError (a LayoutError) from the layout, LayoutError when a
    # third-party layout breaks the return contract, PhotoError for the photo.
    except (LayoutError, PhotoError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    try:
        out = resolve_output_path(args.output, os.fspath(inp), args.allow_outside_output)
    except OutputPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 6

    if out.exists() and not args.overwrite:
        print(
            f"error: output already exists: {out} (use --overwrite to replace it)",
            file=sys.stderr,
        )
        return 4

    try:
        _atomic_write(out, pdf_bytes)
    except OSError as exc:
        print(f"error: cannot write {out}: {exc}", file=sys.stderr)
        return 5

    print(f"wrote {out}")

    if args.png:
        png_out = out.with_suffix(".png")
        try:
            _render_png_preview(out, png_out)
            print(f"wrote {png_out}")
        except (RuntimeError, subprocess.SubprocessError) as exc:
            print(f"warning: PNG preview skipped: {exc}", file=sys.stderr)

    return 0


def _print_discovery_warnings() -> None:
    """Report broken third-party layout entry points without failing the run."""
    for message in discovery_warnings():
        print(f"warning: {message}", file=sys.stderr)


def _active_template() -> str | None:
    """The template ``render`` would use now, or ``None`` (ambiguous/none/error)."""
    name, problem = select_layout(None)
    return None if problem else name


def layouts_command(args: argparse.Namespace) -> int:
    """List the discovered templates and installed layouts."""
    infos = available_layouts()
    templates = list_templates()
    active = _active_template()

    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "name": info.name,
                        "description": info.description,
                        "origin": info.origin,
                        "entry_point": info.entry_point,
                        "path": str(info.path) if info.path is not None else None,
                        "template": info.path is not None,
                        "active": info.name == active,
                    }
                    for info in infos
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        _print_discovery_warnings()
        return 0

    print(
        "available layouts (select one with 'cvgen render <input> --layout <name>')"
    )
    if infos:
        name_w = max(len(info.name) for info in infos)
        origin_w = max(len(info.origin) for info in infos)
        for info in infos:
            note = "  (active template)" if info.name == active else ""
            print(
                f"  {info.name:<{name_w}}  {info.origin:<{origin_w}}  "
                f"{info.description}{note}"
            )
            if info.path is not None:
                print(f"  {'':<{name_w}}  at {info.path}")
    else:  # pragma: no cover - the shipped template is always registered
        print("  (none registered)")
    if len(templates) > 1 and active is None:
        print(
            f"{len(templates)} templates found: name the active one with "
            f"'[template] name = ...' in {config_module.config_path(paths.REPO_ROOT)} "
            f"(or pass --layout)."
        )
    elif active is not None:
        print(f"active template: {active}")
    print(
        f"hint: add a template directory to 'template_dirs' in "
        f"{config_module.config_path(paths.REPO_ROOT)}, or install a package that "
        f"provides the '{ENTRY_POINT_GROUP}' entry point group."
    )
    _print_discovery_warnings()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cvgen",
        description="Render a structured JSON CV to a one-page A4 PDF.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("render", help="render a CV JSON file to PDF")
    r.add_argument(
        "input",
        nargs="?",
        default=None,
        help=(
            "path to the structured CV JSON file; optional when 'input' is set in "
            "cvgen.toml or --language is given"
        ),
    )
    r.add_argument("-o", "--output", help="output PDF name/path (default: <output dir>/<name>.pdf)")
    r.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    r.add_argument("--png", action="store_true", help="also write a PNG preview (requires poppler-utils)")
    r.add_argument(
        "--layout",
        metavar="NAME",
        default=None,
        help=(
            "template/layout to render with; wins over every other source.  Without it: "
            "the only discovered template, or 'cvgen.toml [template] name' when several "
            "exist (see 'cvgen layouts')"
        ),
    )
    r.add_argument(
        "--language",
        metavar="de|en",
        choices=("de", "en"),
        default=None,
        help="shortcut for <profile>/inputs/current_<language>.json",
    )
    r.add_argument(
        "--allow-outside-output",
        action="store_true",
        help="allow --output to point outside the output directory (default: confined)",
    )
    _add_config_options(r)
    r.set_defaults(func=render_command)

    lst = sub.add_parser("layouts", help="list the discovered templates and layouts")
    lst.add_argument("--json", action="store_true", dest="as_json", help="print the list as JSON")
    _add_config_options(lst)
    lst.set_defaults(func=layouts_command)
    return parser


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    """The configuration flags both subcommands accept (CLI beats everything)."""
    parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help=(
            f"configuration file (default: $CVGEN_CONFIG, else <repo>/config/"
            f"{config_module.CONFIG_FILENAME})"
        ),
    )
    parser.add_argument(
        "--profile-dir",
        metavar="DIR",
        default=None,
        help=f"private profile directory (default: $CVGEN_PROFILE_DIR, else cvgen.toml, else <repo>/profile)",
    )


def _apply_config_options(args) -> str | None:
    """Apply ``--config`` and ``--profile-dir``; returns an error message or ``None``."""
    if getattr(args, "config", None):
        try:
            paths.use_config(args.config)
        except config_module.ConfigError as exc:
            return str(exc)
    if paths.config_error():
        return paths.config_error()
    if getattr(args, "profile_dir", None):
        profile = Path(args.profile_dir).expanduser()
        if not profile.is_dir():
            return f"profile directory not found: {profile}"
        paths.use_profile_dir(profile)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    problem = _apply_config_options(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE

    if args.func is render_command:
        layout, problem = select_layout(args.layout)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return EXIT_USAGE
        args.layout = layout

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
