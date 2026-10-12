"""The configured command agent, wired through ``run_model`` and the runner.

The command template is the only way the tool learns how to invoke an agent -
nothing is built in - and ``format`` says how the answer is read.  The
prime-agent JSON event stream is covered by ``test_model_events.py``; here the
runner is exercised with both formats.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cv_batch.config import build_paths
from cv_batch.model import AgentAdapter, ModelError, run_model
from cv_batch.prompt import Limits
from cv_batch.runner import OK, JobSpec, Runner

from conftest import el_entry_id, prime_agent_adapter, selection_answer

FAKES = Path(__file__).resolve().parent / "fakes"
SYSTEM = "You are a selector."


def command_adapter(tmp_path: Path, *, answer: str, exit_code: int = 0, stderr: str = "", sleep: float = 0.0):
    """A text-format adapter backed by the fake command agent."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"answer": answer, "exit": exit_code, "stderr": stderr, "sleep": sleep}),
        encoding="utf-8",
    )
    return AgentAdapter(
        [
            sys.executable,
            str(FAKES / "fake_command_agent.py"),
            "--scenario",
            str(scenario),
            "--system-prompt",
            "{system_prompt}",
            "{prompt}",
        ]
    )


def calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_runner(project, fake_agent, fake_renderer, agent, *, language="de", **overrides):
    paths = build_paths(**project.kwargs())
    kwargs = dict(
        paths=paths,
        language=language,
        agent=agent,
        generator_exe=fake_renderer.exe,
        layout="classic",
        limits=Limits(max_bullets_per_entry=5, max_bullets_total=13, max_headline=60),
        posting_chars=6000,
        model_timeout=10.0,
        render_timeout=10.0,
        trimming=True,
        trim_attempts=8,
        content_retries=1,
        allow_replace_invalid=False,
        allow_overwrite=False,
        log=lambda *_: None,
    )
    kwargs.update(overrides)
    return Runner(**kwargs)


def job(project, folder, language="de"):
    directory = project.add_app(folder)
    return JobSpec(
        folder=folder,
        directory=directory,
        posting=directory / "posting.txt",
        language=language,
        naming=project.naming(language),
    )


def test_run_model_reads_the_answer_from_stdout(tmp_path):
    adapter = command_adapter(tmp_path, answer='{"experience": []}')
    run = run_model(adapter, prompt="PROMPT", system_prompt=SYSTEM, timeout=10)
    assert run.text == '{"experience": []}'
    assert run.answer_source == "stdout"
    assert run.label == Path(sys.executable).name
    assert run.session_id is None
    assert run.usage.empty
    assert calls(tmp_path)[0]["prompt"] == "PROMPT"
    assert calls(tmp_path)[0]["system_prompt"] == SYSTEM


def test_run_model_classifies_a_failed_command(tmp_path):
    adapter = command_adapter(tmp_path, answer="", exit_code=1, stderr="error: quota exceeded")
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=10)
    assert excinfo.value.category == "quota"


def test_run_model_times_out_a_slow_command(tmp_path):
    adapter = command_adapter(tmp_path, answer="too late", sleep=5)
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=0.5)
    assert excinfo.value.category == "timeout"


def test_runner_runs_a_job_with_a_text_command_agent(tmp_path, project, fake_renderer):
    entry = el_entry_id()
    answer = selection_answer({entry: [f"{entry}/el1-short"]})
    adapter = command_adapter(tmp_path, answer=answer)
    runner = run_runner(project, None, fake_renderer, adapter)

    result = runner.run_job(job(project, "Vertex GmbH"))

    assert result.status == OK, result.message
    assert result.pdf is not None and result.pdf.read_bytes().startswith(b"%PDF-")
    evidence = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert evidence["agent"]["label"] == Path(sys.executable).name
    assert evidence["agent"]["command"][0] == sys.executable
    assert evidence["agent"]["session_id"] is None
    assert evidence["agent"]["answer_source"] == "stdout"


def test_runner_records_the_session_and_usage_for_the_json_format(project, fake_agent, fake_renderer):
    entry = el_entry_id()
    answer = selection_answer({entry: [f"{entry}/el1-short"]})
    fake_agent.steps([{"answer": answer, "session_id": "sess-42"}])
    runner = run_runner(project, fake_agent, fake_renderer, prime_agent_adapter(fake_agent.exe))

    result = runner.run_job(job(project, "Json Format Job"))

    assert result.status == OK, result.message
    assert result.session_id == "sess-42"
    evidence = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert evidence["agent"]["answer_source"] == "json-events"
    assert evidence["agent"]["session_id"] == "sess-42"
    assert evidence["agent"]["usage"]["total"] == 126


def test_runner_refuses_a_job_without_a_valid_answer(project, fake_renderer, tmp_path):
    entry = el_entry_id()
    adapter = command_adapter(tmp_path, answer="not json at all")
    runner = run_runner(project, None, fake_renderer, adapter)

    result = runner.run_job(job(project, "Bad Answer"))

    assert result.status == "failed"
    assert fake_renderer.calls() == []
