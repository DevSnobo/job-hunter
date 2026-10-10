"""Cancellation: Ctrl-C stops live child processes and reports a partial batch."""

from __future__ import annotations

import threading
import time

from cv_batch.runner import FAILED, run_batch

from conftest import target_name, target_path
from test_runner import VALID_ANSWER, job_for, make_runner


def _wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_cancel_all_terminates_a_live_model_child(project, fake_agent, fake_renderer):
    fake_agent.steps([{"sleep": 30, "answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Slow Job")
    outcome: list = []

    worker = threading.Thread(target=lambda: outcome.append(runner.run_job(job)), daemon=True)
    started = time.monotonic()
    worker.start()
    assert _wait_until(fake_agent.calls, timeout=10), "the fake model never started"

    runner.cancel_all()
    worker.join(timeout=15)

    assert not worker.is_alive(), "the cancelled child was not terminated"
    assert time.monotonic() - started < 20
    assert outcome and outcome[0].status == FAILED
    assert "cancel" in outcome[0].message.lower()
    assert not (target_path(job.directory, "de")).exists()


def test_cancel_all_terminates_a_live_renderer_child_and_group(project, fake_agent, fake_renderer, tmp_path):
    """A slow renderer is tracked; cancel kills it and its descendant (F2)."""
    marker = tmp_path / "descendant-marker.txt"
    fake_renderer.configure(sleep=30, descendant=str(marker), descendant_sleep=2.0)
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Slow Render")
    outcome: list = []

    worker = threading.Thread(target=lambda: outcome.append(runner.run_job(job)), daemon=True)
    worker.start()
    assert _wait_until(lambda: fake_renderer.calls(), timeout=15), "the fake renderer never started"
    assert not (target_path(job.directory, "de")).exists()

    runner.cancel_all()
    worker.join(timeout=15)

    assert not worker.is_alive(), "the cancelled renderer was not terminated"
    assert outcome and outcome[0].status == FAILED
    assert "cancel" in outcome[0].message.lower()
    # give a surviving renderer/descendant time to publish or write its marker
    time.sleep(3.0)
    assert not (target_path(job.directory, "de")).exists(), (
        "a PDF was published after cancellation"
    )
    assert not marker.exists(), "a renderer descendant survived the cancellation"


def test_parallel_slow_renderer_cancellation_publishes_nothing(project, fake_agent, fake_renderer, tmp_path):
    """Two parallel slow renders; cancel must never publish a late final PDF."""
    marker = tmp_path / "parallel-descendant.txt"
    fake_renderer.configure(sleep=4.0, descendant=str(marker), descendant_sleep=2.0)
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    jobs = [job_for(project, f"Parallel Slow {index}") for index in range(2)]
    summary_box: list = []

    def batch():
        summary_box.append(run_batch(runner, jobs, parallel=2, log=lambda *_: None))

    worker = threading.Thread(target=batch, daemon=True)
    worker.start()
    assert _wait_until(lambda: len(fake_renderer.calls()) >= 2, timeout=20), "renderers did not start"

    runner.cancel_all()
    worker.join(timeout=25)

    assert not worker.is_alive(), "parallel workers did not stop after cancellation"
    assert summary_box and all(result.status == FAILED for result in summary_box[0].results)
    time.sleep(5.0)  # longer than the fake renderer sleep
    for job in jobs:
        target = target_path(job.directory, "de")
        assert not target.exists(), f"a late PDF was published after cancellation: {target}"
    assert not marker.exists(), "a renderer descendant survived the parallel cancellation"


def test_cancellation_before_rendering_skips_the_renderer(project, fake_agent, fake_renderer):
    """The cancellation flag is checked before rendering (F2)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Pre Cancelled")
    runner.cancel.set()

    result = runner.run_job(job)

    assert result.status == FAILED
    assert "cancel" in result.message.lower()
    assert fake_renderer.calls() == []
    assert not (target_path(job.directory, "de")).exists()


def test_publish_gate_refuses_after_cancellation(project, fake_agent, fake_renderer):
    """A result that reaches the publish gate after cancel must not publish (F2)."""
    fake_agent.steps([{"answer": VALID_ANSWER}])
    runner = make_runner(project, fake_agent, fake_renderer)
    job = job_for(project, "Late Result")

    published: list = []

    def cancel_then_publish():
        runner.cancel.set()
        return None

    original = runner._publish

    def guarded(**kwargs):
        cancel_then_publish()
        return original(**kwargs)

    runner._publish = guarded  # type: ignore[assignment]
    result = runner.run_job(job)

    assert result.status == FAILED
    assert "cancel" in result.message.lower()
    assert not (target_path(job.directory, "de")).exists()
    assert list(project.state.glob("inputs/*.json")) == []
