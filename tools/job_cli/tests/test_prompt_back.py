"""Escape in a prompt asks a guided flow to step back one question.

The prompt itself only raises the signal; the flow decides what the previous
question is. These tests drive the real questionary prompts through a pipe so
the Escape binding is checked against prompt_toolkit, not a stand-in.
"""
from __future__ import annotations

import questionary
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from job_cli import ui

OPTIONS = [("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")]


def _ask(question, keys: str):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        question.application.input = pipe
        question.application.output = DummyOutput()
        return question.unsafe_ask()


@pytest.mark.parametrize("factory", [
    lambda: questionary.text("Question"),
    lambda: questionary.confirm("Question"),
    lambda: questionary.select("Question", ["alpha", "beta"]),
    lambda: questionary.checkbox("Question", ["alpha", "beta"]),
])
def test_escape_leaves_the_prompt_with_back(factory):
    question = factory()
    ui._bind_back(question)
    with pytest.raises(ui.Back):
        _ask(question, "\x1b")


def test_escape_does_not_break_arrow_keys_in_a_bound_list():
    question = ui._select_question("Pick", OPTIONS, back=True)
    assert _ask(question, "\x1b[B\x1b[B\r") == "gamma"


def test_a_bound_text_prompt_still_accepts_enter():
    question = questionary.text("Question", default="shown")
    ui._bind_back(question)
    assert _ask(question, "\r") == "shown"
