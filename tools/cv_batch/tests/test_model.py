import json
from pathlib import Path

import pytest

from cv_batch.model import (
    ASTRA,
    DEEPSEEK,
    ModelError,
    PrimeAgentAdapter,
    Usage,
    build_command,
    classify_failure,
    parse_stream,
    run_model,
)

SYSTEM = "You are a selector."


def test_build_command_disables_every_context_resource(tmp_path):
    argv = build_command(
        exe="prime-agent",
        spec=ASTRA,
        prompt="PROMPT",
        session_dir=tmp_path,
        resume_id=None,
        system_prompt=SYSTEM,
    )
    assert argv[0] == "prime-agent"
    assert argv[1:3] == ["-p", "--mode"]
    assert argv[3] == "json"
    assert argv[argv.index("--provider") + 1] == "openai-codex"
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    for flag in (
        "--no-tools",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
    ):
        assert flag in argv
    assert argv[argv.index("--session-dir") + 1] == str(tmp_path)
    assert argv[argv.index("--system-prompt") + 1] == SYSTEM
    assert argv[-2] == "--" and argv[-1] == "PROMPT"
    assert "--resume" not in argv
    assert "--no-session" not in argv
    assert "-c" not in argv and "--continue" not in argv  # never touch the user's sessions


def test_build_command_session_variants(tmp_path):
    resumed = build_command(
        exe="prime-agent",
        spec=DEEPSEEK,
        prompt="p",
        session_dir=tmp_path,
        resume_id="abc123",
        system_prompt=SYSTEM,
    )
    assert resumed[resumed.index("--resume") + 1] == "abc123"
    assert resumed[resumed.index("--model") + 1] == "deepseek-v4-flash"
    assert resumed[resumed.index("--provider") + 1] == "deepseek"

    plain = build_command(
        exe="prime-agent",
        spec=DEEPSEEK,
        prompt="p",
        session_dir=tmp_path,
        resume_id=None,
        system_prompt=SYSTEM,
        no_session=True,
    )
    assert "--no-session" in plain
    assert "--session-dir" not in plain
    assert "deepseek-v4-pro" not in " ".join(plain)


def test_parse_stream_collects_answer_session_and_usage():
    lines = [
        json.dumps({"type": "session", "version": 3, "id": "sess-1"}),
        json.dumps({"type": "agent_start"}),
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"a": 1}'}],
                    "usage": {
                        "input": 10,
                        "output": 4,
                        "cacheRead": 2,
                        "cacheWrite": 1,
                        "totalTokens": 17,
                        "cost": {"total": 0.5},
                    },
                },
            }
        ),
        "not json at all",
        json.dumps({"type": "agent_end", "messages": []}),
    ]
    text, session, usage, events = parse_stream("\n".join(lines))
    assert text == '{"a": 1}'
    assert session == "sess-1"
    assert (usage.input, usage.output, usage.cache_read, usage.total) == (10, 4, 2, 17)
    assert usage.cost == 0.5
    assert events == 4


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
        ("error: unknown session id abc", "session"),
        ("--resume failed", "session"),
        # exact diagnostics of the installed 0.9.8 release, captured offline
        (
            "Error: No session found matching 'definitely-missing-session'.\n"
            "Open prime-agent and press left-arrow to browse sessions.",
            "session",
        ),
        ("SessionSelectorNotFoundError: No session found matching 'abc'", "session"),
        (
            'SessionSelectorError: Ambiguous saved session "abc": matches 1a2b (My CV), 3c4d',
            "session",
        ),
        ("Ambiguous saved session 'x': matches 2 saved sessions", "session"),
        ("something odd happened", "runtime"),
        ("", "runtime"),
    ],
)
def test_classify_failure(text, expected):
    assert classify_failure(text) == expected


def test_classify_failure_timeout():
    assert classify_failure("anything", timed_out=True) == "timeout"


def run(agent, *, spec=ASTRA, prompt="PROMPT", timeout=10, resume_id=None, no_session=False, tmp_path=None):
    return run_model(
        PrimeAgentAdapter(agent.exe),
        spec=spec,
        prompt=prompt,
        system_prompt=SYSTEM,
        session_dir=tmp_path,
        resume_id=resume_id,
        no_session=no_session,
        timeout=timeout,
    )


def test_run_model_success_and_argv(fake_agent, tmp_path):
    fake_agent.steps([{"answer": '{"headline": "x", "experience": []}', "session_id": "s1"}])
    result = run(fake_agent, tmp_path=tmp_path)
    assert result.text == '{"headline": "x", "experience": []}'
    assert result.session_id == "s1"
    assert result.answer_source == "json-events"
    assert result.usage.total == 126
    call = fake_agent.calls()[0]
    assert call["argv"][-1] == "PROMPT"
    assert "--no-tools" in call["argv"]


def test_run_model_raw_stdout_fallback(fake_agent, tmp_path):
    fake_agent.steps([{"answer": "plain answer", "raw": True}])
    result = run(fake_agent, tmp_path=tmp_path)
    assert result.text == "plain answer"
    assert result.answer_source == "raw-stdout"


def test_run_model_quota_failure(fake_agent, tmp_path):
    fake_agent.steps([{"exit": 1, "stderr": "error: quota exceeded", "answer": "ignored"}])
    with pytest.raises(ModelError) as excinfo:
        run(fake_agent, tmp_path=tmp_path)
    assert excinfo.value.category == "quota"


def test_run_model_plain_failure_is_runtime(fake_agent, tmp_path):
    fake_agent.steps([{"exit": 1, "stderr": "error: invalid JSON in model output"}])
    with pytest.raises(ModelError) as excinfo:
        run(fake_agent, tmp_path=tmp_path)
    assert excinfo.value.category == "runtime"


def test_run_model_empty_output_is_invalid_output(fake_agent, tmp_path):
    fake_agent.steps([{"answer": "", "exit": 0}])
    with pytest.raises(ModelError) as excinfo:
        run(fake_agent, tmp_path=tmp_path)
    assert excinfo.value.category == "invalid_output"


def test_run_model_timeout_kills_the_child(fake_agent, tmp_path):
    fake_agent.steps([{"sleep": 5, "answer": "too late"}])
    with pytest.raises(ModelError) as excinfo:
        run(fake_agent, timeout=0.5, tmp_path=tmp_path)
    assert excinfo.value.category == "timeout"


def test_run_model_resume_arguments_are_forwarded(fake_agent, tmp_path):
    fake_agent.steps([{"answer": '{"experience": []}'}])
    run(fake_agent, resume_id="sess-9", tmp_path=tmp_path)
    argv = fake_agent.calls()[0]["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-9"
    assert argv[argv.index("--session-dir") + 1] == str(tmp_path)


def test_successful_answer_text_is_never_classified_as_a_failure(fake_agent, tmp_path):
    """F4b/F5: provider keywords inside the answer must not fail a good run."""
    fake_agent.steps([{"answer": "quota exceeded; 401 Unauthorized; unknown session id"}])

    result = run(fake_agent, tmp_path=tmp_path)

    assert result.text.startswith("quota exceeded")
    # the shared fake may or may not emit a stopReason; neither is a failure
    assert result.stop_reason in (None, "stop")


def test_usage_describe_reports_missing_counters():
    assert Usage().describe() == "tokens: not reported by the CLI"
