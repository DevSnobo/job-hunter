"""Scan ``applications/`` for real postings and classify existing CV targets.

Rules (see the implementation report for the rationale):

* Only immediate real sub-directories of ``applications/`` are considered.
* Symlinked directories, hidden directories (``.staging`` and similar) and
  directories without a regular ``posting.txt`` are ignored.  That excludes
  staging areas and UUID artifact folders in practice.
* A candidate is *usable* when ``posting.txt`` is a non-empty regular file.
* The tailored CV is expected at ``<folder>/<target_template rendered>``, where
  the template comes from ``cv.toml`` (default
  ``CV_{name}_{language}_{folder}.pdf``) and ``{name}`` is the identity slug
  read once from the profile (see :class:`cv_batch.config.TargetNaming`).
* An existing target counts as *valid* when it is a regular, non-symlink file
  with a ``%PDF-`` header and a non-zero size.  Otherwise it is *invalid* and is
  never replaced without an explicit opt-in.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PDF_MAGIC = b"%PDF-"

MISSING = "missing"
VALID = "valid"
INVALID = "invalid"


@dataclass(frozen=True)
class Application:
    """One application folder that holds a posting."""

    folder: str
    path: Path
    posting: Path
    #: identity recorded at scan time; the runner re-checks it before reading.
    dir_dev: int | None = None
    dir_ino: int | None = None
    posting_dev: int | None = None
    posting_ino: int | None = None

    @property
    def name(self) -> str:
        return self.folder


@dataclass(frozen=True)
class Skipped:
    folder: str
    reason: str


@dataclass(frozen=True)
class ScanResult:
    applications: list[Application]
    skipped: list[Skipped]
    unusable: list[Skipped]


@dataclass(frozen=True)
class TargetState:
    state: str
    path: Path
    reason: str = ""
    #: False for target shapes that must never be replaced destructively
    #: (symlink, directory, special file).
    replaceable: bool = True
    #: identity of the inspected entry (``os.lstat``), used to re-check it
    #: immediately before a replacement; ``None`` when nothing exists.
    dev: int | None = None
    ino: int | None = None
    size: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.state == VALID


class PostingReadError(RuntimeError):
    """A posting could not be read with no-follow semantics."""


def classify_target(path: Path) -> TargetState:
    """Classify an expected PDF target without modifying anything.

    The returned state carries the ``lstat`` identity of what was inspected so a
    later destructive step can prove that the entry did not change in between.
    Symlinks, directories and special files are flagged ``replaceable=False``.
    """
    try:
        if path.is_symlink():
            info = os.lstat(path)
            return TargetState(
                INVALID,
                path,
                "target is a symlink; refusing to touch it",
                replaceable=False,
                dev=info.st_dev,
                ino=info.st_ino,
            )
        if not path.exists():
            return TargetState(MISSING, path)
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode):
            return TargetState(
                INVALID,
                path,
                "target path is a directory",
                replaceable=False,
                dev=info.st_dev,
                ino=info.st_ino,
            )
        if not stat.S_ISREG(info.st_mode):
            return TargetState(
                INVALID,
                path,
                "target is not a regular file",
                replaceable=False,
                dev=info.st_dev,
                ino=info.st_ino,
            )
        if info.st_size == 0:
            return TargetState(
                INVALID,
                path,
                "target file is empty (0 bytes)",
                dev=info.st_dev,
                ino=info.st_ino,
                size=0,
            )
        with path.open("rb") as handle:
            head = handle.read(len(PDF_MAGIC))
        if head != PDF_MAGIC:
            return TargetState(
                INVALID,
                path,
                "target is not a PDF (bad header)",
                dev=info.st_dev,
                ino=info.st_ino,
                size=info.st_size,
            )
    except OSError as exc:  # unreadable/permission problems
        return TargetState(INVALID, path, f"cannot inspect target: {exc}", replaceable=False)
    return TargetState(VALID, path, dev=info.st_dev, ino=info.st_ino, size=info.st_size)


def _sort_key(name: str) -> tuple[str, str]:
    folded = unicodedata.normalize("NFC", name).casefold()
    return (folded, name)


def scan(applications_dir: Path) -> ScanResult:
    """Enumerate usable posting folders, sorted deterministically."""
    found: list[Application] = []
    skipped: list[Skipped] = []
    unusable: list[Skipped] = []

    if not applications_dir.is_dir():
        return ScanResult([], [], [Skipped(str(applications_dir), "applications directory not found")])

    for child in sorted(applications_dir.iterdir(), key=lambda p: _sort_key(p.name)):
        name = child.name
        if name.startswith("."):
            continue
        if child.is_symlink():
            skipped.append(Skipped(name, "symlinked directory"))
            continue
        if not child.is_dir():
            continue
        posting = child / "posting.txt"
        if posting.is_symlink():
            skipped.append(Skipped(name, "posting.txt is a symlink"))
            continue
        if not posting.is_file():
            skipped.append(Skipped(name, "no posting.txt"))
            continue
        try:
            posting_info = posting.stat()
            dir_info = child.stat()
        except OSError as exc:
            unusable.append(Skipped(name, f"cannot read posting.txt: {exc}"))
            continue
        if posting_info.st_size == 0:
            unusable.append(Skipped(name, "posting.txt is empty"))
            continue
        found.append(
            Application(
                folder=name,
                path=child,
                posting=posting,
                dir_dev=dir_info.st_dev,
                dir_ino=dir_info.st_ino,
                posting_dev=posting_info.st_dev,
                posting_ino=posting_info.st_ino,
            )
        )

    return ScanResult(found, skipped, unusable)


def _read_all(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def open_directory(path: Path) -> int:
    """Open a directory with ``O_NOFOLLOW``; the caller owns the descriptor.

    Raises :class:`PostingReadError` when the path is not a real directory (a
    symlink or a file), so a folder swapped after the scan cannot be followed.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise PostingReadError(f"cannot open application folder {path}: {exc}") from exc


def read_posting(
    path: Path,
    limit: int,
    *,
    expected_dev: int | None = None,
    expected_ino: int | None = None,
    dir_fd: int | None = None,
) -> str:
    """Read ``posting.txt`` with no-follow semantics and a bounded size.

    The file is opened with ``O_NOFOLLOW`` relative to a no-follow directory
    descriptor, so a posting that was swapped for a symlink between the scan and
    this read fails safely instead of disclosing another readable file to the
    model.  When the scan recorded the inode, the opened file must still be that
    exact inode.  Pass ``dir_fd`` (see :func:`open_directory`) to read relative to
    an *already validated* folder descriptor; then only ``path.name`` is used.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    owns_fd = dir_fd is None
    if owns_fd:
        dir_fd = open_directory(path.parent)
    try:
        try:
            fd = os.open(path.name, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise PostingReadError(
                f"refusing to read {path.name}: {exc} (the file changed or is a symlink)"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PostingReadError(f"{path.name} is not a regular file; refusing to read it")
            if expected_dev is not None and (info.st_dev, info.st_ino) != (expected_dev, expected_ino):
                raise PostingReadError(
                    f"{path.name} changed since the scan (inode mismatch); refusing to read it"
                )
            raw = _read_all(fd, limit * 4 + 4096)
        finally:
            os.close(fd)
    finally:
        if owns_fd:
            os.close(dir_fd)

    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n[... posting truncated ...]"
    return text


__all__ = [
    "Application",
    "INVALID",
    "MISSING",
    "PostingReadError",
    "open_directory",
    "ScanResult",
    "Skipped",
    "TargetState",
    "VALID",
    "classify_target",
    "read_posting",
    "scan",
]
