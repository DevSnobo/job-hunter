"""Resume-recovery regression tests with the exact installed diagnostics.

``prime-agent`` 0.9.8 fails an unusable ``--resume`` selector before inference
with one of these messages (captured offline from the installed binary):

    Error: No session found matching '<selector>'.
    Open prime-agent and press left-arrow to browse sessions.

    SessionSelectorError: Ambiguous saved session "<selector>": matches <id> ...

Both must be classified as session problems so the documented "retry once with
a fresh session" recovery runs.  No live provider call happens here.
"""

from __future__ import annotations

from cv_batch.config import build_paths
from cv_batch.prompt import Limits
from cv_batch.model import PrimeAgentAdapter
from cv_batch.runner import FAILED, OK, JobSpec, Runner, run_batch
from cv_batch.sessions import ModelPolicy, SessionPolicy

from conftest import el_entry_id, selection_answer, sp_entry_id

EL = el_entry_id()
SP = sp_entry_id()
VALID_ANSWER = selection_answer({EL: [f"{EL}/el1-short"], SP: [f"{SP}/b1"]})

MISSING_SESSION = (
    "Error: No session found matching 'sess-A'.\n"
    "Open prime-agent and press left-arrow to browse sessions.\n"
)
AMBIGUOUS_SESSION = 'SessionSelectorError: Ambiguous saved session "sess-A": matches 1a2b (My CV), 3c4d\n'


def make_runner(project, fake_agent, fake_renderer, log=None):
    paths = build_paths(**project.kwargs())
    return Runner(
        paths=paths,
        language="de",
        model_policy=ModelPolicy("auto"),
        session_policy=SessionPolicy("reuse", paths.sessions),
        agent=PrimeAgentAdapter(fake_agent.exe),
        generator_exe=fake_renderer.exe,
        limits=Limits(max_bullets_per_entry=5, max_bullets_total=13, max_headline=60),
        posting_chars=6000,
        model_timeout=10.0,
        render_timeout=10.0,
        trimming=True,
        trim_attempts=8,
        content_retries=1,
        allow_replace_invalid=False,
        log=log or (lambda *_: None),
    )


def job_for(project, folder):
    directory = project.add_app(folder)
    return JobSpec(
        folder=folder,
        directory=directory,
        posting=directory / "posting.txt",
        language="de",
        naming=project.naming("de"),
    )


def test_missing_session_wording_triggers_one_fresh_retry(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": VALID_ANSWER, "session_id": "sess-A"},
            {"exit": 1, "stderr": MISSING_SESSION, "answer": "x"},
            {"answer": VALID_ANSWER, "session_id": "sess-B"},
        ]
    )
    messages: list[str] = []
    runner = make_runner(project, fake_agent, fake_renderer, log=messages.append)
    jobs = [job_for(project, "Missing Session One"), job_for(project, "Missing Session Two")]

    summary = run_batch(runner, jobs, parallel=1, log=messages.append)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = fake_agent.calls()
    assert len(calls) == 3
    assert "--resume" not in calls[0]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "sess-A"
    assert "--resume" not in calls[2]["argv"]  # the fresh retry, then re-anchored
    assert all(call["argv"][call["argv"].index("--provider") + 1] == "openai-codex" for call in calls)
    assert runner.models.astra_exhausted is False  # a session problem is not a provider problem
    assert any("fresh session" in message for message in messages)


def test_ambiguous_session_wording_triggers_one_fresh_retry(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": VALID_ANSWER, "session_id": "sess-A"},
            {"exit": 1, "stderr": AMBIGUOUS_SESSION, "answer": "x"},
            {"answer": VALID_ANSWER, "session_id": "sess-C"},
        ]
    )
    messages: list[str] = []
    runner = make_runner(project, fake_agent, fake_renderer, log=messages.append)
    jobs = [job_for(project, "Ambiguous One"), job_for(project, "Ambiguous Two")]

    summary = run_batch(runner, jobs, parallel=1, log=messages.append)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = fake_agent.calls()
    assert len(calls) == 3
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "sess-A"
    assert "--resume" not in calls[2]["argv"]
    assert runner.models.astra_exhausted is False


def test_session_recovery_is_bounded_to_one_fresh_retry(project, fake_agent, fake_renderer):
    fake_agent.steps(
        [
            {"answer": VALID_ANSWER, "session_id": "sess-A"},
            {"exit": 1, "stderr": MISSING_SESSION, "answer": "x"},
            {"exit": 1, "stderr": MISSING_SESSION, "answer": "x"},
        ]
    )
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, "Bounded One"), job_for(project, "Bounded Two")]

    summary = run_batch(runner, jobs, parallel=1, log=lambda *_: None)

    assert [result.status for result in summary.results] == [OK, FAILED]
    assert len(fake_agent.calls()) == 3  # exactly one extra fresh attempt
