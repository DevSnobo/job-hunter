"""Read-only view of the pre-submission intake folders in ``applications/``.

The posting capture tool (``tools/posting_entry``) writes one folder per
captured posting::

    applications/<company>[_<posting-id>]/posting.txt
    applications/<company>[_<posting-id>]/source.txt

and the CV batch tool (``tools/cv_batch``) writes the tailored CVs into the
same folder.  The folder name is the application identifier that both tools and
the generated file names use, and it is also what identifies the application
here.

The tracker only reads these folders.  ``job add --from-folder`` starts a new
application from one folder: the company name comes from the folder identifier
and the job URL from ``source.txt``.  Nothing in this module renames, moves,
writes, or deletes a folder, and every read refuses to follow a symlink.
"""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import UsageError

#: A posting id is the optional ``_<posting-id>`` trailer of a folder name.
POSTING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
POSTING_FILENAME = "posting.txt"
SOURCE_FILENAME = "source.txt"
MAX_SOURCE_BYTES = 8192
#: A captured job posting is a few KB; the bound only stops a huge or swapped
#: file from being read into memory.  Longer text is marked as truncated.
MAX_POSTING_BYTES = 512 * 1024
MAX_NAME = 200
#: --set values that clear a stored folder link instead of setting one.
CLEAR_WORDS = ("none", "null", "-")


@dataclass(frozen=True)
class IntakeFolder:
    """One ``applications/<identifier>`` folder and what can be read from it."""

    name: str
    path: Path
    company: str
    posting_id: str | None
    source_url: str | None
    source_problem: str | None
    files: tuple[str, ...]
    mtime: float

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def has_posting(self) -> bool:
        return POSTING_FILENAME in self.files


@dataclass(frozen=True)
class Skipped:
    """An entry under ``applications/`` that is not an intake folder."""

    name: str
    reason: str


@dataclass(frozen=True)
class Scan:
    applications_dir: Path
    folders: list[IntakeFolder] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


# --------------------------------------------------------------------------
# the folder identifier <company>[_<posting-id>]
# --------------------------------------------------------------------------
def _looks_like_posting_id(text: str) -> bool:
    return bool(POSTING_ID_RE.match(text)) and any(char.isdigit() for char in text)


def split_identifier(name: str) -> tuple[str, str | None]:
    """Split ``<company>[_<posting-id>]`` into company name and posting id.

    The *last* underscore separates the two parts, and only when the trailer
    looks like a posting id (no spaces, at least one digit).  A company name
    that contains an underscore keeps it, because the trailer is then not a
    posting id::

        Globex_155461          -> ('Globex', '155461')
        Acme GmbH_2463         -> ('Acme GmbH', '2463')
        Initech Bank_2024-0091 -> ('Initech Bank', '2024-0091')
        Umbrella-Corp          -> ('Umbrella-Corp', None)
        no_idea                -> ('no_idea', None)
    """
    head, separator, tail = name.rpartition("_")
    if separator and head.strip() and _looks_like_posting_id(tail):
        return head.strip(), tail
    return name.strip(), None


def build_identifier(company: str, posting_id: str | None = None) -> str:
    """The inverse of :func:`split_identifier`, for tests and messages."""
    text = company.strip()
    return f"{text}_{posting_id}" if posting_id else text


def normalize_link(value: str | None) -> str | None:
    """Validate a stored folder link; blank, ``none``, ``null``, and ``-`` clear it."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in CLEAR_WORDS:
        return None
    if (text in (".", "..") or text.startswith(".") or os.sep in text
            or os.altsep and os.altsep in text
            or "/" in text or "\\" in text or len(text) > MAX_NAME):
        raise UsageError(
            f"unusable intake folder name: {value!r} (a bare folder name below"
            " applications/ is expected)"
        )
    return text


# --------------------------------------------------------------------------
# reading one folder (never follows a symlink, never writes)
# --------------------------------------------------------------------------
def _sort_key(name: str) -> tuple[str, str]:
    folded = unicodedata.normalize("NFC", name).casefold()
    return (folded, name)


def _regular_files(folder: Path) -> tuple[str, ...]:
    """Names of the regular files directly inside the folder (hidden ones skipped)."""
    try:
        entries = sorted(os.listdir(folder), key=_sort_key)
    except OSError:
        return ()
    found: list[str] = []
    for name in entries:
        if name.startswith("."):
            continue
        entry = folder / name
        try:
            if entry.is_symlink() or not stat.S_ISREG(entry.stat().st_mode):
                continue
        except OSError:
            continue
        found.append(name)
    return tuple(found)


def _usable_url(text: str) -> str | None:
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.netloc.rsplit("@", 1)[-1].split(":")[0].rstrip(".")
    if not host or "." not in host:
        return None
    return text


def _read_stored_text(path: Path, *, limit: int, label: str,
                      missing: str) -> tuple[str | None, str | None]:
    """Return ``(text, problem)`` from one stored file; exactly one is set.

    The file is opened with ``O_NOFOLLOW`` and read up to ``limit`` bytes, so a
    file swapped for a symlink cannot disclose another file and a huge file
    cannot exhaust memory.  Text that reaches the bound is marked as truncated.
    """
    try:
        if path.is_symlink():
            return None, f"{label} is a symlink; refusing to follow it"
    except OSError as exc:  # pragma: no cover - defensive
        return None, f"cannot inspect {label}: {exc}"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, missing
    except OSError as exc:
        return None, f"cannot read {label}: {exc}"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, f"{label} is not a regular file"
        chunks: list[bytes] = []
        read_bytes = 0
        while read_bytes < limit:
            chunk = os.read(descriptor, min(limit - read_bytes, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            read_bytes += len(chunk)
        truncated = read_bytes >= limit and bool(os.read(descriptor, 1))
        raw = b"".join(chunks)
    except OSError as exc:  # pragma: no cover - defensive
        return None, f"cannot read {label}: {exc}"
    finally:
        os.close(descriptor)
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n[{label} truncated at {limit} bytes]\n"
    return text, None


def read_source_url(folder: Path) -> tuple[str | None, str | None]:
    """Return ``(url, problem)`` from ``<folder>/source.txt``.

    Exactly one of the two is set.  The file is opened with ``O_NOFOLLOW`` and
    read up to a fixed bound, so a ``source.txt`` swapped for a symlink cannot
    disclose another file.  The first non-blank line is the job URL.
    """
    text, problem = _read_stored_text(folder / SOURCE_FILENAME, limit=MAX_SOURCE_BYTES,
                                      label=SOURCE_FILENAME,
                                      missing="the folder has no source.txt")
    if problem is not None:
        return None, problem
    assert text is not None  # for the type checker: a problem is always set otherwise
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    if not line:
        return None, "source.txt is empty"
    url = _usable_url(line)
    if url is None:
        return None, f"source.txt has no usable http(s) URL ({line[:60]!r})"
    return url, None


def read_posting_file(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, problem)`` from one stored copy of a job posting.

    Exactly one of the two is set.  The file is read as defensively as
    ``source.txt``: a symlink is refused, only a regular file is read, and at
    most :data:`MAX_POSTING_BYTES` bytes are read.  Text without any non-blank
    character counts as a problem (there would be nothing to read).
    """
    name = path.name or POSTING_FILENAME
    text, problem = _read_stored_text(path, limit=MAX_POSTING_BYTES, label=name,
                                      missing=f"there is no {name}")
    if problem is not None:
        return None, problem
    assert text is not None  # for the type checker: a problem is always set otherwise
    if not text.strip():
        return None, f"{name} is empty"
    return text, None


def describe(folder: Path) -> IntakeFolder:
    """Read one folder that the caller has already checked is a real directory."""
    info = folder.stat()
    url, problem = read_source_url(folder)
    company, posting_id = split_identifier(folder.name)
    return IntakeFolder(
        name=folder.name,
        path=folder,
        company=company,
        posting_id=posting_id,
        source_url=url,
        source_problem=problem,
        files=_regular_files(folder),
        mtime=info.st_mtime,
    )


def _holds_intake_files(folder: Path) -> bool:
    return any(os.path.lexists(folder / name)
               for name in (POSTING_FILENAME, SOURCE_FILENAME))


def scan(applications_dir: Path,
         exclude: Collection[str] = ()) -> Scan:
    """Enumerate the intake folders under ``applications/``, sorted by name.

    Hidden entries, symlinks, plain files, and directories that hold neither
    ``posting.txt`` nor ``source.txt`` are reported as skipped instead of being
    offered.  Pass ``exclude`` to leave out the artifact directories that belong
    to registered applications (``applications/<uuid>``): such a folder is an
    application's own file folder, not an intake folder, even when it holds a
    ``posting.txt``.
    """
    base = Path(applications_dir)
    result = Scan(base)
    excluded = set(exclude)
    if not base.is_dir():
        return result
    for entry in sorted(base.iterdir(), key=lambda path: _sort_key(path.name)):
        name = entry.name
        if name.startswith("."):
            result.skipped.append(Skipped(name, "hidden entry"))
            continue
        if entry.is_symlink():
            result.skipped.append(Skipped(name, "symlink; not followed"))
            continue
        if not entry.is_dir():
            continue
        if name in excluded:
            result.skipped.append(Skipped(name, "belongs to an existing application"))
            continue
        if not _holds_intake_files(entry):
            result.skipped.append(Skipped(name, "neither posting.txt nor source.txt"))
            continue
        result.folders.append(describe(entry))
    return result


def find(applications_dir: Path, reference: str) -> IntakeFolder:
    """Resolve a named folder: ``NAME``, ``applications/NAME``, or a path.

    The result must be a real directory directly inside ``applications/``; a
    symlink, a missing name, a file, or anything outside that directory raises
    :class:`UsageError`, so an explicit ``--from-folder`` can never escape the
    applications tree.
    """
    base = Path(applications_dir)
    text = (reference or "").strip()
    if not text:
        raise UsageError("no intake folder given")
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        target = candidate
    else:
        parts = candidate.parts
        if len(parts) == 2 and parts[0] == base.name:
            target = base / parts[1]
        elif len(parts) == 1:
            target = base / parts[0]
        else:
            raise UsageError(
                f"{reference!r} is not an intake folder name; pass the folder name below"
                f" {base} (no path separators)"
            )
    name = target.name
    if (not name or name in (".", "..") or name.startswith(".")
            or len(name) > MAX_NAME):
        raise UsageError(f"unusable intake folder name: {reference!r}")
    if target.is_symlink():
        raise UsageError(f"applications/{name} is a symlink; refusing to use it")
    if not target.exists():
        raise UsageError(f"no intake folder {target}")
    if not target.is_dir():
        raise UsageError(f"{target} is not a directory")
    if target.parent.resolve() != base.resolve():
        raise UsageError(f"{target} is not a folder directly inside {base}")
    return describe(target)


# --------------------------------------------------------------------------
# small helpers for the CLI
# --------------------------------------------------------------------------
def normalize_url(value: object) -> str | None:
    """Normalize a posting URL for comparison (case and a trailing slash are noise)."""
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    return text.casefold() or None


def same_url(left: object, right: object) -> bool:
    """True when both values are non-empty and name the same URL."""
    first, second = normalize_url(left), normalize_url(right)
    return first is not None and first == second


@dataclass(frozen=True)
class LinkProposal:
    """A folder and the application it certainly belongs to."""

    folder: str
    application_id: str
    reason: str


@dataclass(frozen=True)
class LinkAmbiguity:
    """A folder and the applications that could belong to it, in no order."""

    folder: str
    application_ids: tuple[str, ...]


def _employer_key(value: object) -> str:
    return str(value or "").strip().casefold()


def plan_links(folders: Sequence[IntakeFolder],
               applications: Sequence[Mapping[str, object]],
               ) -> tuple[list[LinkProposal], list[LinkAmbiguity]]:
    """Match unlinked folders to applications that have no folder yet.

    ``applications`` are mappings with ``id``, ``company``, ``job_url`` and
    ``intake_folder``.  Two conservative passes:

    1. The folder's ``source.txt`` URL is the application's ``job_url``.  The
       URL identifies the posting itself, so this is certain.
    2. The employer matches, and *exactly one* unlinked application and
       *exactly one* unlinked folder carry that employer.  That is an
       application entered before its folder was recognized.

    Everything else is reported as ambiguous instead of guessed: with several
    folders or several applications for one employer, linking the wrong posting
    would be worse than leaving the link empty.
    """
    linked_folders = {str(app["intake_folder"]) for app in applications
                      if app.get("intake_folder")}
    pending = [folder for folder in folders if folder.name not in linked_folders]
    free = [dict(app) for app in applications if not app.get("intake_folder")]
    proposals: list[LinkProposal] = []
    ambiguous: list[LinkAmbiguity] = []

    for folder in pending:
        if folder.source_url is None:
            continue
        matches = [app for app in free if same_url(app.get("job_url"), folder.source_url)]
        if len(matches) == 1:
            proposals.append(LinkProposal(folder.name, str(matches[0]["id"]), "the same job url"))
            free.remove(matches[0])
        elif len(matches) > 1:
            ambiguous.append(LinkAmbiguity(folder.name,
                                           tuple(str(app["id"]) for app in matches)))

    proposed = {proposal.folder for proposal in proposals}
    folders_per_employer = Counter(_employer_key(folder.company) for folder in pending)
    for folder in pending:
        if folder.name in proposed:
            continue
        key = _employer_key(folder.company)
        matches = [app for app in free if _employer_key(app.get("company")) == key]
        if len(matches) == 1 and folders_per_employer[key] == 1:
            proposals.append(LinkProposal(folder.name, str(matches[0]["id"]),
                                          f"the only application for {folder.company}"))
            free.remove(matches[0])
        elif matches:
            ambiguous.append(LinkAmbiguity(folder.name,
                                           tuple(str(app["id"]) for app in matches)))
    return proposals, ambiguous


def source_host(url: str | None) -> str:
    """Short host label for a source URL (``'-'`` when there is none)."""
    if not url:
        return "-"
    try:
        return urlsplit(url).netloc or url
    except ValueError:  # pragma: no cover - defensive
        return url


def pending(folders: Sequence[IntakeFolder],
            linked: Mapping[str, str] | None = None) -> list[IntakeFolder]:
    """The folders no application was registered from yet."""
    linked = linked or {}
    return [folder for folder in folders if folder.name not in linked]


def newest(folders: Sequence[IntakeFolder]) -> IntakeFolder | None:
    """The folder that was most likely just prepared (latest change wins)."""
    return max(folders, key=lambda folder: (folder.mtime, folder.name), default=None)


__all__ = [
    "IntakeFolder",
    "LinkAmbiguity",
    "LinkProposal",
    "Scan",
    "Skipped",
    "build_identifier",
    "describe",
    "find",
    "newest",
    "normalize_link",
    "normalize_url",
    "pending",
    "plan_links",
    "read_posting_file",
    "read_source_url",
    "same_url",
    "scan",
    "source_host",
    "split_identifier",
]
