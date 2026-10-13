"""The number keys confirm a select entry in one keypress.

A list is normally driven with the arrow keys and Enter. These tests feed keys
into the real questionary prompt (through a pipe input, not a stand-in) and check
that a digit confirms the matching entry at once, that the arrow keys keep
working, and that a digit with no entry does nothing.
"""
from __future__ import annotations

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from job_cli import ui

OPTIONS = [("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")]


def _press(keys: str, options=OPTIONS) -> str:
    """Run the real prompt with ``keys`` typed into it and return its answer."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        question = ui._select_question("Pick", options)
        question.application.input = pipe
        question.application.output = DummyOutput()
        return question.unsafe_ask()


def test_a_digit_confirms_that_entry_at_once():
    assert _press("1") == "alpha"
    assert _press("2") == "beta"
    assert _press("3") == "gamma"


def test_the_tenth_entry_is_the_zero_key():
    options = [(str(i), f"Option {i}") for i in range(1, 11)]
    assert _press("0", options) == "10"


def test_arrow_keys_and_enter_still_work():
    assert _press("\x1b[B\x1b[B\r") == "gamma"


def test_a_digit_without_an_entry_changes_nothing():
    # 9 has no entry in a three-item list; it is ignored and Enter takes the cursor
    assert _press("9\r") == "alpha"
