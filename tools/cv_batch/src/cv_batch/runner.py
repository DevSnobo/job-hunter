"""The per-job pipeline and batch execution.

Safety contract of one job
--------------------------

1. The application folder and the posting are re-validated with no-follow
   semantics right before use (F8): a symlinked/swapped posting or folder fails
   the job instead of leaking another file to the provider.
2. Rendering happens on a tool-owned staging path; the renderer runs in its own
   session and is tracked, so a cancellation (Ctrl-C or timeout) kills the whole
   process group, descendants included (F2).
3. Cancellation is checked before rendering and again inside the *publish gate*
   (a lock also taken by :meth:`Runner.cancel_all`), so a late result from a
   parallel worker can never publish a PDF after cancellation (F2).
4. Run evidence is written atomically **before** the target is placed, and is
   rolled back when placement fails, so evidence and target can never disagree
   in the "successful target without evidence" direction (F7).  A publish
   failure is not retried; it is reported as a failed job with no target.
5. Evidence names contain a hash of the exact folder name and a run-unique
   suffix, so ``A:B`` and ``A?B`` (and same-second parallel runs) cannot clobber
   each other (F9).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import apps as apps_mod
from .build import (
    Selection,
    SelectionValidationError,
    build_cv_dict,
    extract_json_object,
    selection_metadata,
    trim_once,
    validate_selection,
)
from .catalog import Catalog, CatalogError, backlog_for, baseline_for, build_catalog
from .config import DEFAULT_LAYOUT, Paths, TargetNaming
from .model import AgentAdapter, ModelError, ModelRun, Usage, run_model
from .prompt import SYSTEM_PROMPT, Limits, build_user_prompt
from .render import (
    PlaceError,
    RenderError,
    place_no_clobber,
    render_pdf,
    replace_existing,
    replace_invalid,
    terminate_processes,
    validate_pdf_file,
)

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class JobSpec:
    folder: str
    directory: Path
    posting: Path
    language: str
    #: how the CV file of this folder is named (template + identity slug); it is
    #: resolved once at start-up, before any CV data is read (the folder scan
    #: names targets first).
    naming: TargetNaming = TargetNaming(name="")
    #: identities recorded when the folder was scanned; re-checked in the runner
    dir_dev: int | None = None
    dir_ino: int | None = None
    posting_dev: int | None = None
    posting_ino: int | None = None

    @property
    def target(self) -> Path:
        return self.naming.path(self.directory, self.language)

    @classmethod
    def from_application(
        cls, app: apps_mod.Application, language: str, naming: TargetNaming
    ) -> "JobSpec":
        """Build a job from a scanned application, carrying its recorded identity."""
        return cls(
            folder=app.folder,
            directory=app.path,
            posting=app.posting,
            language=language,
            naming=naming,
            dir_dev=getattr(app, "dir_dev", None),
            dir_ino=getattr(app, "dir_ino", None),
            posting_dev=getattr(app, "posting_dev", None),
            posting_ino=getattr(app, "posting_ino", None),
        )


@dataclass
class JobResult:
    job: JobSpec
    status: str
    message: str = ""
    agent: str = ""
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    bullet_count: int = 0
    trims: list[str] = field(default_factory=list)
    pdf: Path | None = None
    input_json: Path | None = None
    #: state-directory copy of the file this job replaced (``None`` when the
    #: target was missing and nothing had to be backed up)
    backup: Path | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def failed(self) -> bool:
        return self.status == FAILED


class JobFailure(RuntimeError):
    pass


class BatchInterrupted(KeyboardInterrupt):
    """Ctrl-C during a batch; carries whatever finished before the interrupt."""

    def __init__(self, summary: "BatchSummary") -> None:
        super().__init__("batch interrupted")
        self.summary = summary


def safe_slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ \-]", "_", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "application"


def folder_digest(folder: str) -> str:
    """Stable short hash of the *exact* folder string (``A:B`` vs ``A?B``)."""
    return hashlib.sha256(folder.encode("utf-8", "surrogatepass")).hexdigest()[:10]


def evidence_slug(folder: str, language: str) -> str:
    """Collision-free evidence identifier for one folder and language."""
    return f"{safe_slug(folder)}-{folder_digest(folder)}-{language}"


def run_stamp() -> str:
    """Second-resolution timestamp; uniqueness comes from the added suffix."""
    return time.strftime("%Y%m%dT%H%M%S")


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON through a temporary file and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


class Runner:
    """Owns configuration and shared state for one batch."""

    def __init__(
        self,
        *,
        paths: Paths,
        language: str,
        agent: AgentAdapter,
        generator_exe: str | None,
        layout: str = DEFAULT_LAYOUT,
        limits: Limits,
        posting_chars: int,
        model_timeout: float,
        render_timeout: float,
        trimming: bool,
        trim_attempts: int,
        content_retries: int,
        allow_replace_invalid: bool,
        allow_overwrite: bool = False,
        log: Callable[[str], None] = print,
    ) -> None:
        self.paths = paths
        self.language = language
        self.agent = agent
        self.generator_exe = generator_exe
        #: renderer layout name, forwarded verbatim to ``cvgen render --layout``
        self.layout = layout
        self.limits = limits
        self.posting_chars = posting_chars
        self.model_timeout = model_timeout
        self.render_timeout = render_timeout
        self.trimming = trimming
        self.trim_attempts = trim_attempts
        self.content_retries = content_retries
        self.allow_replace_invalid = allow_replace_invalid
        #: ``--overwrite``: an existing *valid* CV may be replaced deliberately.
        self.allow_overwrite = allow_overwrite
        self.log = log
        self._catalogs: dict[str, Catalog] = {}
        self._catalog_lock = threading.Lock()
        self.cancel = threading.Event()
        self._live: dict[int, subprocess.Popen] = {}
        self._live_lock = threading.Lock()
        #: held while evidence is written and the target is placed, and taken by
        #: ``cancel_all`` before the cancellation flag is set (F2 publish gate).
        self._publish_lock = threading.Lock()

    # ------------------------------------------------------- child processes
    def track(self, process: subprocess.Popen) -> None:
        """Register a live model/renderer child; refuse new ones after cancel."""
        with self._live_lock:
            self._live[process.pid] = process
            if not self.cancel.is_set():
                return
            # Cancellation raced this start: terminate it immediately.
            processes = list(self._live.values())
            self._live.clear()
        terminate_processes(processes)

    def untrack(self, process: subprocess.Popen) -> None:
        with self._live_lock:
            self._live.pop(process.pid, None)

    def cancel_all(self) -> None:
        """Cancel the batch and terminate every live child process group.

        Setting the flag under the publish gate guarantees that either a
        placement already finished before the cancellation, or it will refuse to
        run: a late parallel result can never publish after cancellation.
        """
        with self._publish_lock:
            self.cancel.set()
        with self._live_lock:
            processes = list(self._live.values())
            self._live.clear()
        terminate_processes(processes)

    # ---------------------------------------------------------------- catalog
    def catalog(self, language: str) -> Catalog:
        with self._catalog_lock:
            if language in self._catalogs:
                return self._catalogs[language]
        baseline = baseline_for(language, self.paths.profile_dir)
        backlog = backlog_for(language, self.paths.backlog_de, self.paths.backlog_en)
        catalog = build_catalog(language=language, baseline_path=baseline, backlog_path=backlog)
        with self._catalog_lock:
            self._catalogs[language] = catalog
        return catalog

    # ------------------------------------------------------------------- job
    def run_job(self, job: JobSpec) -> JobResult:
        started = time.monotonic()
        staged: list[Path] = []
        try:
            result = self._run_job(job, started, staged)
        except KeyboardInterrupt:
            raise
        except JobFailure as exc:
            result = JobResult(job, FAILED, str(exc))
        except (CatalogError, RenderError, PlaceError, ModelError, apps_mod.PostingReadError, OSError) as exc:
            result = JobResult(job, FAILED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            result = JobResult(job, FAILED, f"unexpected {type(exc).__name__}: {exc}")
        finally:
            for directory in staged:
                shutil.rmtree(directory, ignore_errors=True)
        result.duration = time.monotonic() - started
        return result

    def _run_job(self, job: JobSpec, started: float, staged: list[Path]) -> JobResult:
        directory = job.directory
        self._check_directory(job)
        target = job.target
        if target.parent != directory:
            raise JobFailure(f"refusing to write outside {directory}: {target}")
        state = apps_mod.classify_target(target)
        overwriting = False
        if state.is_valid:
            # An existing valid CV is only rewritten with the explicit
            # ``--overwrite`` opt-in; otherwise the job is a deliberate no-op.
            if not self.allow_overwrite:
                return JobResult(job, SKIPPED, f"valid CV already exists ({target.name})")
            overwriting = True
        elif state.state == apps_mod.INVALID:
            if not state.replaceable:
                raise JobFailure(f"target {target.name} is not replaceable ({state.reason})")
            if not (self.allow_replace_invalid or self.allow_overwrite):
                raise JobFailure(
                    f"existing target {target.name} is invalid ({state.reason}); "
                    f"pass --regenerate-invalid (or --overwrite) to replace it"
                )

        catalog = self.catalog(job.language)
        dir_fd = self._open_directory(job)
        try:
            # F8: read through the validated folder descriptor; a swap of the
            # folder path after this point cannot redirect the read.
            posting = apps_mod.read_posting(
                job.posting,
                self.posting_chars,
                expected_dev=job.posting_dev,
                expected_ino=job.posting_ino,
                dir_fd=dir_fd,
            )
        finally:
            os.close(dir_fd)
        selection, run = self._select(catalog, posting)

        cv = build_cv_dict(self._baseline(job.language), catalog, selection)
        stage = (
            self.paths.staging
            / f"{safe_slug(job.folder)}-{folder_digest(job.folder)[:6]}-{job.language}-{uuid.uuid4().hex[:8]}"
        )
        stage.mkdir(parents=True, exist_ok=True)
        staged.append(stage)
        staged_pdf = stage / target.name
        staged_input = stage / "input.json"
        trims: list[str] = []

        attempts = 0
        while True:
            self._raise_if_cancelled("before rendering")
            attempts += 1
            staged_input.write_text(json.dumps(cv, ensure_ascii=False, indent=2), encoding="utf-8")
            if staged_pdf.exists():
                staged_pdf.unlink()
            try:
                render = render_pdf(
                    generator_project=self.paths.generator_project,
                    generator_exe=self.generator_exe,
                    input_path=staged_input,
                    output_path=staged_pdf,
                    layout=self.layout,
                    timeout=self.render_timeout,
                    on_start=self.track,
                    on_end=self.untrack,
                )
            except RenderError:
                if self.cancel.is_set():
                    raise JobFailure("cancelled while rendering; no PDF was published") from None
                raise
            if render.ok:
                break
            # Only a layout overflow reaches this branch as a value.
            trimmed, note = trim_once(selection, catalog)
            if not self.trimming or note is None or attempts > self.trim_attempts:
                raise JobFailure(
                    f"layout overflow and no safe trim left after {attempts} attempt(s); "
                    f"no PDF was written for {job.folder}"
                )
            selection = trimmed
            trims.append(note)
            cv = build_cv_dict(self._baseline(job.language), catalog, selection)
            self.log(f"  trim: {note}")

        self._raise_if_cancelled("before publishing")
        # ``cv`` is the exact generator JSON that produced the accepted staged PDF
        # (post-trim when trimming happened).
        payload = self._evidence_payload(job, selection, catalog, run, cv, target, trims, state)
        saved_input, backup = self._publish(
            job=job,
            state=state,
            target=target,
            staged_pdf=staged_pdf,
            payload=payload,
            overwriting=overwriting,
        )
        bullets = sum(len(entry.bullet_ids) for entry in selection.entries)
        replaced_what = (
            "overwrote an existing valid CV" if overwriting else "replaced an invalid file"
        )
        message = f"wrote {target.name}" + (
            f" ({replaced_what}; backup: {backup.name})" if backup else ""
        )
        return JobResult(
            job,
            OK,
            message,
            agent=run.label,
            session_id=run.session_id,
            usage=run.usage,
            bullet_count=bullets,
            trims=trims,
            pdf=target,
            input_json=saved_input,
            backup=backup,
        )

    # --------------------------------------------------------------- helpers
    def _raise_if_cancelled(self, where: str) -> None:
        if self.cancel.is_set():
            raise JobFailure(f"cancelled {where}; nothing was published")

    def _check_directory(self, job: JobSpec) -> None:
        """Validate the application folder without following links (F8)."""
        directory = job.directory
        if job.posting.parent != directory:
            raise JobFailure(f"posting {job.posting} is outside {directory}")
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise JobFailure(f"application folder is not readable: {directory} ({exc})") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise JobFailure(f"application folder is not a real directory: {directory}")
        if job.dir_dev is not None and (info.st_dev, info.st_ino) != (job.dir_dev, job.dir_ino):
            raise JobFailure(f"application folder changed since the scan; refusing to write: {directory}")

    def _open_directory(self, job: JobSpec) -> int:
        """Open and validate the application folder without following links (F8).

        The returned descriptor is the folder the posting must be read from; the
        caller closes it.  Its identity must match the scan and the ``lstat``
        check performed immediately before the open.
        """
        self._check_directory(job)
        directory = job.directory
        info = os.lstat(directory)
        dir_fd = apps_mod.open_directory(directory)
        opened = os.fstat(dir_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(dir_fd)
            raise JobFailure(f"application folder changed while it was opened: {directory}")
        return dir_fd

    def _check_destination(self, job: JobSpec) -> None:
        """Re-check the destination folder right before the final placement (F8)."""
        directory = job.directory
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise JobFailure(f"application folder disappeared before placement: {directory} ({exc})") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise JobFailure(f"application folder is not a real directory: {directory}")
        if job.dir_dev is not None and (info.st_dev, info.st_ino) != (job.dir_dev, job.dir_ino):
            raise JobFailure(f"application folder changed before placement: {directory}")

    def _baseline(self, language: str) -> dict:
        path = baseline_for(language, self.paths.profile_dir)
        return json.loads(path.read_text(encoding="utf-8"))

    def _select(self, catalog: Catalog, posting: str) -> tuple[Selection, ModelRun]:
        retries = self.content_retries
        validation_error: str | None = None
        prompt = build_user_prompt(
            language=catalog.language, catalog=catalog, posting=posting, limits=self.limits
        )
        while True:
            try:
                run = run_model(
                    self.agent,
                    prompt=prompt,
                    system_prompt=SYSTEM_PROMPT,
                    timeout=self.model_timeout,
                    on_start=self.track,
                    on_end=self.untrack,
                )
            except ModelError as exc:
                if self.cancel.is_set():
                    raise JobFailure("cancelled") from exc
                raise JobFailure(f"model call failed: {exc}") from exc

            try:
                data = extract_json_object(run.text)
                selection = validate_selection(
                    data,
                    catalog,
                    max_bullets_per_entry=self.limits.max_bullets_per_entry,
                    max_bullets_total=self.limits.max_bullets_total,
                    max_headline=self.limits.max_headline,
                )
                return selection, run
            except SelectionValidationError as exc:
                if retries <= 0:
                    raise JobFailure(
                        f"model answer rejected and no retry left: {exc}"
                    ) from exc
                retries -= 1
                validation_error = str(exc)
                self.log(f"  retry: model answer rejected ({exc})")
                prompt = build_user_prompt(
                    language=catalog.language,
                    catalog=catalog,
                    posting=posting,
                    limits=self.limits,
                    validation_error=validation_error,
                )

    # ------------------------------------------------- evidence and publish
    def _evidence_payload(
        self,
        job: JobSpec,
        selection: Selection,
        catalog: Catalog,
        run: ModelRun,
        cv: dict,
        target: Path,
        trims: list[str],
        state: apps_mod.TargetState,
    ) -> dict:
        return {
            # the exact JSON handed to the renderer for the accepted PDF
            "generator_input": cv,
            "generator_input_source": "rendered (post-trim)",
            # additive: how the PDF was produced (which renderer layout), so a
            # finished CV stays reproducible from the evidence alone
            "layout": self.layout,
            "selection": selection_metadata(selection, catalog),
            "job": {
                "folder": job.folder,
                "posting": str(job.posting),
                "target_pdf": str(target),
                "language": job.language,
                "existing_target_state": state.state,
                "existing_target_reason": state.reason,
                "backup": None,
            },
            "agent": {
                "command": list(run.command),
                "label": run.label,
                "session_id": run.session_id,
                "answer_source": run.answer_source,
                "usage": {
                    "input": run.usage.input,
                    "output": run.usage.output,
                    "cache_read": run.usage.cache_read,
                    "cache_write": run.usage.cache_write,
                    "total": run.usage.total,
                    "cost": run.usage.cost,
                },
            },
            "trims": trims,
            "publish": {
                "prepared_before_placement": True,
                "atomic_with_target": True,
                "target": str(target),
                "backup": None,
            },
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _write_evidence(self, job: JobSpec, payload: dict) -> tuple[Path, Path]:
        """Write the canonical and the run evidence atomically (all or nothing)."""
        self.paths.ensure()
        slug = evidence_slug(job.folder, job.language)
        saved = self.paths.inputs / f"{slug}.json"
        run_file = self.paths.runs / f"{run_stamp()}-{slug}-{uuid.uuid4().hex[:6]}.json"
        _atomic_write_json(saved, payload)
        try:
            _atomic_write_json(run_file, payload)
        except BaseException:
            _unlink_quietly(saved)
            raise
        return saved, run_file

    def _publish(
        self,
        *,
        job: JobSpec,
        state: apps_mod.TargetState,
        target: Path,
        staged_pdf: Path,
        payload: dict,
        overwriting: bool = False,
    ) -> tuple[Path, Path | None]:
        """Write evidence, then place the target — atomically with cancellation.

        The publish gate is shared with :meth:`cancel_all`, so a late result from
        a parallel worker cannot place a PDF after a cancellation.  Evidence is
        written first (atomically) and rolled back when placement fails, so a
        failure can never leave a successful target without evidence.

        ``overwriting`` marks the ``--overwrite`` case (the target was a valid CV
        when it was inspected); both replacement paths back the old bytes up.
        """
        backup: Path | None = None
        if state.state == apps_mod.INVALID or overwriting:
            backup = self.paths.replaced / f"{target.name}.{uuid.uuid4().hex[:8]}.bak"
            payload["job"]["backup"] = str(backup)
            payload["publish"]["backup"] = str(backup)
        with self._publish_lock:
            if self.cancel.is_set():
                raise JobFailure("cancelled before publishing; the staged PDF was discarded")
            saved, run_file = self._write_evidence(job, payload)
            try:
                self._check_destination(job)
                validate_pdf_file(staged_pdf)
                if state.state == apps_mod.INVALID:
                    replace_invalid(
                        staged_pdf,
                        target,
                        self.paths.replaced,
                        backup_path=backup,
                        expect_dev=state.dev,
                        expect_ino=state.ino,
                    )
                elif overwriting:
                    # The inspected inode must still be a valid PDF: a target
                    # that changed in between is refused, not overwritten.
                    replace_existing(
                        staged_pdf,
                        target,
                        self.paths.replaced,
                        backup_path=backup,
                        expect_dev=state.dev,
                        expect_ino=state.ino,
                    )
                else:
                    place_no_clobber(staged_pdf, target)
            except BaseException:
                _unlink_quietly(saved)
                _unlink_quietly(run_file)
                raise
        return saved, backup


@dataclass
class BatchSummary:
    results: list[JobResult] = field(default_factory=list)

    @property
    def ok(self) -> list[JobResult]:
        return [r for r in self.results if r.status == OK]

    @property
    def skipped(self) -> list[JobResult]:
        return [r for r in self.results if r.status == SKIPPED]

    @property
    def failed(self) -> list[JobResult]:
        return [r for r in self.results if r.status == FAILED]

    def usage(self) -> Usage:
        total = Usage()
        for result in self.results:
            total.add(result.usage)
        return total


def run_batch(
    runner: Runner,
    jobs: list[JobSpec],
    *,
    parallel: int,
    log: Callable[[str], None] = print,
) -> BatchSummary:
    """Run the selected jobs; failures never stop the remaining jobs."""
    summary = BatchSummary()
    if parallel <= 1:
        try:
            for job in jobs:
                if runner.cancel.is_set():
                    break
                log(f"-> {job.folder} [{job.language}]")
                result = runner.run_job(job)
                _report(result, log)
                summary.results.append(result)
        except KeyboardInterrupt:
            runner.cancel_all()
            raise BatchInterrupted(summary) from None
        return summary

    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futures = {}
    try:
        for job in jobs:
            if runner.cancel.is_set():
                break
            futures[pool.submit(runner.run_job, job)] = job
        for future in list(futures):
            job = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                result = JobResult(job, FAILED, f"unexpected {type(exc).__name__}: {exc}")
            with lock:
                _report(result, log)
                summary.results.append(result)
    except KeyboardInterrupt:
        runner.cancel_all()
        pool.shutdown(wait=False, cancel_futures=True)
        raise BatchInterrupted(summary) from None
    finally:
        if not runner.cancel.is_set():
            pool.shutdown(wait=True)
    summary.results.sort(key=lambda r: jobs.index(r.job))
    return summary


def _report(result: JobResult, log: Callable[[str], None]) -> None:
    if result.status == OK:
        details = [f"agent={result.agent}", f"bullets={result.bullet_count}"]
        if result.session_id:
            details.append(f"session={result.session_id}")
        if result.backup is not None:
            details.append(f"backup={result.backup.name}")
        if result.usage and not result.usage.empty:
            details.append(result.usage.describe())
        log(f"ok  : {result.job.folder} [{result.job.language}] -> {result.job.target.name} ({', '.join(details)})")
    elif result.status == SKIPPED:
        log(f"skip: {result.job.folder} [{result.job.language}] {result.message}")
    else:
        log(f"fail: {result.job.folder} [{result.job.language}] {result.message}")


__all__ = [
    "BatchInterrupted",
    "BatchSummary",
    "JobFailure",
    "JobResult",
    "JobSpec",
    "Runner",
    "evidence_slug",
    "folder_digest",
    "run_batch",
    "run_stamp",
    "safe_slug",
]
