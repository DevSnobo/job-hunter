"""F3 regression: simultaneous Astra failures in a parallel batch.

Two workers run at the same time.  Both call Astra and both fail with a quota
error; the second failure arrives after the first worker already switched the
batch to DeepSeek Flash.  Each worker must still get its own one bounded retry
on Flash.  Before the fix the second worker reported its job as failed.

The fake model is a temporary subprocess whose behaviour depends on the selected
model (Astra fails, Flash succeeds); no live provider call happens.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from cv_batch.config import build_paths
from cv_batch.prompt import Limits
from cv_batch.model import PrimeAgentAdapter
from cv_batch.runner import FAILED, OK, JobSpec, Runner, run_batch
from cv_batch.sessions import ModelPolicy, SessionPolicy

from conftest import el_entry_id, selection_answer, sp_entry_id

EL = el_entry_id()
SP = sp_entry_id()
VALID_ANSWER = selection_answer({EL: [f"{EL}/el1-short"], SP: [f"{SP}/b1"]})
FAKES = Path(__file__).resolve().parent / "fakes"


@dataclass
class ProviderAgent:
    exe: str
    directory: Path

    def calls(self) -> list[dict]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def provider_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderAgent:
    """Astra always fails with a quota error, DeepSeek Flash always succeeds."""
    directory = tmp_path / "provider-agent"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_PROVIDER_DIR", str(directory))
    monkeypatch.setenv("FAKE_PROVIDER_STDERR", "Error: quota exceeded for openai-codex\n")
    monkeypatch.setenv("FAKE_PROVIDER_ANSWER", VALID_ANSWER)
    return ProviderAgent(str(FAKES / "fake_provider_agent.py"), directory)


def make_runner(project, agent: ProviderAgent, renderer, log=None):
    paths = build_paths(**project.kwargs())
    return Runner(
        paths=paths,
        language="de",
        model_policy=ModelPolicy("auto"),
        session_policy=SessionPolicy("fresh", paths.sessions),
        agent=PrimeAgentAdapter(agent.exe),
        generator_exe=renderer.exe,
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


def _providers(calls):
    return [call["argv"][call["argv"].index("--provider") + 1] for call in calls]


def test_two_parallel_astra_failures_both_retry_onto_flash(project, provider_agent, fake_renderer):
    messages: list[str] = []
    runner = make_runner(project, provider_agent, fake_renderer, log=messages.append)
    jobs = [job_for(project, "Race One"), job_for(project, "Race Two")]

    summary = run_batch(runner, jobs, parallel=2, log=messages.append)

    assert [result.status for result in summary.results] == [OK, OK]
    calls = provider_agent.calls()
    assert len(calls) == 4
    assert Counter(_providers(calls)) == Counter({"openai-codex": 2, "deepseek": 2})
    assert all("deepseek-v4-pro" not in " ".join(call["argv"]) for call in calls)
    assert runner.models.astra_exhausted is True
    assert runner.models.switch_reason == "quota"
    assert all(result.model == "deepseek/deepseek-v4-flash" for result in summary.results)


def test_control_the_pre_fix_policy_would_fail_one_of_the_two_jobs(
    project, provider_agent, fake_renderer, monkeypatch
):
    """Control: with the old spec-less policy the second worker fails, so the
    regression test above really detects F3 instead of passing by accident."""

    original = ModelPolicy.note_failure

    def legacy_note_failure(self, category, failed_spec=None):
        return original(self, category)  # the old one-argument behaviour

    monkeypatch.setattr(ModelPolicy, "note_failure", legacy_note_failure)
    runner = make_runner(project, provider_agent, fake_renderer)
    jobs = [job_for(project, "Race One"), job_for(project, "Race Two")]

    summary = run_batch(runner, jobs, parallel=2, log=lambda *_: None)

    assert sorted(result.status for result in summary.results) == [FAILED, OK]
    assert len(provider_agent.calls()) == 3  # no second retry for the in-flight loser
