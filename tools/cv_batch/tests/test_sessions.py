import threading

import pytest

from cv_batch.model import ASTRA, DEEPSEEK
from cv_batch.sessions import ModelPolicy, SessionPolicy


def _barrier_workers(count: int, target) -> tuple[list[threading.Thread], threading.Barrier]:
    """Run ``target`` in ``count`` threads released at the same instant."""
    barrier = threading.Barrier(count)
    threads = [threading.Thread(target=target, args=(barrier,)) for _ in range(count)]
    return threads, barrier


def test_fresh_sessions_are_never_resumed(tmp_path):
    policy = SessionPolicy("fresh", tmp_path)
    assert policy.plan().resume_id is None
    policy.record("sess-1")
    assert policy.plan().resume_id is None
    assert policy.plan().session_dir == tmp_path


def test_reuse_mode_resumes_the_single_tool_owned_session(tmp_path):
    policy = SessionPolicy("reuse", tmp_path)
    first = policy.plan()
    assert first.resume_id is None
    policy.record("sess-1")
    second = policy.plan()
    assert second.resume_id == "sess-1"
    assert second.session_dir == tmp_path
    policy.record("sess-2")  # only the first id becomes the anchor
    assert policy.plan().resume_id == "sess-1"
    policy.reset()
    assert policy.plan().resume_id is None


def test_none_mode_uses_no_session(tmp_path):
    policy = SessionPolicy("none", tmp_path)
    plan = policy.plan()
    assert plan.no_session is True
    assert plan.session_dir is None and plan.resume_id is None


def test_unknown_session_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        SessionPolicy("shared", tmp_path)


def test_auto_switches_only_on_recognised_failures():
    policy = ModelPolicy("auto")
    assert policy.current() is ASTRA
    assert policy.note_failure("invalid_output") is False
    assert policy.note_failure("runtime") is False
    assert policy.note_failure("timeout") is False
    assert policy.current() is ASTRA
    assert policy.note_failure("quota") is True
    assert policy.current() is DEEPSEEK
    assert policy.note_failure("quota") is False  # already switched
    assert policy.astra_exhausted and policy.switch_reason == "quota"


@pytest.mark.parametrize("choice", ["astra", "deepseek"])
def test_explicit_model_choice_never_switches(choice):
    policy = ModelPolicy(choice)
    expected = ASTRA if choice == "astra" else DEEPSEEK
    assert policy.current() is expected
    assert policy.note_failure("quota") is False
    assert policy.current() is expected
    assert policy.astra_exhausted is False


def test_in_flight_astra_failure_retries_once_after_another_worker_switched():
    policy = ModelPolicy("auto")
    assert policy.note_failure("quota", failed_spec=ASTRA) is True  # worker A switches
    assert policy.current() is DEEPSEEK
    # worker B was already in flight on Astra when the switch happened
    assert policy.note_failure("quota", failed_spec=ASTRA) is True
    assert policy.current() is DEEPSEEK
    assert policy.astra_exhausted is True
    assert policy.switch_reason == "quota"


def test_failed_flash_call_never_grants_another_retry():
    policy = ModelPolicy("auto")
    assert policy.note_failure("quota", failed_spec=ASTRA) is True
    for _ in range(5):
        assert policy.note_failure("quota", failed_spec=DEEPSEEK) is False
    assert policy.note_failure("auth", failed_spec=DEEPSEEK) is False
    assert policy.note_failure("availability", failed_spec=DEEPSEEK) is False
    assert policy.current() is DEEPSEEK
    assert policy.switch_reason == "quota"  # the first recognised reason is kept


def test_flash_failure_before_any_switch_does_not_move_astra():
    policy = ModelPolicy("auto")
    assert policy.note_failure("quota", failed_spec=DEEPSEEK) is False
    assert policy.current() is ASTRA
    assert policy.astra_exhausted is False


@pytest.mark.parametrize("category", ["timeout", "runtime", "invalid_output", "session", "aborted", "cancelled"])
def test_only_recognised_categories_allow_a_retry(category):
    policy = ModelPolicy("auto")
    assert policy.note_failure(category, failed_spec=ASTRA) is False
    assert policy.current() is ASTRA
    assert policy.astra_exhausted is False


@pytest.mark.parametrize("choice", ["astra", "deepseek"])
def test_explicit_choice_never_grants_a_retry_even_for_an_astra_failure(choice):
    policy = ModelPolicy(choice)
    assert policy.note_failure("quota", failed_spec=ASTRA) is False
    assert policy.allowed_switch is False
    assert policy.astra_exhausted is False


def test_legacy_single_argument_form_keeps_the_first_switch_only():
    policy = ModelPolicy("auto")
    assert policy.note_failure("quota") is True
    assert policy.note_failure("quota") is False
    assert policy.current() is DEEPSEEK
    assert policy.switch_reason == "quota"


def test_two_workers_failing_at_the_same_time_both_retry_onto_flash():
    """F3 barrier regression: the in-flight loser of the switch race still retries."""
    workers = 4
    policy = ModelPolicy("auto")
    seen: list[tuple[str, bool, str]] = []
    lock = threading.Lock()

    def worker(barrier: threading.Barrier) -> None:
        spec = policy.current()  # every worker starts on Astra
        barrier.wait()  # release all workers at the same instant
        allowed = policy.note_failure("quota", failed_spec=spec)
        with lock:
            seen.append((spec.name, allowed, policy.current().name))

    threads, _ = _barrier_workers(workers, worker)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(seen) == workers
    assert all(entry == ("astra", True, "deepseek") for entry in seen)
    assert policy.astra_exhausted is True
    assert policy.switch_reason == "quota"


def test_runner_style_loop_cannot_retry_the_same_batch_more_than_once():
    """Mimic Runner._select: after the Flash retry fails, the loop must stop."""
    policy = ModelPolicy("auto")
    attempted: list[str] = []
    switched = False
    for _ in range(10):  # a buggy unbounded loop must still terminate
        spec = policy.current()
        attempted.append(spec.name)
        if not switched and policy.note_failure("quota", failed_spec=spec):
            switched = True
            continue
        break

    assert attempted == ["astra", "deepseek"]
