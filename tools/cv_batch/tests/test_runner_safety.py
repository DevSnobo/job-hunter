"""Pipeline safety regressions: fallback race, renderer output, evidence, path swaps.

F3  parallel Astra failures must both retry once onto DeepSeek Flash.
F6  a renderer that exits 0 with junk/empty output is a failure, never a target.
F7  evidence is written atomically before placement and rolled back on failure.
F8  swapped postings/folders fail safely instead of reaching the provider.
F9  invalid replacement stages on the destination filesystem and never
    overwrites a target that another job just made valid.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

import cv_batch.render as render_mod
from cv_batch import apps as apps_mod
from cv_batch.render import PlaceError, replace_invalid, validate_pdf_file
from cv_batch.runner import (
    FAILED,
    OK,
    JobSpec,
    evidence_slug,
    folder_digest,
    run_batch,
)

from conftest import el_entry_id, selection_answer, sp_entry_id, target_name, target_path
from test_runner import EL, SP, VALID_ANSWER, job_for, make_runner

pytestmark = []


def scanned_job(project, folder: str, language: str = "de") -> JobSpec:
    """A job that carries the identity recorded by the real scan (F8)."""
    if not (project.applications / folder).is_dir():
        project.add_app(folder)
    scan = apps_mod.scan(project.applications)
    app = next(item for item in scan.applications if item.folder == folder)
    return JobSpec.from_application(app, language, project.naming(language))


def target_of(project, folder: str, language: str = "de") -> Path:
    return target_path(project.applications / folder, language)


# --------------------------------------------------------------------- F3
def test_simultaneous_astra_failures_both_retry_onto_deepseek_flash(project, fake_agent, fake_renderer):
    """Barrier test: both parallel workers fail on Astra at the same time (F3)."""
    quota = {"exit": 1, "stderr": "error: quota exceeded for openai-codex", "answer": "x"}
    fake_agent.steps(
        [
            {**quota, "barrier": 2},
            {**quota, "barrier": 2},
            {"answer": VALID_ANSWER},
            {"answer": VALID_ANSWER},
        ]
    )
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, "Race One"), job_for(project, "Race Two")]

    summary = run_batch(runner, jobs, parallel=2, log=lambda *_: None)

    assert [result.status for result in summary.results] == [OK, OK], [
        result.message for result in summary.results
    ]
    calls = fake_agent.calls()
    assert len(calls) == 4, "the fallback retry must stay bounded at one per job"
    primary = [call["argv"][call["argv"].index("--model") + 1] for call in calls[:2]]
    fallback = [call["argv"][call["argv"].index("--model") + 1] for call in calls[2:]]
    assert primary == ["gpt-6-astra", "gpt-6-astra"]
    assert fallback == ["deepseek-v4-flash", "deepseek-v4-flash"]
    assert runner.models.astra_exhausted is True
    assert "deepseek-v4-pro" not in " ".join(" ".join(call["argv"]) for call in calls)


# --------------------------------------------------------------------- F6
@pytest.mark.parametrize("mode", ["junk", "empty"])
def test_successful_renderer_with_invalid_output_is_a_failure(project, fake_agent, fake_renderer, mode):
    """Exit code 0 with junk/empty output must never be published (F6)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(**{mode: True})
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Junk Render")

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "pdf" in result.message.lower() or "empty" in result.message.lower()
    assert not target_of(project, "Junk Render").exists()
    assert list(project.state.glob("inputs/*.json")) == []
    assert list(project.state.glob("runs/*.json")) == []
    assert list(project.state.glob("staging/*")) == []


# --------------------------------------------------------------------- F7
def test_failed_evidence_write_leaves_no_target_and_reports_failure(project, fake_agent, fake_renderer):
    """Evidence is prepared before placement: a failed write cannot publish (F7)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Evidence Blocked")
    blocked = project.state / "inputs" / f"{evidence_slug(job.folder, 'de')}.json"
    blocked.mkdir(parents=True)  # a directory where the evidence file must go

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "evidence" in result.message.lower() or "directory" in result.message.lower()
    assert blocked.is_dir()
    assert not target_of(project, "Evidence Blocked").exists()
    assert list(project.state.glob("runs/*.json")) == []
    assert list(project.state.glob("staging/*")) == []


def test_evidence_records_the_post_trim_generator_json(project, fake_agent, fake_renderer):
    """The stored generator input is the JSON that was actually rendered (F7)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(overflow_times=1)
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Trimmed Evidence")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert len(fake_renderer.calls()) == 2, "the second render used the trimmed selection"
    saved = __import__("json").loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["trims"], "the trim must be recorded"
    assert len(saved["selection"]["experience"][0]["trimmed"]) == 1
    assert saved["selection"]["experience"][0]["trimmed"] == [f"{EL}/el2-short"]
    rendered_bullets = saved["generator_input"]["experience"][0]["bullets"]
    assert rendered_bullets == [saved["selection"]["experience"][0]["bullets"][0]["text"]]
    assert saved["generator_input_source"].startswith("rendered")
    assert saved["publish"]["prepared_before_placement"] is True


def test_target_that_loses_the_race_rolls_the_evidence_back(project, fake_agent, fake_renderer, monkeypatch):
    """Placement failure removes the already written evidence (F7)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Lost Race")
    target = target_of(project, "Lost Race")
    real_render = render_mod.render_pdf

    def render_then_race(**kwargs):
        result = real_render(**kwargs)
        target.write_bytes(b"%PDF-1.4 published by another run")
        return result

    monkeypatch.setattr("cv_batch.runner.render_pdf", render_then_race)

    real = runner.run_job(job)

    assert real.status == FAILED
    assert target.read_bytes() == b"%PDF-1.4 published by another run"
    assert list(project.state.glob("inputs/*.json")) == []
    assert list(project.state.glob("runs/*.json")) == []


def test_evidence_names_are_collision_free_for_similar_folder_names(project, fake_agent, fake_renderer):
    """``A:B`` and ``A?B`` (and same-second runs) must not clobber each other (F9)."""
    assert evidence_slug("A:B", "de") != evidence_slug("A?B", "de")
    assert evidence_slug("A:B", "de") == evidence_slug("A:B", "de")
    assert folder_digest("A:B") != folder_digest("A?B")
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, "A:B"), job_for(project, "A?B")]

    summary = run_batch(runner, jobs, parallel=2, log=lambda *_: None)

    assert [result.status for result in summary.results] == [OK, OK]
    assert len(list(project.state.glob("inputs/*.json"))) == 2
    assert len(list(project.state.glob("runs/*.json"))) == 2


# --------------------------------------------------------------------- F8
def test_swapped_posting_symlink_fails_before_the_model_call(project, fake_agent, fake_renderer, tmp_path):
    """A posting swapped for a symlink must not reach the provider (F8)."""
    secret = tmp_path / "private-source.txt"
    secret.write_text("PRIVATE-GITHUB-TOKEN", encoding="utf-8")
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = scanned_job(project, "Swapped")
    job.posting.unlink()
    os.symlink(secret, job.posting)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "PostingReadError" in result.message
    assert fake_agent.calls() == [], "the private file must never be sent to the model"
    assert not target_of(project, "Swapped").exists()


def test_posting_replaced_by_a_different_file_fails_before_the_model_call(project, fake_agent, fake_renderer):
    """The scanned posting inode is re-checked before reading (F8)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = scanned_job(project, "Replaced Posting")
    job.posting.unlink()
    job.posting.write_text("a different posting", encoding="utf-8")

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "changed since the scan" in result.message
    assert fake_agent.calls() == []


def test_application_folder_swapped_for_a_symlink_is_rejected(project, fake_agent, fake_renderer):
    """A folder swapped for a symlink after the scan fails safely (F8)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = scanned_job(project, "Swapped Folder")
    real = job.directory
    moved = real.with_name(real.name + ".moved")
    real.rename(moved)
    os.symlink(moved, real)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "not a real directory" in result.message
    assert fake_agent.calls() == []
    assert not (moved / target_name(job.folder, "de")).exists()


# --------------------------------------------------------------------- F9
def test_invalid_replacement_stages_on_the_destination_filesystem(project, fake_agent, fake_renderer, monkeypatch):
    """The last step of an invalid replacement is a same-directory rename (F9)."""
    replaced: list[tuple[Path, Path]] = []
    real_os = render_mod.os

    class SpyOS:
        def __getattr__(self, name):  # pragma: no cover - delegation
            return getattr(real_os, name)

        def replace(self, src, dst):
            replaced.append((Path(src), Path(dst)))
            return real_os.replace(src, dst)

    monkeypatch.setattr(render_mod, "os", SpyOS())
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer, allow_replace_invalid=True)
    job = job_for(project, "Cross FS")
    target = target_of(project, "Cross FS")
    target.write_bytes(b"junk that is not a pdf")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert replaced, "the replacement must use os.replace"
    source, destination = replaced[0]
    assert destination == target
    assert source.parent == target.parent, "the staged copy must live on the destination filesystem"
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"junk that is not a pdf"


def test_invalid_replacement_refuses_a_target_that_became_valid(project, fake_agent, fake_renderer, monkeypatch):
    """A target that another job just made valid is never overwritten (F9)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer, allow_replace_invalid=True)
    job = job_for(project, "Flipped Valid")
    target = target_of(project, "Flipped Valid")
    target.write_bytes(b"junk that is not a pdf")
    real_render = render_mod.render_pdf

    def render_then_fix(**kwargs):
        result = real_render(**kwargs)
        target.write_bytes(b"%PDF-1.4 now a valid file")  # same inode, now a PDF
        return result

    monkeypatch.setattr("cv_batch.runner.render_pdf", render_then_fix)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "refusing" in result.message
    assert target.read_bytes() == b"%PDF-1.4 now a valid file"
    assert list(project.state.glob("replaced/*.bak")) == []
    assert list(project.state.glob("inputs/*.json")) == []
    assert list(project.state.glob("runs/*.json")) == []


def test_overwrite_refuses_a_target_that_changed_before_placement(project, fake_agent, fake_renderer, monkeypatch):
    """An existing valid CV that changed while rendering is refused, not overwritten."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer, allow_overwrite=True)
    job = job_for(project, "Changed Valid")
    target = target_of(project, "Changed Valid")
    target.write_bytes(b"%PDF-1.4 original")
    real_render = render_mod.render_pdf

    def render_then_change(**kwargs):
        result = real_render(**kwargs)
        target.write_bytes(b"not a pdf any more")  # same inode, no longer a PDF
        return result

    monkeypatch.setattr("cv_batch.runner.render_pdf", render_then_change)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "refusing" in result.message
    assert target.read_bytes() == b"not a pdf any more"
    assert list(project.state.glob("replaced/*.bak")) == []
    assert list(project.state.glob("inputs/*.json")) == []
    assert list(project.state.glob("runs/*.json")) == []


def test_replace_existing_checks_the_inspected_inode_and_the_pdf_header(tmp_path):
    """A target replaced by another inode, or emptied, is refused (--overwrite)."""
    from cv_batch.render import replace_existing

    target = tmp_path / "CV.pdf"
    target.write_bytes(b"%PDF-1.4 old")
    info = target.stat()
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(b"%PDF-1.4 new")

    target.unlink()
    target.write_bytes(b"%PDF-1.4 written by another run")
    with pytest.raises(PlaceError) as excinfo:
        replace_existing(staged, target, tmp_path / "backups", expect_dev=info.st_dev, expect_ino=info.st_ino)
    assert "changed" in str(excinfo.value)
    assert target.read_bytes() == b"%PDF-1.4 written by another run"
    assert list((tmp_path / "backups").glob("*")) == []

    same_inode = tmp_path / "Same.pdf"
    same_inode.write_bytes(b"%PDF-1.4 old")
    info = same_inode.stat()
    same_inode.write_bytes(b"")
    with pytest.raises(PlaceError) as excinfo:
        replace_existing(
            staged, same_inode, tmp_path / "backups", expect_dev=info.st_dev, expect_ino=info.st_ino
        )
    assert "refusing" in str(excinfo.value)
    assert same_inode.read_bytes() == b""


def test_replace_invalid_checks_the_inspected_inode(tmp_path):
    """A target replaced by a different inode is refused (F8/F9)."""
    target = tmp_path / "CV.pdf"
    target.write_bytes(b"junk")
    info = target.stat()
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(b"%PDF-1.4 new")
    target.unlink()
    target.write_bytes(b"another junk file")

    with pytest.raises(PlaceError) as excinfo:
        replace_invalid(
            staged,
            target,
            tmp_path / "backups",
            expect_dev=info.st_dev,
            expect_ino=info.st_ino,
        )

    assert "changed" in str(excinfo.value)
    assert target.read_bytes() == b"another junk file"


# ------------------------------------------------- F9: genuine cross-filesystem runs

@pytest.fixture
def other_filesystem_state(tmp_path):
    """A state directory on a *different* filesystem (skips when unavailable)."""
    for base in (Path("/dev/shm"), Path("/run/user") / str(os.getuid())):
        if not base.is_dir() or not os.access(base, os.W_OK):
            continue
        if base.stat().st_dev == tmp_path.stat().st_dev:
            continue
        directory = base / f"cvs-exdev-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        directory.mkdir()
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return
    pytest.skip("no writable second filesystem for the EXDEV test")


def test_cross_filesystem_missing_target_is_copied_into_place(
    project, fake_agent, fake_renderer, other_filesystem_state
):
    """A state directory on another filesystem still places a missing target (F9)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer, state_dir=other_filesystem_state)
    job = job_for(project, "Cross Device")
    assert os.stat(other_filesystem_state).st_dev != os.stat(job.directory).st_dev

    result = runner.run_job(job)

    assert result.status == OK, result.message
    target = target_of(project, "Cross Device")
    assert target.read_bytes().startswith(b"%PDF-")
    assert list(other_filesystem_state.glob("staging/*")) == []
    assert sorted(p.name for p in job.directory.iterdir()) == sorted(
        ["posting.txt", "source.txt", target.name]
    )


def test_cross_filesystem_invalid_replacement_works(
    project, fake_agent, fake_renderer, other_filesystem_state
):
    """An invalid target is replaced even when the state dir is on another device (F9)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(
        project, fake_agent, fake_renderer, allow_replace_invalid=True, state_dir=other_filesystem_state
    )
    job = job_for(project, "Cross Device Replace")
    target = target_of(project, "Cross Device Replace")
    target.write_bytes(b"junk that is not a pdf")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(other_filesystem_state.glob("replaced/*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"junk that is not a pdf"
    assert not list(job.directory.glob(".*.part"))


# ------------------------------------------------- F2/F6: timeouts and staged output

def test_renderer_timeout_kills_the_group_and_leaves_no_target(project, fake_agent, fake_renderer, tmp_path):
    """A render timeout kills the renderer and its descendant, and publishes nothing."""
    marker = tmp_path / "timeout-descendant.txt"
    fake_renderer.configure(sleep=30, descendant=str(marker), descendant_sleep=2.0)
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    runner.render_timeout = 1.0
    job = job_for(project, "Timeout Render")

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "timed out" in result.message
    assert not target_of(project, "Timeout Render").exists()
    time.sleep(3.0)
    assert not marker.exists(), "a renderer descendant survived the timeout"
    assert list(project.state.glob("staging/*")) == []


def test_validate_pdf_file_rejects_symlinks_junk_and_empty_files(tmp_path):
    """Only a regular, non-symlink, non-empty ``%PDF-`` file is accepted (F6)."""
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.7 body")
    validate_pdf_file(good)

    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    with pytest.raises(render_mod.RenderError):
        validate_pdf_file(junk)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(render_mod.RenderError):
        validate_pdf_file(empty)

    link = tmp_path / "link.pdf"
    os.symlink(good, link)
    with pytest.raises(render_mod.RenderError):
        validate_pdf_file(link)

    missing = tmp_path / "missing.pdf"
    with pytest.raises(render_mod.RenderError):
        validate_pdf_file(missing)
