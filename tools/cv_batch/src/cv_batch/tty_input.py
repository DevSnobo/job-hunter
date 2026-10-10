"""The coloured single-line prompt for the interactive CLI.

The ``job`` CLI (questionary) paints the text the user *types* with questionary's
default ``answer`` class - orange and bold - while the prompt text itself stays
in the terminal's regular font.  ``cv`` should look identical, so its interactive
prompts are read through a small prompt_toolkit session that renders the input
line with the same style class.

The class name and its colour are a cross-tool contract, so the literal style
string lives in :data:`ANSWER_STYLE` (keep it in sync with
``tools/job_cli/src/job_cli/ui.py`` and
``tools/posting_entry/src/posting_entry/ui.py``, which uses the same
helper names on purpose).

This module is optional at runtime: prompt_toolkit is imported lazily inside the
functions, and the caller (:func:`cv_batch.cli._ask`) uses the plain ``input()``
path whenever the streams are not a real TTY or the launcher cannot be built.
"""

from __future__ import annotations

from functools import lru_cache

#: Style of the text the user types: questionary's default ``answer`` class, i.e.
#: 256-colour 214 (``#FF9D00``), bold.  This is the cross-tool contract; do not
#: "improve" the colour here without changing the other two tools as well.
ANSWER_STYLE = "fg:#FF9D00 bold"

#: Explicit empty rule for prompt_toolkit's ``prompt`` class.  Without it the
#: prompt message would be rendered by prompt_toolkit's own default style
#: (blue, bold) instead of the terminal's regular font.
PROMPT_STYLE = ""


@lru_cache(maxsize=1)
def input_style():
    """The shared prompt_toolkit style: coloured input, regular prompt text.

    Built lazily (and cached) so that importing this module never imports
    prompt_toolkit.
    """
    from prompt_toolkit.styles import Style

    return Style.from_dict({"answer": ANSWER_STYLE, "prompt": PROMPT_STYLE})


def answer_processor():
    """A processor that paints the whole input line with the ``answer`` class."""
    from prompt_toolkit.layout.processors import Processor, Transformation

    class AnswerClassProcessor(Processor):
        """Attach ``class:answer`` to every fragment of the input line.

        prompt_toolkit renders the line as ``(style, text)`` tuples in this
        version; the attribute form is handled defensively so that a different
        prompt_toolkit version cannot break the prompt.
        """

        def apply_transformation(self, transformation_input):
            texts = [
                fragment[1] if isinstance(fragment, tuple) else fragment.text
                for fragment in transformation_input.fragments
            ]
            return Transformation([("class:answer", text) for text in texts])

    return AnswerClassProcessor()


def read_coloured_line(prompt: str) -> str:
    """Read one line, rendering the typed text in the shared ``answer`` colour.

    The ``prompt`` string is passed through verbatim (questionary adds no prefix
    either, so the visible question stays exactly the text it is today).  Raises
    the same exceptions as :func:`input`: ``EOFError`` on Ctrl-D and
    ``KeyboardInterrupt`` on Ctrl-C.
    """
    from prompt_toolkit import PromptSession

    session = PromptSession(style=input_style(), input_processors=[answer_processor()])
    return session.prompt(prompt)


__all__ = ["ANSWER_STYLE", "PROMPT_STYLE", "answer_processor", "input_style", "read_coloured_line"]
