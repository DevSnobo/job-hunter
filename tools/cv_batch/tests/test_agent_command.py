"""The command agent: the seam that lets any CLI agent be plugged in.

The built-in adapter (``prime-agent``) is covered by the other model tests; here
only the generic ``CommandAgentAdapter`` and its wiring through ``run_model`` and
the runner are exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cv_batch.config import build_paths
from cv_batch.model import CommandAgentAdapter, ModelError, run_model
from cv_batch.prompt import Limits
from cv_batch.runner import OK, JobSpec, Runner
from cv_batch.sessions import ModelPolicy, SessionPolicy

from conftest import el_entry_id, selection_answer

FAKES = Path(__file__).resolve().parent / "fakes"
SYSTEM = "You are a selector."


def test_template_substitutes_prompt_and_system_prompt():
    adapter = CommandAgentAdapter(["my-agent", "--system", "{system_prompt}", "{prompt}"])
    assert adapter.build_command(prompt="TASK", system_prompt="SYS") == [
        "my-agent",
        "--system",
        "SYS",
        "TASK",
    ]


def test_template_without_a_system_slot_prepends_the_instructions():
    adapter = CommandAgentAdapter(["my-agent", "{prompt}"])
    assert adapter.build_command(prompt="TASK", system_prompt="SYS") == ["my-agent", "SYS\n\nTASK"]


def test_template_requires_a_prompt_placeholder():
    with pytest.raises(ValueError):
        CommandAgentAdapter(["my-agent", "--print"])
    with pytest.raises(ValueError):
        CommandAgentAdapter([])


def test_label_and_description_name_the_program():
    adapter = CommandAgentAdapter(["/usr/local/bin/my-agent", "--go", "{prompt}"])
    assert adapter.label(None) == "my-agent"
    assert adapter.describe() == "/usr/local/bin/my-agent --go '{prompt}'"


def command_adapter(tmp_path: Path, *, answer: str, exit_code: int = 0, stderr: str = "", sleep: float = 0.0):
    """A command adapter backed by the fake agent, plus its scenario path."""
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"answer": answer, "exit": exit_code, "stderr": stderr, "sleep": sleep}),
        encoding="utf-8",
    )
    adapter = CommandAgentAdapter(
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
    return adapter


def calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_model_reads_the_answer_from_stdout(tmp_path):
    adapter = command_adapter(tmp_path, answer='{"experience": []}')
    run = run_model(adapter, prompt="PROMPT", system_prompt=SYSTEM, timeout=10)
    assert run.text == '{"experience": []}'
    assert run.answer_source == "stdout"
    assert run.agent == "command"
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


def test_runner_runs_a_job_with_a_command_agent(tmp_path, project, fake_renderer):
    entry = el_entry_id()
    answer = selection_answer({entry: [f"{entry}/el1-short"]})
    adapter = command_adapter(tmp_path, answer=answer)

    paths = build_paths(**project.kwargs())
    runner = Runner(
        paths=paths,
        language="de",
        model_policy=ModelPolicy("auto"),
        session_policy=SessionPolicy("fresh", paths.sessions),
        agent=adapter,
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
    directory = project.add_app("Vertex GmbH")
    job = JobSpec(
        folder="Vertex GmbH",
        directory=directory,
        posting=directory / "posting.txt",
        language="de",
        naming=project.naming("de"),
    )

    result = runner.run_job(job)

    assert result.status == OK, result.message
    assert result.pdf is not None and result.pdf.read_bytes().startswith(b"%PDF-")
    evidence = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert evidence["model"]["agent"] == "command"
    assert evidence["model"]["selector"] == Path(sys.executable).name
    # The command agent has no sessions: the directory stays empty.
    assert list(paths.sessions.iterdir()) == []
