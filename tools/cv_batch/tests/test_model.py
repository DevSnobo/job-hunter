"""The generic agent adapter: command template, answer formats and failures.

The prime-agent JSON event stream is covered separately in
``test_model_events.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cv_batch.model import (
    AGENT_FORMATS,
    AgentAdapter,
    ModelError,
    Usage,
    classify_failure,
    run_model,
)

FAKES = Path(__file__).resolve().parent / "fakes"
SYSTEM = "You are a selector."


def test_template_substitutes_prompt_and_system_prompt():
    adapter = AgentAdapter(["my-agent", "--system", "{system_prompt}", "{prompt}"])
    assert adapter.build_command(prompt="TASK", system_prompt="SYS") == [
        "my-agent",
        "--system",
        "SYS",
        "TASK",
    ]


def test_template_without_a_system_slot_prepends_the_instructions():
    adapter = AgentAdapter(["my-agent", "{prompt}"])
    assert adapter.build_command(prompt="TASK", system_prompt="SYS") == ["my-agent", "SYS\n\nTASK"]


def test_the_command_must_carry_a_prompt_placeholder():
    with pytest.raises(ValueError):
        AgentAdapter(["my-agent", "--print"])
    with pytest.raises(ValueError):
        AgentAdapter([])


def test_the_format_must_be_known():
    with pytest.raises(ValueError):
        AgentAdapter(["my-agent", "{prompt}"], format="rpc")


def test_name_and_description_name_the_program():
    adapter = AgentAdapter(["/usr/local/bin/my-agent", "--go", "{prompt}"])
    assert adapter.name == "my-agent"
    assert adapter.label() == "my-agent"
    assert adapter.describe() == "/usr/local/bin/my-agent --go '{prompt}'"
    assert adapter.format == "text"
    assert AGENT_FORMATS == ("text", "prime-agent-json")


def test_the_text_format_takes_stdout_and_the_json_format_parses_events():
    plain = AgentAdapter(["my-agent", "{prompt}"])
    parsed = plain.parse("  the answer  \n")
    assert parsed.text == "the answer" and parsed.source == "stdout"
    events = AgentAdapter(["my-agent", "{prompt}"], format="prime-agent-json")
    line = json.dumps(
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}
    )
    assert events.parse(line).text == "x"


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


def test_run_model_plain_failure_is_runtime(tmp_path):
    adapter = command_adapter(tmp_path, answer="", exit_code=1, stderr="error: invalid JSON in model output")
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=10)
    assert excinfo.value.category == "runtime"


def test_run_model_empty_output_is_invalid_output(tmp_path):
    adapter = command_adapter(tmp_path, answer="", exit_code=0)
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=10)
    assert excinfo.value.category == "invalid_output"


def test_run_model_timeout_kills_the_child(tmp_path):
    adapter = command_adapter(tmp_path, answer="too late", sleep=5)
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=0.5)
    assert excinfo.value.category == "timeout"


def test_a_missing_executable_is_a_clean_error(tmp_path):
    adapter = AgentAdapter(["/nonexistent/agent", "{prompt}"])
    with pytest.raises(ModelError) as excinfo:
        run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=10)
    assert "not found" in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Error: quota exceeded for this team", "quota"),
        ("429 Too Many Requests", "quota"),
        ("rate limit reached", "quota"),
        ("you are out of credits", "quota"),
        ("401 Unauthorized", "auth"),
        ("not logged in, run /login", "auth"),
        ("Model is not available for the current Prime team.", "availability"),
        ("503 service unavailable", "availability"),
        ("invalid JSON in model output", "runtime"),
        ("something odd happened", "runtime"),
        ("", "runtime"),
    ],
)
def test_classify_failure(text, expected):
    assert classify_failure(text) == expected


def test_classify_failure_timeout():
    assert classify_failure("anything", timed_out=True) == "timeout"


def test_successful_answer_text_is_never_treated_as_a_failure(tmp_path):
    """Provider keywords inside the answer must not fail a good run."""
    adapter = command_adapter(tmp_path, answer="quota exceeded; 401 Unauthorized")
    run = run_model(adapter, prompt="P", system_prompt=SYSTEM, timeout=10)
    assert run.text.startswith("quota exceeded")


def test_usage_describe_reports_missing_counters():
    assert Usage().describe() == "tokens: not reported by the CLI"
