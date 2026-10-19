"""Console input helpers: single-line prompts, multi-line paste, clipboard.

On a real terminal the single-line questions are asked through a small
prompt_toolkit session that paints the text the user *types* in the same
orange+bold as the ``job`` CLI (questionary's ``answer`` class).  Pipes,
redirects, injected test doubles and any prompt_toolkit failure keep the plain
``stdin.readline()`` path.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache

TERMINATOR = "."
#: Style of the text the user types: questionary's default ``answer`` class,
#: i.e. 256-colour 214 (``#FF9D00``), bold.  The ``job`` CLI shows exactly this
#: colour, so the literal is a cross-tool contract - keep it in sync with
#: ``tools/job_cli/src/job_cli/ui.py`` and
#: ``tools/cv_batch/src/cv_batch/tty_input.py``.
ANSWER_STYLE = "fg:#FF9D00 bold"
#: Explicit empty rule for prompt_toolkit's ``prompt`` class.  Without it the
#: prompt message itself would be drawn with prompt_toolkit's own default style
#: instead of the terminal's regular font, and only the *input* should be
#: coloured here.
PROMPT_STYLE = ""

CLIPBOARD_REQUEST = ":clipboard"
# Each entry is (executable name, extra arguments). ``read_clipboard`` resolves
# the executable with ``shutil.which`` and prepends it, so the argument list
# must not repeat the tool name.
CLIPBOARD_COMMANDS = (
    ("wl-paste", []),
    ("xclip", ["-selection", "clipboard", "-o"]),
    ("xsel", ["--clipboard", "--output"]),
)


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


class Back(Exception):
    """Escape was pressed: return to the previous question."""


def read_coloured_line(prompt: str, *, back: bool = False) -> str:
    """Read one line with the typed text in the shared ``answer`` colour.

    The prompt string is passed through verbatim: questionary adds no prefix
    either, so the visible question stays exactly the same text.  Raises the
    same exceptions as :func:`input`, i.e. ``EOFError`` on Ctrl-D and
    ``KeyboardInterrupt`` on Ctrl-C.

    With ``back=True``, Escape leaves the question with :class:`Back` instead
    of inserting anything.  The binding is eager so a bare Escape does not wait
    for a meta-key sequence; prompt_toolkit still recognises the arrow keys
    (which also start with Escape) before it.
    """
    from prompt_toolkit import PromptSession

    key_bindings = None
    if back:
        from prompt_toolkit.key_binding import KeyBindings

        key_bindings = KeyBindings()

        @key_bindings.add("escape", eager=True)
        def go_back(event):
            event.app.exit(exception=Back())

    session = PromptSession(style=input_style(), input_processors=[answer_processor()],
                            key_bindings=key_bindings)
    return session.prompt(prompt)


class Cancelled(Exception):
    """The user (or end of input) aborted the current entry."""


class Ui:
    """Thin wrapper around stdin/stdout so the flow stays testable."""

    def __init__(self, stdin=None, stdout=None, stderr=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    def say(self, text: str = "") -> None:
        print(text, file=self.stdout)
        self.stdout.flush()

    def warn(self, text: str) -> None:
        print(f"! {text}", file=self.stderr)
        self.stderr.flush()

    def interactive_session(self) -> bool:
        """True only for the real interactive terminal (a live user).

        Same strict gate as :meth:`coloured_input_allowed`, reused on purpose:
        an extra question is only ever asked when a human is at the keyboard.
        Piped/redirected runs and injected ``StringIO`` test doubles must never
        see it, because nobody could answer it.
        """
        return self.coloured_input_allowed()

    def coloured_input_allowed(self) -> bool:
        """True only when the coloured editor may be used.

        The coloured prompt_toolkit editor is for the real interactive terminal
        only: both streams must be the actual ``sys`` streams and both must be
        TTYs.  Pipes, redirects, ``StringIO`` test doubles and the
        documentation/scripting use of the tool must keep the plain
        ``stdin.readline()`` behaviour unchanged.
        """
        if self.stdin is not sys.stdin or self.stdout is not sys.stdout:
            return False
        try:
            return bool(self.stdin.isatty() and self.stdout.isatty())
        except (AttributeError, OSError, ValueError):  # pragma: no cover - closed stream
            return False

    def _read_line(self, *, prompt: str | None = None, eof_is_finish: bool = False,
                   back: bool = False):
        """Return the next line without its newline, or None on EOF.

        ``prompt`` is passed by the single-line questions only.  With it, and on
        a real terminal, the question is asked through the coloured editor (see
        :func:`read_coloured_line`), which draws the prompt text itself; in every
        other case the prompt is written to stdout and the line comes from
        ``stdin.readline()``, exactly as before.

        The multi-line posting reader calls this without ``prompt`` on purpose:
        pasted text is never coloured.
        """
        if prompt is not None and self.coloured_input_allowed():
            try:
                if back:
                    return read_coloured_line(prompt, back=True)
                return read_coloured_line(prompt)
            except Back:
                raise
            except KeyboardInterrupt:
                raise Cancelled("interrupted") from None
            except EOFError:
                # prompt_toolkit raises instead of returning an empty string;
                # EOF must keep meaning "no line" for every caller.
                return None
            except Exception:
                # Defensive: a missing or broken prompt_toolkit, or a terminal it
                # cannot drive, must never take the capture flow down.  Re-ask
                # through the plain reader instead (at worst the question text is
                # then printed twice).
                pass
        if prompt is not None:
            self.stdout.write(prompt)
            self.stdout.flush()
        try:
            line = self.stdin.readline()
        except KeyboardInterrupt:
            raise Cancelled("interrupted") from None
        except EOFError:
            return None
        if line == "":
            return None
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        return line

    def ask_optional(self, prompt: str, *, default: str = "", back: bool = False):
        """Ask for one line. Blank input yields ``default``; EOF returns ``None``.

        Use this when end-of-input means "stop normally" rather than "cancel".
        With ``back=True``, Escape raises :class:`Back` so the caller can ask the
        previous question again.
        """
        line = self._read_line(prompt=prompt, back=back)
        if line is None:
            self.say()
            return None
        line = line.strip()
        return line or default

    def ask(self, prompt: str, *, default: str = "", back: bool = False) -> str:
        """Ask for one line. Blank input yields ``default``; EOF cancels."""
        line = self.ask_optional(prompt, default=default, back=back)
        if line is None:
            raise Cancelled("end of input")
        return line

    def ask_yes_no_or_eof(self, prompt: str, *, default: bool = True,
                          back: bool = False):
        """Like ``ask_yes_no`` but return ``None`` on EOF instead of cancelling."""
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.ask_optional(f"{prompt} {suffix} ", back=back)
            if answer is None:
                return None
            answer = answer.strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes", "j", "ja"}:
                return True
            if answer in {"n", "no", "nein"}:
                return False
            self.say("Please answer 'y' or 'n'.")

    def ask_yes_no(self, prompt: str, *, default: bool = True,
                   back: bool = False) -> bool:
        """Ask a yes/no question. EOF cancels (nothing is written on EOF)."""
        answer = self.ask_yes_no_or_eof(prompt, default=default, back=back)
        if answer is None:
            raise Cancelled("end of input")
        return answer

    def ask_multiline(self) -> tuple[str, bool]:
        """Read a pasted posting text. Returns ``(text, terminated_by_dot)``."""
        self.say("Posting text: paste it now. Multiple lines, blank lines and Unicode are kept.")
        self.say("Finish with a line that contains only a dot '.' and press Enter.")
        self.say(f"Type {CLIPBOARD_REQUEST} alone to read the text from the clipboard instead.")
        self.say("(Ctrl-D stops the text too, but then you may lose the paste: prefer the dot.)")
        self.say()

        chunks: list[str] = []
        while True:
            line = self._read_line()
            if line is None:
                if any(chunk.strip() for chunk in chunks):
                    self.warn("input ended without the '.' terminator line")
                    return "".join(chunks), False
                raise Cancelled("end of input while reading the posting text")
            if line.strip() == TERMINATOR:
                return "".join(chunks), True
            if line.strip() == CLIPBOARD_REQUEST and not any(chunk.strip() for chunk in chunks):
                text, detail = read_clipboard()
                if text is None:
                    self.warn(f"clipboard not read ({detail}); paste manually instead")
                    continue
                self.say(f"Read {len(text)} characters from the clipboard ({detail}).")
                return normalize_pasted_text(text), True
            chunks.append(line + "\n")


def normalize_pasted_text(text: str) -> str:
    """Normalize line endings and guarantee one trailing newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def read_clipboard(timeout: float = 10.0):
    """Try wl-paste, xclip, xsel in turn.

    Returns ``(text, tool)`` on success and ``(None, reason)`` otherwise. The
    clipboard is only ever read when the caller explicitly asks for it.
    """
    attempts: list[str] = []
    for tool, argv in CLIPBOARD_COMMANDS:
        executable = shutil.which(tool)
        if not executable:
            attempts.append(f"{tool}: not installed")
            continue
        try:
            proc = subprocess.run([executable, *argv], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            attempts.append(f"{tool}: timed out")
            continue
        except OSError as exc:
            attempts.append(f"{tool}: {exc}")
            continue
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            attempts.append(f"{tool}: {detail}")
            continue
        text = proc.stdout.decode("utf-8", "replace")
        if not text.strip():
            attempts.append(f"{tool}: clipboard is empty")
            continue
        return text, tool
    return None, "; ".join(attempts)
