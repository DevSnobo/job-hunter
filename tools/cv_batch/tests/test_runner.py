"""End-to-end pipeline tests with fake model and fake renderer subprocesses."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cv_batch.apps import classify_target
from cv_batch.config import build_paths
from cv_batch.prompt import Limits
from cv_batch.runner import FAILED, OK, SKIPPED, JobSpec, Runner, run_batch
from cv_batch.sessions import ModelPolicy, SessionPolicy

from conftest import el_entry_id, selection_answer, sp_entry_id, target_name, target_path

EL = el_entry_id()
SP = sp_entry_id()

VALID_ANSWER = selection_answer({EL: [f"{EL}/el1-short", f"{EL}/el2-short"], SP: [f"{SP}/b1"]})


def make_runner(
    project,
    fake_agent,
    fake_renderer=None,
    *,
    language="de",
    model="auto",
    session_mode="fresh",
    allow_replace_invalid=False,
    allow_overwrite=False,
    trimming=True,
    content_retries=1,
    layout="classic",
    log=None,
    state_dir=None,
):
    kwargs = project.kwargs()
    if state_dir is not None:
        kwargs["state_dir"] = state_dir
    paths = build_paths(**kwargs)
    return Runner(
        paths=paths,
        language=language,
        model_policy=ModelPolicy(model),
        session_policy=SessionPolicy(session_mode, paths.sessions),
        exe=fake_agent.exe,
        generator_exe=fake_renderer.exe if fake_renderer else None,
        layout=layout,
        limits=Limits(max_bullets_per_entry=5, max_bullets_total=13, max_headline=60),
        posting_chars=6000,
        model_timeout=10.0,
        render_timeout=10.0,
        trimming=trimming,
        trim_attempts=8,
        content_retries=content_retries,
        allow_replace_invalid=allow_replace_invalid,
        allow_overwrite=allow_overwrite,
        log=log or (lambda *_: None),
    )


def job_for(project, folder, language="de"):
    directory = project.add_app(folder)
    return JobSpec(
        folder=folder,
        directory=directory,
        posting=directory / "posting.txt",
        language=language,
        naming=project.naming(language),
    )


def test_happy_path_writes_exactly_one_pdf_next_to_the_posting(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Vertex GmbH")
    before = {p.name: p.read_bytes() for p in job.directory.iterdir()}

    result = runner.run_job(job)

    assert result.status == OK, result.message
    target = target_path(job.directory, "de")
    assert result.pdf == target
    assert target.read_bytes().startswith(b"%PDF-")
    after = {p.name: p.read_bytes() for p in job.directory.iterdir()}
    assert set(after) - set(before) == {target.name}
    for name, content in before.items():
        assert after[name] == content
    assert os.stat(job.directory / "posting.txt").st_mtime_ns == os.stat(job.directory / "posting.txt").st_mtime_ns

    # evidence for reproducibility is stored in the tool-owned state directory
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["generator_input"]["experience"][0]["bullets"][0].startswith("Mock-Implementierung")
    assert saved["selection"]["experience"][0]["bullets"][0]["id"] == f"{EL}/el1-short"
    assert saved["job"]["folder"] == "Vertex GmbH"
    assert saved["layout"] == "classic"  # additive: how the PDF was produced
    assert saved["model"]["selector"] == "openai-codex/gpt-6-astra"
    assert list(project.state.glob("runs/*.json"))
    assert list(project.state.glob("staging/*")) == []


def test_the_chosen_layout_is_recorded_and_forwarded(project, fake_agent, fake_renderer):
    """A non-default layout is rendered *and* stored, so a CV stays reproducible."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(layouts=("classic", "compact-2026"))
    runner = make_runner(project, fake_agent, fake_renderer, layout="compact-2026")
    job = job_for(project, "Layout Job")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    argv = fake_renderer.calls()[0]["argv"]
    assert argv[argv.index("--layout") + 1] == "compact-2026"
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["layout"] == "compact-2026"


def test_the_rendered_timeline_is_newest_first_even_when_the_model_reorders(project, fake_agent, fake_renderer):
    """The model may answer in any order; the CV timeline stays newest job first."""
    reversed_answer = selection_answer({SP: [f"{SP}/b1"], EL: [f"{EL}/el1-short"]})
    fake_agent.steps([{"answer": reversed_answer}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Ordered Timeline")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    rendered = [entry["meta"].split("|")[0].strip() for entry in saved["generator_input"]["experience"]]
    assert rendered == ["Analytical Engines Ltd, Berlin", "Numerical Machines GmbH, Leipzig"]
    assert [item["entry"] for item in saved["selection"]["experience"]] == [EL, SP]
    assert saved["selection"]["model_entry_order_normalized"] == [SP, EL]
    assert fake_renderer.calls(), "the fake renderer received the ordered JSON"


def test_skip_when_a_valid_target_exists(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Alpha")
    target = target_path(job.directory, "de")
    target.write_bytes(b"%PDF-1.4 existing")
    runner = make_runner(project, fake_agent, fake_renderer)

    result = runner.run_job(job)

    assert result.status == SKIPPED
    assert target.read_bytes() == b"%PDF-1.4 existing"
    assert fake_agent.calls() == []


def test_valid_target_is_replaced_with_backup_when_overwrite_is_allowed(project, fake_agent, fake_renderer):
    """--overwrite: an existing valid CV is replaced deliberately, old bytes kept."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Alpha")
    target = target_path(job.directory, "de")
    target.write_bytes(b"%PDF-1.4 the previously tailored CV")
    runner = make_runner(project, fake_agent, fake_renderer, allow_overwrite=True)

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert "overwrote an existing valid CV" in result.message
    assert target.read_bytes().startswith(b"%PDF-")
    assert target.read_bytes() != b"%PDF-1.4 the previously tailored CV"
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"%PDF-1.4 the previously tailored CV"
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["job"]["existing_target_state"] == "valid"
    assert saved["job"]["backup"] == str(backups[0])
    assert saved["publish"]["backup"] == str(backups[0])
    assert list(project.state.glob("staging/*")) == []


def test_overwrite_also_replaces_a_safe_invalid_target(project, fake_agent, fake_renderer):
    """--overwrite covers the invalid targets too (the replacement stays safe)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Beta")
    target = target_path(job.directory, "de")
    target.write_bytes(b"junk that is not a pdf")
    runner = make_runner(project, fake_agent, fake_renderer, allow_overwrite=True)

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert "replaced an invalid file" in result.message
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"junk that is not a pdf"


def test_invalid_target_is_reported_and_untouched_without_opt_in(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Beta")
    target = target_path(job.directory, "de")
    target.write_bytes(b"")
    runner = make_runner(project, fake_agent, fake_renderer)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "invalid" in result.message and "--regenerate-invalid" in result.message
    assert target.read_bytes() == b""
    assert fake_agent.calls() == []


def test_invalid_target_is_replaced_with_backup_when_requested(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Beta")
    target = target_path(job.directory, "de")
    target.write_bytes(b"junk that is not a pdf")
    runner = make_runner(project, fake_agent, fake_renderer, allow_replace_invalid=True)

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"junk that is not a pdf"
    assert "backup" in result.message


def test_symlinked_target_is_never_replaced(project, fake_agent, fake_renderer, tmp_path):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Gamma")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 real file elsewhere")
    target = target_path(job.directory, "de")
    os.symlink(outside, target)
    runner = make_runner(project, fake_agent, fake_renderer, allow_replace_invalid=True)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "symlink" in result.message
    assert target.is_symlink()
    assert outside.read_bytes() == b"%PDF-1.4 real file elsewhere"


def test_renderer_failure_leaves_no_pdf_and_cleans_staging(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(fail=True)
    job = job_for(project, "Delta")
    runner = make_runner(project, fake_agent, fake_renderer)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "photo not found" in result.message
    assert not (target_path(job.directory, "de")).exists()
    assert list(project.state.glob("staging/*")) == []
    assert sorted(p.name for p in job.directory.iterdir()) == ["posting.txt", "source.txt"]


def test_overflow_is_repaired_by_trimming_then_succeeds(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(overflow_times=2)
    job = job_for(project, "Epsilon")
    messages: list[str] = []
    runner = make_runner(project, fake_agent, fake_renderer, log=messages.append)

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert len(result.trims) == 2
    assert len(fake_renderer.calls()) == 3
    assert (target_path(job.directory, "de")).exists()
    assert any("trim:" in message for message in messages)


def test_overflow_with_trimming_disabled_fails_cleanly(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    fake_renderer.configure(overflow_times=1)
    job = job_for(project, "Zeta")
    runner = make_runner(project, fake_agent, fake_renderer, trimming=False)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "overflow" in result.message
    assert not (target_path(job.directory, "de")).exists()


def test_invalid_model_answer_is_retried_once_with_the_validator_message(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": json.dumps({"experience": [{"entry": EL, "bullets": ["invented/id"]}]})},
            {"answer": VALID_ANSWER},
        ]
    )
    job = job_for(project, "Eta")
    runner = make_runner(project, fake_agent, fake_renderer)

    result = runner.run_job(job)

    assert result.status == OK, result.message
    calls = fake_agent.calls()
    assert len(calls) == 2
    assert "rejected by the validator" in calls[1]["argv"][-1]
    assert "unknown candidate id" in calls[1]["argv"][-1]
    assert len(fake_renderer.calls()) == 1


def test_invalid_model_answer_without_retries_fails_before_rendering(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": '{"experience": [{"entry": "exp/nope", "bullets": ["x"]}]}'}])
    job = job_for(project, "Theta")
    runner = make_runner(project, fake_agent, fake_renderer, content_retries=0)

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "no retry left" in result.message
    assert fake_renderer.calls() == []
    assert not (target_path(job.directory, "de")).exists()


def test_prompt_is_bounded_and_excludes_source_url_and_tool_instructions(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    job = job_for(project, "Iota")
    runner = make_runner(project, fake_agent, fake_renderer)

    runner.run_job(job)

    argv = fake_agent.calls()[0]["argv"]
    prompt = argv[-1]
    assert "Stellenanzeige fuer Iota" in prompt
    assert "example.invalid" not in prompt  # source.txt is never read
    system = argv[argv.index("--system-prompt") + 1]
    assert "untrusted data" in system
    assert "no markdown fences" in system
    for flag in ("--no-tools", "--no-builtin-tools", "--no-extensions", "--no-skills"):
        assert flag in argv


def test_quota_failure_switches_to_deepseek_flash_for_the_rest_of_the_batch(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"exit": 1, "stderr": "error: quota exceeded for openai-codex", "answer": "ignored"},
            {"answer": VALID_ANSWER},
        ]
    )
    messages: list[str] = []
    runner = make_runner(project, fake_agent, fake_renderer, log=messages.append)
    jobs = [job_for(project, "Kappa"), job_for(project, "Lambda")]

    summary = run_batch(runner, jobs, parallel=1, log=messages.append)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = fake_agent.calls()
    assert len(calls) == 3
    providers = [call["argv"][call["argv"].index("--provider") + 1] for call in calls]
    models = [call["argv"][call["argv"].index("--model") + 1] for call in calls]
    assert providers == ["openai-codex", "deepseek", "deepseek"]
    assert models == ["gpt-6-astra", "deepseek-v4-flash", "deepseek-v4-flash"]
    assert "deepseek-v4-pro" not in " ".join(" ".join(call["argv"]) for call in calls)
    assert runner.models.astra_exhausted is True
    assert any("switching to" in message for message in messages)


def test_unusable_resume_falls_back_to_a_fresh_session_once(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": VALID_ANSWER, "session_id": "sess-A"},
            {"exit": 1, "stderr": "error: unknown session id sess-A", "answer": "x"},
            {"answer": VALID_ANSWER, "session_id": "sess-B"},
        ]
    )
    messages: list[str] = []
    runner = make_runner(project, fake_agent, fake_renderer, session_mode="reuse", log=messages.append)
    jobs = [job_for(project, "Resume One"), job_for(project, "Resume Two")]

    summary = run_batch(runner, jobs, parallel=1, log=messages.append)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = fake_agent.calls()
    assert len(calls) == 3
    assert "--resume" not in calls[0]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "sess-A"
    assert "--resume" not in calls[2]["argv"]
    assert any("fresh session" in message for message in messages)
    assert runner.models.astra_exhausted is False


def test_generic_failure_does_not_switch_models(project, fake_agent, fake_renderer):
    fake_agent.steps([{"exit": 1, "stderr": "error: could not parse provider response", "answer": "x"}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Mu")

    result = runner.run_job(job)

    assert result.status == FAILED
    calls = fake_agent.calls()
    assert len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("--provider") + 1] == "openai-codex"
    assert runner.models.astra_exhausted is False


def test_parallel_jobs_use_separate_fresh_sessions(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, f"Parallel {index}") for index in range(3)]

    summary = run_batch(runner, jobs, parallel=3, log=lambda *_: None)

    assert [result.status for result in summary.results] == [OK, OK, OK]
    calls = fake_agent.calls()
    assert len(calls) == 3
    assert all("--resume" not in call["argv"] for call in calls)
    session_dirs = {call["argv"][call["argv"].index("--session-dir") + 1] for call in calls}
    assert session_dirs == {str(project.state / "sessions")}
    assert all(Path(entry).is_relative_to(project.state) for entry in session_dirs)


def test_reuse_mode_resumes_one_explicit_tool_owned_session_sequentially(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": VALID_ANSWER, "session_id": "sess-A"},
            {"answer": VALID_ANSWER, "session_id": "sess-B"},
        ]
    )
    runner = make_runner(project, fake_agent, fake_renderer, session_mode="reuse")
    jobs = [job_for(project, "Reuse One"), job_for(project, "Reuse Two")]

    summary = run_batch(runner, jobs, parallel=1, log=lambda *_: None)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = fake_agent.calls()
    assert "--resume" not in calls[0]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "sess-A"
    assert summary.results[1].resumed is True


def test_none_session_mode_passes_no_session(project, fake_agent, fake_renderer):
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer, session_mode="none")
    job = job_for(project, "NoSession")

    assert runner.run_job(job).status == OK
    argv = fake_agent.calls()[0]["argv"]
    assert "--no-session" in argv and "--session-dir" not in argv


def test_failures_do_not_stop_the_remaining_jobs(project, fake_agent, fake_renderer):
    fake_agent.steps([{"exit": 1, "stderr": "error: unexpected upstream failure"}])
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, "One"), job_for(project, "Two")]

    summary = run_batch(runner, jobs, parallel=1, log=lambda *_: None)

    assert [result.status for result in summary.results] == [FAILED, FAILED]
    assert len(fake_agent.calls()) == 2


def test_english_language_uses_the_english_catalog_and_target(project, fake_agent, fake_renderer):
    answer = selection_answer({EL: [f"{EL}/el1-short"]})
    fake_agent.steps([{"answer": answer, "session_id": "s"}])
    runner = make_runner(project, fake_agent, fake_renderer, language="en")
    job = job_for(project, "English Job", language="en")

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert (target_path(job.directory, "en")).exists()
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["generator_input"]["language"] == "en"
    assert saved["generator_input"]["headline"] == "Software Developer"
    assert saved["generator_input"]["experience"][0]["bullets"][0] == "Mock implementation of an internal API for other teams."
    prompt = fake_agent.calls()[0]["argv"][-1]
    assert "CV language: en (English)" in prompt
