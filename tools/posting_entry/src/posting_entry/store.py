"""Directory naming and staged, non-destructive writes for intake folders.

The layout this tool owns is::

    applications/<company>[_<posting-id>]/posting.txt
    applications/<company>[_<posting-id>]/source.txt

Existing folders are never renamed, overwritten or deleted. Every created
folder contains both ``posting.txt`` and a nonblank ``source.txt``.
"""

from __future__ import annotations

import os
import shutil
import unicodedata
import uuid
from urllib.parse import urlsplit

MAX_COMPONENT = 200
STAGING_PREFIX = ".posting-entry-"


class PostingsError(Exception):
    """Base class for user-facing errors."""


class InvalidInput(PostingsError):
    """The user supplied a value that cannot be used."""


class Collision(PostingsError):
    """The destination already exists (or is a symlink)."""


def normalize_component(raw: str, *, what: str = "value") -> str:
    """Return a safe single path component derived from ``raw``.

    Newlines from accidental multi-line pastes collapse to single spaces, path
    separators become ``_``, and control characters are refused. Unicode is
    preserved (NFC).
    """
    text = unicodedata.normalize("NFC", raw)
    for ch in text:
        if (ord(ch) < 32 and not ch.isspace()) or ord(ch) == 127:
            raise InvalidInput(f"{what} contains a control character; please retype it on one line")
    text = text.replace("/", "_").replace("\\", "_")
    text = " ".join(text.split())
    if not text:
        raise InvalidInput(f"{what} must not be empty")
    if set(text) == {"."}:
        raise InvalidInput(f"{what} must not be {text!r}")
    if text.startswith("."):
        raise InvalidInput(f"{what} must not start with '.'")
    if os.sep in text or os.altsep and os.altsep in text:
        raise InvalidInput(f"{what} must not contain a path separator")
    if len(text) > MAX_COMPONENT:
        raise InvalidInput(f"{what} is longer than {MAX_COMPONENT} characters; please shorten it")
    return text


def entry_name(company: str, posting_id: str | None) -> str:
    """Build the folder name ``<company>`` or ``<company>_<posting-id>``."""
    name = normalize_component(company, what="company name")
    if posting_id:
        name = f"{name}_{normalize_component(posting_id, what='posting id')}"
    if len(name) > MAX_COMPONENT:
        raise InvalidInput(f"folder name is longer than {MAX_COMPONENT} characters; please shorten it")
    return name


def normalize_source_url(raw: str) -> str:
    """Validate and normalize a source URL.

    A source is required: blank input is refused, because every intake folder
    must contain a ``source.txt`` pointing at the posting.
    """
    text = " ".join(raw.split())
    if not text:
        raise InvalidInput("source URL must not be empty; paste the job posting link")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise InvalidInput(f"source URL must start with http:// or https:// (got {text!r})")
    host = parts.netloc.rsplit("@", 1)[-1].split(":")[0].rstrip(".")
    if not host or "." not in host:
        raise InvalidInput(f"source URL has no usable host (got {text!r})")
    return text


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_file(path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def describe_conflict(dest) -> str:
    """One-line description of an existing destination."""
    path = os.fspath(dest)
    if os.path.islink(path):
        return "symlink"
    if os.path.isdir(path):
        try:
            entries = sorted(os.listdir(path))
        except OSError:
            entries = []
        shown = ", ".join(entries[:6]) or "empty directory"
        return f"directory ({shown})"
    return "file"


def ensure_destination(root, name: str) -> str:
    """Return the destination path, refusing anything that already exists."""
    if not name or os.sep in name or name != os.path.basename(name):
        raise InvalidInput(f"refusing unsafe folder name {name!r}")
    dest = os.path.join(os.fspath(root), name)
    if os.path.lexists(dest):
        raise Collision(f"{dest} already exists: {describe_conflict(dest)}")
    return dest


def save_entry(root, name: str, posting_text: str, source_url: str) -> str:
    """Write one intake folder and return the created directory.

    The files are written inside a staging directory first. The destination is
    then reserved with an exclusive ``mkdir`` so an existing file, symlink or
    directory can never be replaced, and the finished files are moved in. Any
    failure removes the partially promoted destination again.
    """
    if not posting_text.strip():
        raise InvalidInput("posting text must not be empty")
    source_url = normalize_source_url(source_url)
    root = os.fspath(root)
    os.makedirs(root, exist_ok=True)
    dest = ensure_destination(root, name)

    staging = os.path.join(root, f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        os.mkdir(staging, 0o700)
        _write_file(os.path.join(staging, "posting.txt"), posting_text)
        _write_file(os.path.join(staging, "source.txt"), source_url + "\n")
        _fsync_dir(staging)

        try:
            os.mkdir(dest, 0o755)
        except FileExistsError:
            raise Collision(f"{dest} already exists: {describe_conflict(dest)}") from None

        promoted = False
        try:
            for filename in sorted(os.listdir(staging)):
                os.rename(os.path.join(staging, filename), os.path.join(dest, filename))
            promoted = True
            _fsync_dir(dest)
            _fsync_dir(root)
        except BaseException:
            if not promoted:
                shutil.rmtree(dest, ignore_errors=True)
            raise
        return dest
    finally:
        if os.path.isdir(staging) and not os.path.islink(staging):
            shutil.rmtree(staging, ignore_errors=True)
