"""Safe import of posting text, CVs, cover letters, and other files.

Copies are the default; the source file is never moved or deleted. Writes are
staged to a temporary file and finished with an atomic rename, then registered
in SQLite. If registration fails the staged file is removed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import repo
from .errors import JobError, UsageError
from .paths import Layout

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class ImportResult:
    artifact_id: int
    relative_path: str
    absolute_path: Path
    replaced: bool


def application_dir(layout: Layout, app_id: str) -> Path:
    """The artifact directory for one application.

    Symlinks are refused outright. A link inside applications/ could point at
    another application, at applications/ itself, or outside the tree, and
    deletion must never follow it.
    """
    base = layout.applications_dir
    candidate = _lexical_application_dir(base, app_id)
    if candidate.is_symlink():
        raise UsageError(
            f"applications/{app_id} is a symlink; refusing to follow it"
            " (escape risk). Replace it with a real directory."
        )
    resolved = candidate.resolve()
    base_resolved = base.resolve()
    if resolved == base_resolved:
        raise UsageError(
            f"applications/{app_id} resolves to the applications/ directory itself; refusing"
        )
    if resolved.parent != base_resolved:
        raise UsageError(f"resolved application directory escapes applications/ (got {resolved})")
    return resolved


def _lexical_application_dir(base: Path, app_id: str) -> Path:
    if not SAFE_ID.match(app_id) or ".." in app_id:
        raise UsageError(f"unsafe application id: {app_id!r}")
    return base / app_id


def deletion_directory(layout: Layout, app_id: str) -> Path | None:
    """The application directory that may be removed, or None when none exists.

    The result is the lexical ``applications/<id>`` path (never a resolved
    symlink target). Anything that is not exactly ``applications/<id>`` -- a
    symlink (including a link to another application or to applications/), a
    file, or an escaping path -- raises UsageError so the caller can abort
    before changing the database.
    """
    base = layout.applications_dir
    lexical = _lexical_application_dir(base, app_id)
    if lexical.is_symlink():
        raise UsageError(
            f"applications/{app_id} is a symlink; refusing to delete it"
            " (escape risk). Remove the link manually."
        )
    if not lexical.exists():
        return None
    if not lexical.is_dir():
        raise UsageError(f"applications/{app_id} is not a directory; refusing to delete it")
    resolved = lexical.resolve()
    base_resolved = base.resolve()
    if resolved == base_resolved:
        raise UsageError(
            f"applications/{app_id} resolves to the applications/ directory itself; refusing"
        )
    if resolved.parent != base_resolved:
        raise UsageError(f"resolved application directory escapes applications/ (got {resolved})")
    return lexical


def remove_application_dir(layout: Layout, app_id: str) -> Path | None:
    """Delete one application directory after re-checking it is not a symlink."""
    directory = deletion_directory(layout, app_id)
    if directory is None:
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise UsageError(f"applications/{app_id} changed on disk; refusing to delete it")
    shutil.rmtree(directory)
    return directory


def relative_path_for(app_id: str, filename: str) -> str:
    return f"applications/{app_id}/{filename}"


def safe_filename(name: str) -> str:
    base = os.path.basename(name.strip())
    if not base or base in (".", "..") or base.startswith("."):
        raise UsageError(f"unusable filename: {name!r}")
    if "/" in base or "\\" in base:
        raise UsageError(f"filename must not contain a path separator: {name!r}")
    return base


def _next_free_name(directory: Path, filename: str, taken: set[str] | None = None) -> str:
    """Version a filename instead of overwriting: cv.pdf, cv-2.pdf, cv-3.pdf."""
    taken = taken or set()
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    counter = 1
    while (directory / candidate).exists() or candidate in taken:
        counter += 1
        candidate = f"{stem}-{counter}{suffix}"
    return candidate


def import_artifact(
    layout: Layout,
    conn: sqlite3.Connection,
    app_id: str,
    source: str | Path,
    *,
    artifact_type: str = "other",
    label: str | None = None,
    name: str | None = None,
    overwrite: bool = False,
) -> ImportResult:
    app = repo.get_application(conn, app_id)
    source_path = Path(source).expanduser()
    if not source_path.exists():
        raise UsageError(f"file not found: {source_path}")
    if not source_path.is_file():
        raise UsageError(f"not a regular file: {source_path}")
    filename = safe_filename(name or source_path.name)

    directory = application_dir(layout, app_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename

    base = layout.applications_dir.resolve()
    resolved_target = target.resolve()
    if resolved_target != base and base not in resolved_target.parents:
        raise UsageError("target escapes the applications directory")
    if target.resolve() == source_path.resolve() and not overwrite:
        raise UsageError("source and target are the same file; use --overwrite to re-register it")

    relative = relative_path_for(str(app["id"]), filename)
    existing = conn.execute(
        "SELECT id FROM artifacts WHERE relative_path = ?", (relative,)
    ).fetchone()
    if existing is not None and not overwrite and name is not None:
        raise UsageError(
            f"{relative} is already registered; use --overwrite to replace it"
            " or omit --name to store a new version"
        )
    keep_row_id = int(existing["id"]) if (existing is not None and overwrite) else None
    replaced_backup: Path | None = None
    if target.exists() or (existing is not None and not overwrite):
        if not overwrite:
            registered_names = {
                Path(str(row["relative_path"])).name
                for row in repo.artifacts_for(conn, str(app["id"]))
            }
            filename = _next_free_name(directory, filename, registered_names)
            target = directory / filename
            relative = relative_path_for(str(app["id"]), filename)
        else:
            replaced_backup = target.with_name(target.name + ".replaced-" + _stamp())

    handle, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-import-")
    temp = Path(temp_name)
    handle_closed = False
    try:
        with open(handle, "wb") as out, source_path.open("rb") as inp:
            while True:
                chunk = inp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        handle_closed = True
        if replaced_backup is not None:
            target.replace(replaced_backup)
        temp.replace(target)
    except BaseException:
        if not handle_closed:
            with contextlib.suppress(OSError):
                os.close(handle)
        with contextlib.suppress(OSError):
            temp.unlink()
        if replaced_backup is not None and replaced_backup.exists():
            with contextlib.suppress(OSError):
                replaced_backup.replace(target)
        raise
    try:
        if keep_row_id is not None:
            repo.update_artifact(conn, keep_row_id, artifact_type=artifact_type, label=label)
            artifact_id = keep_row_id
        else:
            artifact_id = repo.add_artifact(conn, str(app["id"]), artifact_type, relative, label)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        if replaced_backup is not None and replaced_backup.exists():
            with contextlib.suppress(OSError):
                replaced_backup.replace(target)
        raise
    if replaced_backup is not None and replaced_backup.exists():
        with contextlib.suppress(OSError):
            replaced_backup.unlink()
    return ImportResult(artifact_id, relative, target, replaced_backup is not None)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")


def registered_paths(conn: sqlite3.Connection) -> set[str]:
    return {str(row["relative_path"]) for row in repo.all_artifacts(conn)}


def files_on_disk(layout: Layout) -> list[str]:
    base = layout.applications_dir
    if not base.is_dir():
        return []
    found: list[str] = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.name.startswith(".tmp-import-"):
            found.append(str(path.relative_to(layout.root)))
    return found


def doctor_report(layout: Layout, conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Health of artifact files.

    applications/ may also contain pre-submission intake folders that are not
    registered applications (for example <company> or <company>_<posting-id>).
    Those are reported as untracked folders, never as orphans to delete.
    """
    registered = registered_paths(conn)
    on_disk = set(files_on_disk(layout))
    known_apps = {str(row["id"]) for row in repo.all_rows(
        conn, "SELECT id FROM applications")}
    missing = sorted(p for p in registered if not (layout.root / p).is_file())
    orphans: list[str] = []
    untracked: list[str] = []
    for path in sorted(on_disk):
        if path in registered:
            continue
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] == "applications" and parts[1] in known_apps:
            orphans.append(path)
        else:
            untracked.append(path)
    unsafe = sorted(p for p in registered if not p.startswith("applications/") or "../" in p)
    return {"missing_files": missing, "unregistered_files": orphans, "unsafe_paths": unsafe,
            "untracked_folders": untracked}


def remove_file(layout: Layout, relative_path: str) -> None:
    """Explicit cleanup of an artifact file. Never called automatically."""
    path = (layout.root / relative_path).resolve()
    base = layout.root.resolve()
    if base not in path.parents:
        raise JobError(f"refusing to delete outside the project: {relative_path}")
    path.unlink()
