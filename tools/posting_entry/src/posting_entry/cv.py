"""Run the repository's ``cv`` tool for a folder that was just saved.

After a posting has been saved, the capture offers the tailored DE and EN CVs.
The real work belongs to the ``cv`` launcher in ``<job root>/bin/cv``, so this
module only discovers that launcher, builds its command line and reports the
outcome.

It is deliberately stdlib-only: importing it must never pull in prompt_toolkit,
which stays a lazily imported dependency of :mod:`posting_entry.ui`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

#: The tailored CVs offered after a saved posting, in this order.
CV_LANGUAGES = ("de", "en")
#: Name of the launcher below ``<job root>/bin``: ``<job root>/bin/cv`` is the
#: only accepted launcher.
LAUNCHER_NAME = "cv"
#: How many directory levels the upward search looks at.
MAX_ROOT_WALK = 6


def _looks_like_root(path: Path) -> bool:
    """A job repository root carries the launcher or the migration marker."""
    return (path / "bin" / LAUNCHER_NAME).is_file() or (path / "sql" / "migrations").is_dir()


def job_root_for(applications_root: Path) -> Path:
    """The job repository root that owns ``applications_root``.

    Resolution order: ``$JOB_ROOT``, then the nearest directory at or above the
    applications directory that carries the root marker (``bin/cv`` or
    ``sql/migrations``), then the parent of the applications directory, which is
    the best guess left when no marker is present.
    """
    env = os.environ.get("JOB_ROOT")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_dir():
            return candidate
    start = Path(applications_root).expanduser().resolve().parent
    for candidate in (start, *start.parents[:MAX_ROOT_WALK]):
        if _looks_like_root(candidate):
            return candidate
    return start


def launcher_for(applications_root: Path) -> Path | None:
    """The ``cv`` launcher of the job root, or ``None``.

    The launcher is looked up below the job root instead of on ``PATH`` on
    purpose: only that script knows which project and which job root it must
    run, so a same-named program elsewhere is not a substitute.  ``is_file``
    follows symlinks, which is wanted - ``<job root>/bin/cv`` is normally
    reached through the root entry ``<job root>/cv``.  No other name is looked
    up: ``bin/cv`` is the one accepted launcher.
    """
    root = job_root_for(applications_root)
    candidate = root / "bin" / LAUNCHER_NAME
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def cv_argv(launcher: Path, folder: str, language: str) -> list[str]:
    """The exact command line that tailors the CVs of one folder.

    ``--all`` and ``--yes`` make the sibling tool run without asking anything:
    this call happens in the middle of the capture flow, so it must not open a
    second interactive dialog.
    """
    return [os.fspath(launcher), "--folder", folder, "--language", language, "--all", "--yes"]


def retry_command(launcher: Path, folder: str, language: str) -> str:
    """The same command line as one copy-pasteable shell line."""
    return " ".join(shlex.quote(part) for part in cv_argv(launcher, folder, language))


def generate(ui, applications_root: Path, folder: str, *, runner=subprocess.run) -> None:
    """Generate the DE and EN CVs of ``folder`` through the sibling launcher.

    ``runner`` is injectable so the flow can be tested without spawning a
    process; it is called with the inherited stdio (the default of
    ``subprocess.run``) because the user is meant to watch the real tool.

    This function never raises and never turns a CV problem into a failure of
    the capture: the posting is already saved, so every problem is reported as
    a warning that names the language, the exit code and the retry command.
    """
    launcher = launcher_for(applications_root)
    if launcher is None:
        root = job_root_for(applications_root)
        expected = root / "bin" / LAUNCHER_NAME
        ui.warn(f"the CV tool {expected} is missing or not executable; the posting itself is saved")
        for language in CV_LANGUAGES:
            ui.warn(
                f"generate the {language.upper()} CV later with: "
                f"{retry_command(expected, folder, language)}"
            )
        return

    job_root = job_root_for(applications_root)
    for language in CV_LANGUAGES:
        ui.say(f"Generating the {language.upper()} CV for {folder} ...")
        try:
            completed = runner(cv_argv(launcher, folder, language), cwd=os.fspath(job_root))
        except OSError as exc:
            # An unusable launcher (permissions, missing interpreter) must be
            # reported like a failing run, not crash the capture.
            ui.warn(
                f"the {language.upper()} CV generation could not start ({exc}); "
                f"retry with: {retry_command(launcher, folder, language)}"
            )
            continue
        except KeyboardInterrupt:
            # Ctrl-C belongs to the running tool here, not to the capture: the
            # posting is saved, so this must never look like a cancelled entry.
            ui.warn(
                f"the {language.upper()} CV generation was interrupted; "
                f"retry with: {retry_command(launcher, folder, language)}"
            )
            break
        returncode = getattr(completed, "returncode", 1)
        if returncode:
            ui.warn(
                f"the {language.upper()} CV generation failed (exit {returncode}); "
                f"retry with: {retry_command(launcher, folder, language)}"
            )
