"""Run the repository's posting capture (``./posting``) from the tracker menu.

The "Prepare application" menu entry hands over to the sibling
``tools/posting_entry`` project through the repository launcher
``<job root>/bin/posting``.  It asks for one posting (``--once``) so the menu
runs one application from the posting through its CV offer to the registration.
This module only discovers that launcher, builds its command line and reports
whether the capture ran; creating the folder, the ``posting.txt``/``source.txt``
pair and the optional CVs stays entirely with the posting tool.

The launcher is looked up below the job root instead of on ``PATH`` on purpose:
only that script knows which project and which job root it must run.  The same
pattern is used by ``tools/posting_entry/cv.py`` to reach ``./cv``.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

#: Name of the launcher below ``<job root>/bin``: ``<job root>/bin/posting`` is
#: the only accepted launcher.
LAUNCHER_NAME = "posting"


def launcher_for(root: Path) -> Path | None:
    """The ``posting`` launcher of the job root, or ``None``.

    ``is_file`` follows symlinks, which is wanted - ``<job root>/bin/posting`` is
    normally reached through the root entry ``<job root>/posting``.  No other
    name is looked up.
    """
    candidate = Path(root) / "bin" / LAUNCHER_NAME
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def argv(launcher: Path) -> list[str]:
    """The command line that starts the guided capture for exactly one posting.

    ``--once`` makes the capture stop after the first posting, so the menu entry
    runs the posting, its CV offer and the registration as one application, top
    to bottom, without the "Another posting?" question in between. That question
    belongs to a standalone ``./posting`` run, which captures a batch.
    """
    return [os.fspath(launcher), "--once"]


def retry_command(launcher: Path) -> str:
    """The same command line as one copy-pasteable shell line."""
    return " ".join(shlex.quote(part) for part in argv(launcher))


def run(ui, root: Path, *, runner=subprocess.run) -> bool:
    """Run the posting capture of ``root``; return whether it ran at all.

    ``runner`` is injectable so the flow can be tested without spawning a
    process; it is called with the inherited stdio (the default of
    ``subprocess.run``) because the user is meant to watch and answer the real
    tool.  ``JOB_ROOT`` is set for the child so the capture writes into the same
    job root as this invocation, exactly as the standalone ``./posting``
    launcher does.

    A missing or unstartable launcher is reported as a warning and returns
    ``False`` - preparing is optional work in the menu and never becomes a
    tracker error.  A capture that ran but exited non-zero still returns
    ``True``: it may have saved entries before stopping, so the caller still
    looks for the folders it created.
    """
    launcher = launcher_for(root)
    if launcher is None:
        ui.note(f"[yellow]the posting capture tool {Path(root) / 'bin' / LAUNCHER_NAME}"
                " is missing or not executable; capture the posting with ./posting"
                " instead[/yellow]")
        return False
    env = dict(os.environ, JOB_ROOT=os.fspath(root))
    try:
        completed = runner(argv(launcher), cwd=os.fspath(root), env=env)
    except OSError as exc:
        ui.note(f"[yellow]the posting capture could not start ({exc}); retry with:"
                f" {retry_command(launcher)}[/yellow]")
        return False
    except KeyboardInterrupt:
        ui.note("[yellow]the posting capture was interrupted[/yellow]")
        return True
    returncode = getattr(completed, "returncode", 1)
    if returncode:
        ui.note(f"[yellow]the posting capture stopped (exit {returncode}); retry with:"
                f" {retry_command(launcher)}[/yellow]")
    return True
