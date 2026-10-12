"""Run a CLI agent and read its answer.

The tool needs one thing from a CLI agent: given a system prompt and a user
prompt, it prints the answer - one JSON object - so the tool can read it from
the child's stdout.  Which agent that is, and how it is invoked, is config, not
code: the command line lives in ``cv.toml`` ``[agent] command`` (or
``$CV_AGENT_COMMAND`` / ``--agent-command``).  The command is one argument list;
``{prompt}`` is replaced with the user prompt and is required, ``{system_prompt}``
with the selector instructions when the template carries it (otherwise the
instructions are prepended to the prompt).  Replacement is literal and per
argument: no shell runs and the prompt is never split.

Two answer formats exist, selected with ``cv.toml [agent] format``:

* ``text`` (the default): the answer - one JSON object - is the child's stdout.
  Any CLI agent that prints its answer works.
* ``prime-agent-json``: the child prints ``prime-agent``'s JSON event stream
  (one JSON object per line), and the answer is read from the assistant events,
  together with the session id and the token usage.

The ``prime-agent-json`` contract, verified against the installed 0.9.8 ``--help``
and documentation:

* ``prime-agent -p --mode json -- <prompt>`` streams one JSON object per line.
  The first line is ``{"type":"session","version":3,"id":"<uuid>",...}``.
* A completed assistant message is repeated by the stream: ``message_end``,
  then ``turn_end`` (field ``message``), then ``agent_end`` (field ``messages``).
  Installed 0.9.8 also derives the outcome of a run from the *last assistant
  ``message_end``*, exactly like the ``lastAssistant`` helper of the headless
  implementation.  ``message_end`` is therefore the canonical source for the
  assistant text, the token usage and the final ``stopReason``; the other two
  events are only a fallback when a stream carries no assistant ``message_end``.
  Counting all three events would report the same usage up to three times.
* ``stopReason`` is one of ``stop`` | ``length`` | ``toolUse`` | ``error`` |
  ``aborted``.  In JSON mode a failed or aborted assistant message still leaves
  the process exit code at 0, so the status must be read from the event, not
  from the exit code or from a keyword scan over unrelated prompt text.

Nothing here invents flags, probes for quota or retries paid calls.  The agent
command is the user's own; ``cv.toml.example`` documents a working prime-agent
command and alternatives.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence
from pathlib import Path

#: Placeholders a command template may carry.
PROMPT_PLACEHOLDER = "{prompt}"
SYSTEM_PROMPT_PLACEHOLDER = "{system_prompt}"

#: How the answer is read from the child's output.
AGENT_FORMATS = ("text", "prime-agent-json")


class ModelError(RuntimeError):
    """A model call failed.  ``category`` names the kind of failure."""

    def __init__(self, message: str, *, category: str = "runtime") -> None:
        super().__init__(message)
        self.category = category


#: Recognised failure wording, for a useful one-line error message.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # quota / rate limit / capacity
    ("quota", re.compile(r"quota", re.I)),
    ("quota", re.compile(r"rate[ _-]?limit", re.I)),
    ("quota", re.compile(r"too many requests", re.I)),
    ("quota", re.compile(r"\b429\b")),
    ("quota", re.compile(r"usage limit", re.I)),
    ("quota", re.compile(r"(out of|no|insufficient|exhausted)[^\n]{0,20}(credit|budget|balance|capacity)", re.I)),
    ("quota", re.compile(r"at capacity", re.I)),
    # availability
    ("availability", re.compile(r"service unavailable", re.I)),
    ("availability", re.compile(r"\b50[234]\b")),
    ("availability", re.compile(r"overloaded", re.I)),
    ("availability", re.compile(r"model[^\n]{0,30}not available", re.I)),
    ("availability", re.compile(r"not available for the current (prime )?team", re.I)),
    ("availability", re.compile(r"no models? (match|available)", re.I)),
    ("availability", re.compile(r"unknown provider", re.I)),
    ("availability", re.compile(r"model[^\n]{0,20}not found", re.I)),
    ("availability", re.compile(r"ECONNREFUSED|ENOTFOUND|EAI_AGAIN|getaddrinfo", re.I)),
    # auth
    ("auth", re.compile(r"\b401\b")),
    ("auth", re.compile(r"\b403\b")),
    ("auth", re.compile(r"unauthori[sz]ed", re.I)),
    ("auth", re.compile(r"forbidden", re.I)),
    ("auth", re.compile(r"not logged in", re.I)),
    ("auth", re.compile(r"login required", re.I)),
    ("auth", re.compile(r"authentication", re.I)),
    ("auth", re.compile(r"invalid api key", re.I)),
    ("auth", re.compile(r"no (credentials|api key)", re.I)),
    ("auth", re.compile(r"re-?login", re.I)),
]


def classify_failure(text: str, *, timed_out: bool = False) -> str:
    """Map process output to a failure category, for the error message.

    This only names the problem (``quota``, ``availability``, ``auth``,
    ``runtime``); the tool never retries or switches models on it.
    """
    if timed_out:
        return "timeout"
    for category, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return category
    return "runtime"


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    cost: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.total += other.total
        self.cost += other.cost

    @property
    def empty(self) -> bool:
        return not any((self.input, self.output, self.cache_read, self.cache_write, self.total, self.cost))

    def describe(self) -> str:
        if self.empty:
            return "tokens: not reported by the CLI"
        return (
            f"tokens: in {self.input}, out {self.output}, "
            f"cache read {self.cache_read}, cache write {self.cache_write}, total {self.total}"
            + (f", cost {self.cost:.4f}" if self.cost else "")
        )


@dataclass
class ModelRun:
    """Everything one agent call produced."""

    label: str
    text: str
    session_id: str | None
    usage: Usage
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    event_count: int
    answer_source: str  # "json-events" | "stdout"
    stop_reason: str | None = None  # final assistant stopReason, e.g. "stop"
    command: list[str] = field(default_factory=list)


def _usage_from(message: dict) -> Usage:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return Usage()
    cost = raw.get("cost")
    if isinstance(cost, dict):
        cost_total = float(cost.get("total") or 0.0)
    elif isinstance(cost, (int, float)):
        cost_total = float(cost)
    else:
        cost_total = 0.0

    def number(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return Usage(
        input=number("input"),
        output=number("output"),
        cache_read=number("cacheRead"),
        cache_write=number("cacheWrite"),
        total=number("totalTokens"),
        cost=cost_total,
    )


def _text_from(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


@dataclass
class ParsedStream:
    """What one event stream told us about the run.

    ``text``/``usage``/``stop_reason`` come from the canonical assistant event
    (see :data:`ASSISTANT_EVENT_PRIORITY`); ``error_signals`` collects only
    structured error fields, never assistant or prompt prose.
    """

    text: str = ""
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    events: int = 0
    #: how the answer was read: ``json-events`` (prime-agent) or ``stdout``.
    source: str = "json-events"
    stop_reason: str | None = None
    error_message: str | None = None
    error_signals: list[str] = field(default_factory=list)


#: Completed-assistant events, most authoritative first (installed 0.9.8).
ASSISTANT_EVENT_PRIORITY = ("message_end", "turn_end", "agent_end")

#: Assistant ``stopReason`` values that mean the call did not complete.
FAILED_STOP_REASONS = ("error", "aborted")

#: ``stopReason`` of a run cancelled by the user or by the CLI itself.
ABORTED_STOP_REASON = "aborted"

#: Event fields that carry a machine-readable error string.
_ERROR_FIELDS = ("errorMessage", "finalError", "error", "reason", "detail")


def _failure_signals(event: dict) -> list[str]:
    """Structured error strings of one event (assistant text is never used)."""
    found: list[str] = []

    def walk(value: object, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and key in _ERROR_FIELDS and item.strip():
                    found.append(item.strip()[:300])
                else:
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:32]:
                walk(item, depth + 1)

    walk(event)
    return found


def _message_identity(message: dict) -> tuple[object, ...]:
    """Stable identity of one assistant message, for de-duplicating the stream."""
    for key in ("responseId", "id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return (key, value)
    return (
        "signature",
        json.dumps(message.get("content"), sort_keys=True, ensure_ascii=False, default=str),
        str(message.get("timestamp")),
        json.dumps(message.get("usage"), sort_keys=True, ensure_ascii=False, default=str),
        str(message.get("stopReason")),
    )


def _assistant_sightings(stdout: str) -> tuple[list[tuple[str, dict]], str | None, int, list[str]]:
    """Every assistant message in the stream, with the event type that carried it."""
    sightings: list[tuple[str, dict]] = []
    session_id: str | None = None
    events = 0
    signals: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        if session_id is None:
            candidate = event.get("id") if event.get("type") == "session" else event.get("sessionId")
            if isinstance(candidate, str) and candidate:
                session_id = candidate
        for signal in _failure_signals(event):
            if signal not in signals:
                signals.append(signal)
        event_type = event.get("type")
        if event_type in ASSISTANT_EVENT_PRIORITY:
            if event_type == "agent_end":
                messages = event.get("messages")
                candidates = messages if isinstance(messages, list) else []
            else:
                candidates = [event.get("message")]
            for message in candidates:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    sightings.append((event_type, message))
    return sightings, session_id, events, signals


def _canonical_messages(sightings: list[tuple[str, dict]]) -> list[dict]:
    """Assistant messages of one canonical event type, each counted once.

    ``message_end`` wins; ``turn_end`` and ``agent_end`` are only used when the
    stream carries no assistant ``message_end`` at all.  Within the chosen
    source a repeated message identity is counted once.
    """
    for event_type in ASSISTANT_EVENT_PRIORITY:
        group = [message for kind, message in sightings if kind == event_type]
        if not group:
            continue
        unique: list[dict] = []
        seen: set[tuple[object, ...]] = set()
        for message in group:
            identity = _message_identity(message)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(message)
        return unique
    return []


def parse_events(stdout: str) -> ParsedStream:
    """Parse one JSON event stream into text, session, usage and final status."""
    sightings, session_id, events, signals = _assistant_sightings(stdout)

    canonical = _canonical_messages(sightings)
    answer = ""
    usage = Usage()
    for message in canonical:
        text = _text_from(message)
        if text.strip():
            answer = text
        usage.add(_usage_from(message))

    stop_reason: str | None = None
    error_message: str | None = None
    if canonical:
        last = canonical[-1]
        raw_reason = last.get("stopReason")
        if isinstance(raw_reason, str) and raw_reason:
            stop_reason = raw_reason
        raw_error = last.get("errorMessage")
        if isinstance(raw_error, str) and raw_error.strip():
            error_message = raw_error.strip()
    return ParsedStream(
        text=answer,
        session_id=session_id,
        usage=usage,
        events=events,
        stop_reason=stop_reason,
        error_message=error_message,
        error_signals=signals,
    )


class AgentAdapter:
    """One configured CLI agent (see the module docstring).

    ``command`` is the full argument list from ``cv.toml`` ``[agent] command``;
    ``format`` says how the answer is read from the child's output.
    """

    def __init__(self, command: Sequence[str], *, format: str = "text") -> None:
        parts = tuple(str(part) for part in command)
        if not parts:
            raise ValueError("the agent command must not be empty")
        if not any(PROMPT_PLACEHOLDER in part for part in parts):
            raise ValueError(f"the agent command must contain {PROMPT_PLACEHOLDER}")
        if format not in AGENT_FORMATS:
            raise ValueError(f"unknown agent format {format!r}; choose from {AGENT_FORMATS!r}")
        self.command = parts
        self.format = format

    @property
    def name(self) -> str:
        """Short agent name: the program name of the command."""
        first = self.command[0]
        return os.path.basename(first) or first

    def describe(self) -> str:
        """The full command line, for the plan and the confirm prompt."""
        return " ".join(shlex.quote(part) for part in self.command)

    def label(self) -> str:
        """Short label recorded in the run evidence and the report."""
        return self.name

    def build_command(self, *, prompt: str, system_prompt: str) -> list[str]:
        """Substitute the placeholders of the command template (never a shell)."""
        if any(SYSTEM_PROMPT_PLACEHOLDER in part for part in self.command):
            replacements = {PROMPT_PLACEHOLDER: prompt, SYSTEM_PROMPT_PLACEHOLDER: system_prompt}
        else:
            replacements = {PROMPT_PLACEHOLDER: f"{system_prompt}\n\n{prompt}"}
        argv: list[str] = []
        for part in self.command:
            for placeholder, value in replacements.items():
                part = part.replace(placeholder, value)
            argv.append(part)
        return argv

    def parse(self, stdout: str) -> ParsedStream:
        if self.format == "prime-agent-json":
            return parse_events(stdout)
        return ParsedStream(text=(stdout or "").strip(), source="stdout")

    def failure_signals(self, stdout: str, stderr: str, parsed: ParsedStream) -> str:
        return _failure_signal(stdout, stderr, parsed)


def run_model(
    agent: AgentAdapter,
    *,
    prompt: str,
    system_prompt: str,
    timeout: float = 240.0,
    cwd: Path | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> ModelRun:
    """Run one agent call.  Raises :class:`ModelError` on a failed call.

    ``on_start``/``on_end`` let the caller track the live child process so a
    cancelled batch can terminate it cleanly.
    """
    label = agent.label()
    argv = agent.build_command(prompt=prompt, system_prompt=system_prompt)
    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    if not sys_stdin_available():
        env.setdefault("CI", "1")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ModelError(f"agent executable not found: {argv[0]}", category="runtime") from exc
    except OSError as exc:
        raise ModelError(f"cannot start {argv[0]}: {exc}", category="runtime") from exc

    if on_start is not None:
        on_start(process)
    try:
        return _finish(
            process,
            agent=agent,
            label=label,
            timeout=timeout,
            started=started,
            command=argv,
        )
    finally:
        if on_end is not None:
            on_end(process)


def _finish(
    process: subprocess.Popen,
    *,
    agent: AgentAdapter,
    label: str,
    timeout: float,
    started: float,
    command: list[str],
) -> ModelRun:
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        raise ModelError(
            f"{label} timed out after {timeout:.0f}s",
            category="timeout",
        ) from None

    duration = time.monotonic() - started
    parsed = agent.parse(stdout or "")
    answer = parsed.text
    source = parsed.source
    if not answer.strip():
        stripped = (stdout or "").strip()
        if stripped and parsed.events == 0:
            answer, source = stripped, "raw-stdout"

    # Only structured error fields, stderr, and (for a non-event stream) the raw
    # stdout are classified.  Assistant prose and the echoed prompt never are.
    signal_text = agent.failure_signals(stdout or "", stderr or "", parsed)

    if process.returncode != 0:
        category = classify_failure(signal_text, timed_out=timed_out)
        detail = (
            _first_meaningful_line(parsed.error_message or "")
            or _first_meaningful_line(stderr)
            or _first_meaningful_line(stdout)
            or "no output"
        )
        raise ModelError(
            f"{label} failed (exit {process.returncode}, {category}): {detail}",
            category=category,
        )
    if parsed.stop_reason in FAILED_STOP_REASONS:
        if parsed.stop_reason == ABORTED_STOP_REASON:
            detail = _first_meaningful_line(parsed.error_message or "") or "the agent aborted the run"
            raise ModelError(
                f"{label} was aborted before completing ({detail})",
                category="aborted",
            )
        category = classify_failure(signal_text)
        detail = (
            _first_meaningful_line(parsed.error_message or "")
            or _first_meaningful_line(stderr)
            or "the agent reported an error"
        )
        raise ModelError(
            f"{label} stopped with an error ({category}): {detail}",
            category=category,
        )
    if not answer.strip():
        category = classify_failure(signal_text)
        if category == "runtime":
            category = "invalid_output"
        detail = _first_meaningful_line(stderr) or "the agent produced no answer"
        raise ModelError(f"{label} produced no answer: {detail}", category=category)

    return ModelRun(
        label=label,
        text=answer,
        session_id=parsed.session_id,
        usage=parsed.usage,
        exit_code=process.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        duration=duration,
        event_count=parsed.events,
        answer_source=source,
        stop_reason=parsed.stop_reason,
        command=command,
    )


def _failure_signal(stdout: str, stderr: str, parsed: ParsedStream) -> str:
    """Text used for failure classification.

    Structured event error fields come first, then stderr.  Raw stdout is added
    only when the stream carried no JSON event at all, because a JSON stream
    echoes the user prompt and the assistant prose, which must never decide the
    failure category.
    """
    parts = list(parsed.error_signals)
    if stderr.strip():
        parts.append(stderr)
    if parsed.events == 0 and stdout.strip():
        parts.append(stdout)
    return "\n".join(parts)


def sys_stdin_available() -> bool:
    try:
        return os.isatty(0)
    except OSError:  # pragma: no cover - defensive
        return False


def _terminate(process: subprocess.Popen) -> None:
    """Kill the whole process group of a child (it is its own session)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return ""


__all__ = [
    "ABORTED_STOP_REASON",
    "AGENT_FORMATS",
    "ASSISTANT_EVENT_PRIORITY",
    "AgentAdapter",
    "FAILED_STOP_REASONS",
    "ModelError",
    "ModelRun",
    "PROMPT_PLACEHOLDER",
    "ParsedStream",
    "SYSTEM_PROMPT_PLACEHOLDER",
    "Usage",
    "classify_failure",
    "parse_events",
    "run_model",
]
