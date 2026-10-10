"""Session and model-fallback policy.

Three documented session modes:

``fresh``  (default) one new, tool-owned session per job; sessions are saved for
           inspection but never resumed.
``reuse``  one single tool-owned session is created for the first job and
           resumed for every later job of the batch, sequentially.  Reuse is not
           a token promise: the history grows and provider caching decides.
``none``   ``--no-session``; nothing is saved anywhere.

Parallel batches always use fresh sessions: a live session is never shared.

The model-fallback policy is shared, not per worker.  It is thread-safe, and a
worker whose in-flight Astra call fails after another worker already switched
still gets its own one bounded retry on the now-current Flash model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .config import ASTRA_MODEL, ASTRA_PROVIDER, DEEPSEEK_MODEL, DEEPSEEK_PROVIDER
from .model import ASTRA, DEEPSEEK, SWITCH_CLASSES, ModelSpec, initial_model

SESSION_MODES = ("fresh", "reuse", "none")


@dataclass(frozen=True)
class SessionPlan:
    session_dir: Path | None
    resume_id: str | None
    no_session: bool


class SessionPolicy:
    """Decides the session arguments of each call (thread-safe)."""

    def __init__(self, mode: str, session_dir: Path) -> None:
        if mode not in SESSION_MODES:
            raise ValueError(f"unknown session mode {mode!r}; choose from {SESSION_MODES!r}")
        self.mode = mode
        self.session_dir = session_dir
        self._anchor: str | None = None
        self._lock = threading.Lock()

    def plan(self) -> SessionPlan:
        with self._lock:
            if self.mode == "none":
                return SessionPlan(None, None, True)
            if self.mode == "reuse" and self._anchor:
                return SessionPlan(self.session_dir, self._anchor, False)
            return SessionPlan(self.session_dir, None, False)

    def record(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            if self.mode == "reuse" and self._anchor is None:
                self._anchor = session_id

    def reset(self) -> None:
        """Forget the reuse anchor (used when the model changes mid-batch)."""
        with self._lock:
            self._anchor = None

    @property
    def anchor(self) -> str | None:
        return self._anchor


class ModelPolicy:
    """Astra first, DeepSeek Flash as the only fallback.  Never DeepSeek Pro.

    One instance is shared by every worker of a parallel batch, so the whole
    transition is guarded by a single lock: a worker that fails while another
    worker is switching must still see a consistent model and switch reason.
    """

    def __init__(self, choice: str) -> None:
        if choice not in ("auto", "astra", "deepseek"):
            raise ValueError(f"unknown model choice {choice!r}; choose from auto|astra|deepseek")
        self.choice = choice
        self._lock = threading.Lock()
        self._astra_exhausted = False
        self._switch_reason = ""

    @property
    def astra_exhausted(self) -> bool:
        with self._lock:
            return self._astra_exhausted

    @property
    def switch_reason(self) -> str:
        with self._lock:
            return self._switch_reason

    @property
    def allowed_switch(self) -> bool:
        """True while an ``auto`` batch may still move from Astra to Flash."""
        with self._lock:
            return self.choice == "auto" and not self._astra_exhausted

    def current(self) -> ModelSpec:
        """The model a new call must use right now."""
        with self._lock:
            if self.choice == "auto" and self._astra_exhausted:
                return DEEPSEEK
            return initial_model(self.choice)

    def note_failure(self, category: str, failed_spec: ModelSpec | None = None) -> bool:
        """Record a failed call; return True when *this* caller must retry once.

        ``failed_spec`` is the model the caller actually ran.  A recognised
        Astra failure switches the batch to Flash and grants the caller one
        retry on it -- also when another worker switched first, so an in-flight
        Astra call is never abandoned by the race.

        The retry stays bounded: a failure of any model other than Astra (the
        Flash fallback, or an explicitly chosen model) never grants a retry,
        because retrying would repeat the same model.

        The legacy one-argument call keeps the old behaviour: it only reports
        the first synthesised switch of the batch and never an in-flight one.
        """
        if category not in SWITCH_CLASSES:
            return False
        if self.choice != "auto":
            return False
        with self._lock:
            if failed_spec is None:
                if self._astra_exhausted:
                    return False
                self._mark_exhausted(category)
                return True
            if failed_spec != ASTRA:
                return False
            self._mark_exhausted(category)
            return True

    def _mark_exhausted(self, category: str) -> None:
        """Switch to Flash once; the first recognised reason is kept."""
        if not self._astra_exhausted:
            self._astra_exhausted = True
            self._switch_reason = category


__all__ = [
    "ASTRA",
    "ASTRA_MODEL",
    "ASTRA_PROVIDER",
    "DEEPSEEK",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_PROVIDER",
    "ModelPolicy",
    "SESSION_MODES",
    "SessionPlan",
    "SessionPolicy",
]
