"""Provider event-stream fixtures for the installed prime-agent JSON contract.

The streams here are literal fixtures shaped after the installed 0.9.8
documentation (``docs/json.md``, ``docs/session-format.md``).  No live provider
call happens, and none of the token numbers below is a live measurement: they
are fixed literals used to prove that one assistant message is counted once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cv_batch.model import (
    ABORTED_STOP_REASON,
    AgentAdapter,
    ModelError,
    parse_events,
    run_model,
)

SYSTEM = "You are a selector."
FAKES = Path(__file__).resolve().parent / "fakes"

USAGE_A = {
    "input": 100,
    "output": 20,
    "cacheRead": 5,
    "cacheWrite": 1,
    "totalTokens": 126,
    "cost": {"total": 0.0012},
}
USAGE_B = {
    "input": 200,
    "output": 30,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 230,
    "cost": {"total": 0.002},
}


def assistant(
    text: str = '{"headline": "x", "experience": []}',
    *,
    usage: dict | None = None,
    stop_reason: str = "stop",
    error_message: str | None = None,
    timestamp: int = 1,
    response_id: str | None = None,
) -> dict:
    """One AssistantMessage exactly as the installed release serialises it."""
    message: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "responses",
        "provider": "openai-codex",
        "model": "gpt-6-astra",
        "usage": dict(USAGE_A if usage is None else usage),
        "stopReason": stop_reason,
        "timestamp": timestamp,
    }
    if response_id is not None:
        message["responseId"] = response_id
    if error_message is not None:
        message["errorMessage"] = error_message
    return message


def user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": 0}


def lifecycle(messages: list[dict], *, session_id: str = "sess-1") -> list[dict]:
    """The documented normal lifecycle: message_end, turn_end, agent_end."""
    events: list[dict] = [
        {"type": "session", "version": 3, "id": session_id, "timestamp": 0, "cwd": "/tmp"}
    ]
    for message in messages:
        events.append({"type": "agent_start"})
        events.append({"type": "turn_start"})
        events.append({"type": "message_start", "message": {**message, "content": []}})
        events.append({"type": "message_update", "message": message, "assistantMessageEvent": {"type": "text_delta"}})
        events.append({"type": "message_end", "message": message})
        events.append({"type": "turn_end", "message": message, "toolResults": []})
    events.append({"type": "agent_end", "messages": list(messages)})
    return events


def stream(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


# --------------------------------------------------------------------- usage


def test_full_normal_lifecycle_counts_the_single_message_once():
    events = lifecycle([assistant("answer-body")])
    parsed = parse_events(stream(events))

    assert parsed.text == "answer-body"
    assert parsed.session_id == "sess-1"
    assert parsed.events == len(events)
    assert parsed.stop_reason == "stop"
    assert (parsed.usage.input, parsed.usage.output) == (100, 20)
    assert (parsed.usage.cache_read, parsed.usage.cache_write) == (5, 1)
    assert parsed.usage.total == 126
    assert parsed.usage.cost == pytest.approx(0.0012)


def test_two_distinct_messages_are_counted_once_each():
    first = assistant("first", timestamp=1)
    second = assistant("second", usage=USAGE_B, timestamp=2)
    parsed = parse_events(stream(lifecycle([first, second])))

    assert parsed.text == "second"
    assert parsed.usage.total == 126 + 230
    assert parsed.usage.input == 300
    assert parsed.usage.cost == pytest.approx(0.0032)


def test_stream_without_message_end_falls_back_to_turn_end_once():
    message = assistant("only-turn-end")
    events = [
        {"type": "session", "version": 3, "id": "sess-2", "timestamp": 0},
        {"type": "turn_start"},
        {"type": "turn_end", "message": message, "toolResults": []},
        {"type": "agent_end", "messages": [message]},
    ]
    parsed = parse_events(stream(events))

    assert parsed.text == "only-turn-end"
    assert parsed.usage.total == 126
    assert parsed.session_id == "sess-2"


def test_stream_with_only_agent_end_uses_each_message_once():
    first = assistant("first", timestamp=1)
    second = assistant("second", usage=USAGE_B, timestamp=2)
    events = [
        {"type": "session", "version": 3, "id": "sess-3", "timestamp": 0},
        {"type": "agent_end", "messages": [first, second]},
    ]
    parsed = parse_events(stream(events))

    assert parsed.text == "second"
    assert parsed.usage.total == 126 + 230


def test_repeated_identical_message_end_is_counted_once():
    message = assistant("same")
    events = [
        {"type": "session", "version": 3, "id": "sess-4", "timestamp": 0},
        {"type": "message_end", "message": message},
        {"type": "message_end", "message": json.loads(json.dumps(message))},
    ]
    parsed = parse_events(stream(events))

    assert parsed.usage.total == 126


def test_response_id_is_used_as_message_identity_when_present():
    first = assistant("same", usage=USAGE_A, response_id="resp-1")
    second = assistant("same", usage=USAGE_B, response_id="resp-1", timestamp=99)
    events = [
        {"type": "session", "version": 3, "id": "sess-5", "timestamp": 0},
        {"type": "message_end", "message": first},
        {"type": "message_end", "message": second},
    ]
    parsed = parse_events(stream(events))

    # same responseId -> same message -> the first sighting wins
    assert parsed.usage.total == 126


# ------------------------------------------------------------ final status


def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **scenario) -> str:
    directory = tmp_path / "provider-scenario"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"exit": 0, **scenario}
    (directory / "scenario.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("FAKE_JSON_AGENT_SCENARIO", str(directory / "scenario.json"))
    return str(FAKES / "fake_json_agent.py")


def run_cli(exe: str, **kwargs):
    adapter = AgentAdapter([exe, "{prompt}"], format="prime-agent-json")
    return run_model(adapter, prompt="PROMPT", system_prompt=SYSTEM, timeout=10, **kwargs)


def test_json_error_status_fails_even_with_exit_zero_and_content(tmp_path, monkeypatch):
    message = assistant(
        '{"headline": "looks fine"}',
        usage=USAGE_A,
        stop_reason="error",
        error_message="429 Too Many Requests: quota exceeded for this team",
    )
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]), exit=0)

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "quota"
    assert "quota" in str(excinfo.value)


def test_json_error_status_classifies_authentication(tmp_path, monkeypatch):
    message = assistant(
        '{"headline": "x"}',
        stop_reason="error",
        error_message="401 Unauthorized: not logged in",
    )
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]))

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "auth"


def test_json_error_status_with_unknown_text_stays_runtime(tmp_path, monkeypatch):
    message = assistant(
        '{"headline": "x"}',
        stop_reason="error",
        error_message="the provider returned an unexpected body",
    )
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]))

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "runtime"


def test_json_error_status_with_empty_content_still_fails(tmp_path, monkeypatch):
    message = assistant("", stop_reason="error", error_message="service unavailable (503)")
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]))

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "availability"


def test_aborted_status_never_succeeds_and_never_switches(tmp_path, monkeypatch):
    message = assistant(
        '{"headline": "looks fine"}',
        stop_reason=ABORTED_STOP_REASON,
        error_message="Request was aborted",
    )
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]))

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "aborted"
    assert "aborted" in str(excinfo.value)


def test_success_reports_the_stop_reason(tmp_path, monkeypatch):
    message = assistant('{"headline": "ok"}', usage=USAGE_B)
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]))

    result = run_cli(exe)

    assert result.text == '{"headline": "ok"}'
    assert result.stop_reason == "stop"
    assert result.usage.total == 230
    assert result.answer_source == "json-events"


def test_prompt_prose_never_decides_the_failure_category(tmp_path, monkeypatch):
    """A prompt that talks about quota must not turn a broken provider into a switch."""
    message = assistant(
        '{"headline": "x"}',
        stop_reason="error",
        error_message="interface error",
    )
    events = [
        {"type": "session", "version": 3, "id": "sess-9", "timestamp": 0},
        {"type": "message_end", "message": user_message("quota exceeded, 429, 401 unauthorized")},
        {"type": "message_end", "message": message},
        {"type": "agent_end", "messages": [message]},
    ]
    exe = fake_cli(tmp_path, monkeypatch, events=events)

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "runtime"


def test_successful_run_is_not_failed_by_earlier_error_text(tmp_path, monkeypatch):
    """A recovered provider error inside the stream does not fail a finished run."""
    message = assistant('{"headline": "ok"}', usage=USAGE_A)
    events = lifecycle([message])
    events.insert(
        2,
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 1000,
            "errorMessage": "429 rate limit reached",
        },
    )
    exe = fake_cli(tmp_path, monkeypatch, events=events)

    result = run_cli(exe)

    assert result.text == '{"headline": "ok"}'
    assert result.stop_reason == "stop"


def test_nonzero_exit_still_classifies_from_stderr(tmp_path, monkeypatch):
    exe = fake_cli(tmp_path, monkeypatch, stdout_text="", stderr="error: quota exceeded for openai-codex", exit=1)

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "quota"


def test_nonzero_exit_with_json_error_message_classifies_structurally(tmp_path, monkeypatch):
    message = assistant("", stop_reason="error", error_message="429 rate limit reached")
    exe = fake_cli(tmp_path, monkeypatch, events=lifecycle([message]), stderr="", exit=1)

    with pytest.raises(ModelError) as excinfo:
        run_cli(exe)

    assert excinfo.value.category == "quota"

