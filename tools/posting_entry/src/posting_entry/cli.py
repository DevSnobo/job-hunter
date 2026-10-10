"""Guided command-line capture of job postings (pre-application intake).

Each finished entry lands in::

    applications/<company>[_<posting-id>]/posting.txt
    applications/<company>[_<posting-id>]/source.txt

Nothing is submitted, no database is touched, and existing folders are kept.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, config as config_module, cv
from .store import (
    Collision,
    InvalidInput,
    PostingsError,
    describe_conflict,
    entry_name,
    normalize_component,
    normalize_source_url,
    save_entry,
)
from .ui import Cancelled, Ui, normalize_pasted_text, read_clipboard

CANCEL_EXIT = 130


def default_root(applications_dir: Path | None = None) -> Path:
    """The ``applications`` directory the capture writes into.

    ``$JOB_ROOT`` (exported by the ``./posting`` launcher) wins and means
    ``<JOB_ROOT>/applications``; then the ``applications_dir`` of the config
    file (already resolved against the job root by :mod:`posting_entry.config`);
    without either, the repository root is found by walking up from the current
    directory, so the tool also works when it is started by its console script
    from inside the checkout.
    """
    job_root = os.environ.get("JOB_ROOT")
    if job_root:
        return Path(job_root).expanduser() / "applications"
    if applications_dir is not None:
        return applications_dir
    return cv.job_root_for(Path.cwd() / "applications") / "applications"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="posting",
        description="Guided capture of a pasted job posting into applications/<company>[_<posting-id>]/.",
        epilog=(
            "Typical use: run ./posting and answer the four questions. Posting text is pasted "
            "as multiple lines and finished with a line containing only a dot. "
            "After a saved posting the tool offers once to generate the tailored DE and EN CVs "
            "with the sibling ./cv tool. "
            "Options only pre-answer the questions, e.g. for scripting or tests."
        ),
    )
    parser.add_argument("--root", default=None, help="applications directory (default: $JOB_ROOT/applications)")
    parser.add_argument("--company", default=None, help="answer for the company question")
    parser.add_argument("--posting-id", default=None, help="answer for the posting-id question ('' skips it)")
    parser.add_argument("--source", default=None, help="answer for the required source-URL question")
    parser.add_argument("--text-file", default=None, help="read the posting text from this UTF-8 file instead of pasting")
    parser.add_argument(
        "--clipboard",
        action="store_true",
        help="try to read the posting text from the clipboard (wl-paste/xclip/xsel); falls back to pasting",
    )
    parser.add_argument("--once", action="store_true", help="capture one posting and exit")
    parser.add_argument(
        "--no-cv",
        action="store_true",
        help="never offer to generate the tailored CVs after a saved posting "
             "(overrides config/posting.toml: offer_cv)",
    )
    parser.add_argument("--version", action="version", version=f"posting-entry {__version__}")
    return parser


def _answer(ui: Ui, preset: str | None, prompt: str, validator, *, default: str = "") -> str:
    """Use the preset answer when given, otherwise ask until the value is valid."""
    if preset is not None:
        return validator(preset)
    while True:
        raw = ui.ask(prompt, default=default)
        try:
            return validator(raw)
        except InvalidInput as exc:
            ui.warn(str(exc))


def _company_validator(raw: str) -> str:
    return normalize_component(raw, what="company name")


def _posting_id_validator(raw: str) -> str:
    if not raw.strip():
        return ""
    return normalize_component(raw, what="posting id")


def _source_validator(raw: str) -> str:
    return normalize_source_url(raw)


def _read_text_file(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PostingsError(f"cannot read text file {path}: {exc}") from None
    except UnicodeDecodeError as exc:
        raise PostingsError(f"text file {path} is not valid UTF-8: {exc}") from None
    return normalize_pasted_text(content)


def _posting_text(ui: Ui, args, *, source_from_clipboard: bool = False) -> str:
    """The posting text: from a file, from the clipboard, or pasted.

    ``source_from_clipboard`` carries the ``default_source_from_clipboard`` key
    of the config file; ``--clipboard`` has exactly the same effect and
    ``--text-file`` always wins over both.
    """
    if args.text_file:
        text = _read_text_file(args.text_file)
        if not text.strip():
            raise PostingsError(f"text file {args.text_file} is empty")
        return text
    if args.clipboard or source_from_clipboard:
        text, detail = read_clipboard()
        if text is not None:
            ui.say(f"Read {len(text)} characters from the clipboard ({detail}).")
            return normalize_pasted_text(text)
        ui.warn(f"clipboard not read ({detail}); paste the text manually instead")
    while True:
        text, terminated = ui.ask_multiline()
        if text.strip():
            if not terminated:
                ui.warn("the posting text was not finished with a '.' line")
            return text
        ui.warn("the posting text is empty; paste it again")


def _resolve_collision(ui: Ui, root: Path, company: str, posting_id: str) -> tuple[str, str, str, str]:
    """Return ``(company, posting_id, name, action)`` with action 'ok' or 'skip'.

    An existing destination is never overwritten: the user either supplies a
    different posting id, skips this posting, or cancels.
    """
    while True:
        name = entry_name(company, posting_id)
        dest = root / name
        if not os.path.lexists(dest):
            return company, posting_id, name, "ok"
        ui.say()
        ui.say(f"Destination already exists: {dest}")
        ui.say(f"  it is a {describe_conflict(dest)}")
        ui.say("Existing folders are never overwritten, renamed or deleted.")
        choice = ui.ask("[o]ther posting id / [s]kip this posting / [c]ancel: ", default="").lower()
        if choice in {"o", "other"}:
            # Always ask for the new id; reusing the preset would instantly
            # collide again and consume the real answer as a menu choice.
            posting_id = _answer(
                ui,
                None,
                "Posting id (blank = none; use one that does not exist yet): ",
                _posting_id_validator,
            )
            continue
        if choice in {"s", "skip"}:
            return company, posting_id, name, "skip"
        if choice in {"c", "cancel"}:
            raise Cancelled("cancelled at the existing-folder prompt")
        ui.say("Please answer 'o', 's' or 'c' (Ctrl-C also cancels).")


def capture_one(ui: Ui, root: Path, args, *, source_from_clipboard: bool = False) -> str:
    """Capture a single posting. Returns 'saved', 'skipped' or 'discarded'.

    ``source_from_clipboard`` is the resolved config value for the posting text
    (see :func:`_posting_text`).
    """
    company = _answer(ui, args.company, "Company name: ", _company_validator)
    posting_id = _answer(
        ui,
        args.posting_id,
        "Posting id (optional, blank = none): ",
        _posting_id_validator,
    )
    source_url = _answer(
        ui,
        args.source,
        "Source URL (LinkedIn, Instaffo, ...; required): ",
        _source_validator,
    )

    company, posting_id, name, action = _resolve_collision(ui, root, company, posting_id)
    if action == "skip":
        ui.say(f"Skipped {name}; the existing folder is untouched.")
        return "skipped"

    text = _posting_text(ui, args, source_from_clipboard=source_from_clipboard)
    dest = root / name

    ui.say()
    ui.say("--- review ---")
    ui.say(f"Company    : {company}")
    ui.say(f"Posting id : {posting_id or '-'}")
    ui.say(f"Folder     : {dest}")
    ui.say(f"Source URL : {source_url or '-'}")
    ui.say(f"Text       : {len(text.splitlines())} lines, {len(text)} characters")
    for line in text.splitlines()[:3]:
        ui.say(f"  | {line[:100]}")
    if len(text.splitlines()) > 3:
        ui.say("  | ...")

    if not ui.ask_yes_no("Save this posting?", default=True):
        ui.say("Discarded; nothing was written.")
        return "discarded"

    saved = save_entry(root, name, text, source_url)
    ui.say(f"Saved {saved}")
    _offer_cv(ui, root, name, args)
    return "saved"


def _offer_cv(ui: Ui, root: Path, folder: str, args) -> None:
    """Offer the tailored DE and EN CVs for a folder that was just saved.

    Asked only in a real interactive session (and not with ``--no-cv``): a
    piped or scripted capture has nobody to answer it.

    The entry is already on disk at this point, so nothing here may abort the
    capture or change its exit code - a CV problem is a warning, and both EOF
    and Ctrl-C mean "skip the CVs" instead of "cancel" (the question uses
    ``ask_yes_no_or_eof`` for exactly that reason).
    """
    if args.no_cv or not ui.interactive_session():
        return
    try:
        wanted = ui.ask_yes_no_or_eof("Generate the tailored CVs now (DE and EN)?", default=True)
    except (Cancelled, KeyboardInterrupt):
        # Without this catch the end of the session would report "the current
        # entry was not saved", which is plain wrong after a successful save.
        ui.say("CV generation skipped.")
        return
    if wanted:
        cv.generate(ui, root, folder)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ui = Ui()
    preset_root = Path(args.root).expanduser() if args.root else None
    try:
        config = config_module.load(preset_root)
    except config_module.ConfigError as exc:
        # A config problem is a *usage* error, not a failed capture: name the
        # file and the problem and stop before anything is asked or written.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    root = preset_root or default_root(config.applications_dir)
    # offer_cv = false behaves exactly like --no-cv, so the flag and the config
    # key share the one place that decides whether the CVs are offered.
    args.no_cv = args.no_cv or not config.offer_cv

    ui.say("Job posting capture (pre-application intake).")
    ui.say(f"Applications directory: {root}")
    ui.say("Existing folders are never overwritten. Ctrl-C or Ctrl-D cancels the current entry.")
    ui.say()

    try:
        while True:
            outcome = capture_one(
                ui, root, args,
                source_from_clipboard=config.default_source_from_clipboard,
            )
            if args.once:
                break
            another = ui.ask_yes_no_or_eof("Another posting?", default=True)
            if another is None:
                # EOF between entries is a normal stop, not a cancellation:
                # any entries saved earlier in this session are already complete.
                if outcome == "saved":
                    ui.say("End of input after the last saved entry; stopping.")
                else:
                    ui.say("End of input; stopping.")
                break
            if not another:
                break
    except Cancelled as exc:
        ui.warn(
            f"cancelled ({exc}); the current entry was not saved "
            "(entries saved earlier in this session are kept)"
        )
        return CANCEL_EXIT
    except KeyboardInterrupt:
        ui.warn("interrupted; the current entry was not saved (entries saved earlier in this session are kept)")
        return CANCEL_EXIT
    except Collision as exc:
        ui.warn(str(exc))
        return 1
    except PostingsError as exc:
        ui.warn(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
