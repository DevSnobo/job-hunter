"""Invoke the existing CV generator and place the PDF without clobbering.

The renderer is *only* invoked from this Python pipeline.  The model never runs
it.  Output is written to a tool-owned staging path (so the generator never
touches a real file first) and then hard-linked to the final name, which makes
the final step a no-overwrite operation.  Only two explicit opt-ins replace an
existing target: ``--regenerate-invalid`` (invalid targets) and ``--overwrite``
(existing valid CVs); both back the old bytes up first and re-check the target
immediately before the replacement.

The renderer layout is an opaque pass-through (``--layout``): this module
forwards whatever name it is given and never validates it against a local list,
so a third-party layout package can be used without changing cv_batch.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

OVERFLOW_HINT = "overflow"

PDF_MAGIC = b"%PDF-"

#: grace period for a terminated process group before SIGKILL
GRACE_SECONDS = 5.0


def terminate_process_group(process: subprocess.Popen, *, kill: bool = False) -> None:
    """Signal the whole process group of ``process`` (descendants included)."""
    signal_number = signal.SIGKILL if kill else signal.SIGTERM
    pid = process.pid
    try:
        os.killpg(os.getpgid(pid), signal_number)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.kill() if kill else process.terminate()
    except OSError:  # pragma: no cover - defensive
        pass


def terminate_processes(processes: Iterable[subprocess.Popen], *, grace: float = GRACE_SECONDS) -> None:
    """Terminate a set of child leaders, escalating to SIGKILL after ``grace``.

    Each child is started in its own session, so the group signal also reaches
    renderer/model descendants (uv, the real generator, ...).
    """
    live = [process for process in processes if process.poll() is None]
    for process in live:
        terminate_process_group(process)
    deadline = time.monotonic() + grace
    for process in live:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
    for process in live:
        if process.poll() is None:
            terminate_process_group(process, kill=True)


def validate_pdf_file(path: Path) -> None:
    """Require a regular, non-symlink, non-empty file that starts with ``%PDF-``."""
    try:
        if path.is_symlink():
            raise RenderError(f"staged output is a symlink: {path}", category="render")
        info = os.lstat(path)
    except OSError as exc:
        raise RenderError(f"staged output is missing: {path} ({exc})", category="render") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RenderError(f"staged output is not a regular file: {path}", category="render")
    if info.st_size == 0:
        raise RenderError("renderer reported success but produced an empty file", category="render")
    try:
        with path.open("rb") as handle:
            head = handle.read(len(PDF_MAGIC))
    except OSError as exc:
        raise RenderError(f"cannot read staged output {path}: {exc}", category="render") from exc
    if head != PDF_MAGIC:
        raise RenderError(
            f"renderer reported success but the staged output is not a PDF (header {head!r})",
            category="render",
        )


class RenderError(RuntimeError):
    def __init__(self, message: str, *, category: str = "render", stderr: str = "") -> None:
        super().__init__(message)
        self.category = category
        self.stderr = stderr


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    category: str = "ok"


def render_command(
    *,
    generator_project: Path,
    generator_exe: str | None,
    input_path: Path,
    output_path: Path,
    layout: str | None = None,
) -> list[str]:
    """Build the renderer command, using the project's own environment.

    ``layout`` is an opaque pass-through: a concrete name (including the
    default, ``classic``) is forwarded verbatim as ``--layout <name>``.  It is
    never validated against a local list, because the generator owns the layout
    registry and a third-party package can add layouts; a bad name must surface
    as the generator's own render failure (mapped to the render-error
    categories in :func:`render_pdf`), not as a guess made here.  ``None``
    omits the flag entirely, which leaves the choice to the generator's default.
    """
    tail = ["render", str(input_path), "--output", str(output_path), "--allow-outside-output"]
    if layout is not None:
        tail += ["--layout", layout]
    if generator_exe:
        return [generator_exe, *tail]
    return ["uv", "run", "--quiet", "--project", str(generator_project), "cvgen", *tail]


def render_pdf(
    *,
    generator_project: Path,
    generator_exe: str | None,
    input_path: Path,
    output_path: Path,
    layout: str | None = None,
    timeout: float = 150.0,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> RenderResult:
    """Render ``input_path`` to ``output_path``; never overwrites an existing file.

    The renderer runs in its own session and is reported through
    ``on_start``/``on_end`` so a cancelled batch can terminate the complete
    process group (``uv``, the generator and any descendants).  A timeout kills
    the whole group, not only the leader.

    ``layout`` selects the generator layout (see :func:`render_command`); it is
    forwarded unchanged and an unknown name fails like any other renderer error.
    """
    argv = render_command(
        generator_project=generator_project,
        generator_exe=generator_exe,
        input_path=input_path,
        output_path=output_path,
        layout=layout,
    )
    if output_path.exists():
        raise RenderError(f"refusing to render onto an existing file: {output_path}", category="exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RenderError(f"renderer not found: {argv[0]}", category="runtime") from exc
    except OSError as exc:
        raise RenderError(f"cannot start renderer {argv[0]}: {exc}", category="runtime") from exc

    if on_start is not None:
        on_start(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_processes([process])
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
            raise RenderError(f"renderer timed out after {timeout:.0f}s", category="timeout") from None
    finally:
        if on_end is not None:
            on_end(process)

    code = process.returncode
    stdout, stderr = stdout or "", stderr or ""
    if code == 0:
        try:
            validate_pdf_file(output_path)
        except RenderError as exc:
            raise RenderError(str(exc), category="render", stderr=stderr) from exc
        return RenderResult(True, code, stdout, stderr)
    if code == 3 and OVERFLOW_HINT in stderr.lower():
        return RenderResult(False, code, stdout, stderr, category="overflow")
    raise RenderError(
        f"renderer failed (exit {code}): {_first_line(stderr) or _first_line(stdout)}",
        category="render",
        stderr=stderr,
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


class PlaceError(RuntimeError):
    """The finished PDF could not be placed safely."""


def place_no_clobber(staged: Path, final: Path) -> None:
    """Move ``staged`` to ``final`` without ever overwriting an existing file."""
    if final.is_symlink():
        raise PlaceError(f"refusing to write through a symlink: {final}")
    if final.exists():
        raise PlaceError(f"refusing to overwrite an existing file: {final}")
    try:
        os.link(staged, final)
        staged.unlink()
        return
    except FileExistsError as exc:
        raise PlaceError(f"refusing to overwrite an existing file: {final}") from exc
    except OSError as exc:
        if exc.errno not in (getattr(os, "EXDEV", 18),):
            raise PlaceError(f"cannot place {final}: {exc}") from exc
    # Different filesystem: copy into the destination directory, then link there.
    part = final.parent / f".{final.name}.cv-{uuid.uuid4().hex}.part"
    try:
        shutil.copy2(staged, part)
        os.link(part, final)
    except FileExistsError as exc:
        raise PlaceError(f"refusing to overwrite an existing file: {final}") from exc
    finally:
        try:
            part.unlink()
        except OSError:
            pass
    staged.unlink()


def _stage_beside(staged: Path, final: Path) -> Path:
    """Copy ``staged`` into the destination directory (same filesystem).

    This makes the last step a same-filesystem ``os.replace``: a state directory
    on another filesystem (EXDEV) cannot break an invalid-target replacement.
    """
    part = final.parent / f".{final.name}.cv-{uuid.uuid4().hex}.part"
    try:
        with staged.open("rb") as source, part.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return part


def _require_replaceable_target(
    final: Path,
    *,
    label: str = "target",
    expect_dev: int | None = None,
    expect_ino: int | None = None,
) -> os.stat_result:
    """Require a regular, non-symlink target with the inode that was inspected."""
    if final.is_symlink():
        raise PlaceError(f"refusing to replace a symlinked target: {final}")
    try:
        info = os.lstat(final)
    except OSError as exc:
        raise PlaceError(f"the {label} disappeared before replacement: {final} ({exc})") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PlaceError(f"refusing to replace a non-regular target: {final}")
    if expect_dev is not None and (info.st_dev, info.st_ino) != (expect_dev, expect_ino):
        raise PlaceError(
            f"the target {final.name} changed after it was inspected; refusing to replace it"
        )
    return info


def _backup_and_replace(
    staged: Path,
    final: Path,
    backup_dir: Path,
    backup_path: Path | None,
) -> Path:
    """Copy the current target into ``backup_dir``, then move the new PDF in."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_path or (backup_dir / f"{final.name}.{uuid.uuid4().hex[:8]}.bak")
    shutil.copy2(final, backup)
    part = _stage_beside(staged, final)
    try:
        os.replace(part, final)
    finally:
        try:
            part.unlink()
        except OSError:
            pass
    return backup


def replace_invalid(
    staged: Path,
    final: Path,
    backup_dir: Path,
    *,
    backup_path: Path | None = None,
    expect_dev: int | None = None,
    expect_ino: int | None = None,
) -> Path:
    """Back up an invalid target, then place the new PDF (explicit opt-in only).

    The target is re-checked immediately before the replacement: it must still be
    a regular, non-symlink file with the exact inode that was inspected (and it
    must still be a non-PDF).  A target that became valid — for example because
    another job replaced it — is never overwritten.
    """
    _require_replaceable_target(
        final, label="invalid target", expect_dev=expect_dev, expect_ino=expect_ino
    )
    try:
        with final.open("rb") as handle:
            head = handle.read(len(PDF_MAGIC))
    except OSError as exc:
        raise PlaceError(f"cannot re-read the invalid target {final}: {exc}") from exc
    if head == PDF_MAGIC:
        raise PlaceError(
            f"the target {final.name} is now a valid PDF; refusing to overwrite it"
        )
    return _backup_and_replace(staged, final, backup_dir, backup_path)


def replace_existing(
    staged: Path,
    final: Path,
    backup_dir: Path,
    *,
    backup_path: Path | None = None,
    expect_dev: int | None = None,
    expect_ino: int | None = None,
) -> Path:
    """Back up an *existing valid* CV, then place the new PDF (``--overwrite``).

    Counterpart of :func:`replace_invalid` for a target that already was a valid
    PDF when it was inspected.  The target is re-checked immediately before the
    replacement: it must still be the exact regular, non-symlink file that was
    inspected and must still be a non-empty PDF, so a target that changed in
    between is refused instead of being overwritten blindly.
    """
    info = _require_replaceable_target(final, expect_dev=expect_dev, expect_ino=expect_ino)
    if info.st_size == 0:
        raise PlaceError(
            f"the target {final.name} changed after it was inspected (now empty); "
            f"refusing to replace it"
        )
    try:
        with final.open("rb") as handle:
            head = handle.read(len(PDF_MAGIC))
    except OSError as exc:
        raise PlaceError(f"cannot re-read the target {final}: {exc}") from exc
    if head != PDF_MAGIC:
        raise PlaceError(
            f"the target {final.name} is no longer a valid PDF; refusing to replace it"
        )
    return _backup_and_replace(staged, final, backup_dir, backup_path)


__all__ = [
    "GRACE_SECONDS",
    "PDF_MAGIC",
    "PlaceError",
    "RenderError",
    "RenderResult",
    "place_no_clobber",
    "render_command",
    "render_pdf",
    "replace_existing",
    "replace_invalid",
    "terminate_process_group",
    "terminate_processes",
    "validate_pdf_file",
]
