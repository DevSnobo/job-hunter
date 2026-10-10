"""Coloured typed input (the shared ``answer`` style) and the stream-aware console."""

from __future__ import annotations

import fcntl
import io
import os
import pty
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest

from cv_batch import cli, tty_input


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeStream:
    """A writable stream with a fixed ``isatty()`` answer."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self.tty


# --------------------------------------------------- cross-tool style contract

def test_the_shared_answer_style_matches_the_job_cli():
    """The colour of the typed text is a cross-tool contract, not a local taste."""
    assert tty_input.ANSWER_STYLE == "fg:#FF9D00 bold"
    rules = dict(tty_input.input_style().style_rules)
    assert callable(tty_input.answer_processor)
    assert rules["answer"] == "fg:#FF9D00 bold"
    # the prompt text must stay in the terminal's regular font
    assert rules["prompt"] == ""
    assert tty_input.PROMPT_STYLE == ""


# ------------------------------------------------------ routing of the reader

def test_the_plain_path_never_imports_prompt_toolkit():
    """The colour path is optional: importing the CLI must not pull it in."""
    completed = subprocess.run(
        [sys.executable, "-c", "import sys, cv_batch.cli; print('prompt_toolkit' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_a_real_terminal_uses_the_coloured_reader(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTTY(""))
    monkeypatch.setattr(sys, "stdout", FakeTTY(""))
    assert cli._coloured_input_allowed(cli.Console(plain=False)) is True


def test_pipes_redirects_and_plain_mode_keep_the_plain_reader(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "de")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("prompt_toolkit must not run on a non-terminal")

    monkeypatch.setattr(tty_input, "read_coloured_line", forbidden)

    # not a TTY on stdin
    monkeypatch.setattr(sys, "stdin", io.StringIO("de\n"))
    monkeypatch.setattr(sys, "stdout", FakeTTY(""))
    assert cli._ask("Language: ", cli.Console(plain=False)) == "de"

    # --plain / --quiet: a real terminal, but plain input and no ANSI
    monkeypatch.setattr(sys, "stdin", FakeTTY("de\n"))
    monkeypatch.setattr(sys, "stdout", FakeTTY(""))
    assert cli._ask("Language: ", cli.Console(plain=True)) == "de"


def test_a_broken_coloured_reader_falls_back_to_the_plain_reader(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTTY("de\n"))
    monkeypatch.setattr(sys, "stdout", FakeTTY(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": "de")

    def broken(*_args, **_kwargs):
        raise RuntimeError("prompt_toolkit cannot drive this terminal")

    monkeypatch.setattr(tty_input, "read_coloured_line", broken)

    assert cli._ask("Language: ", cli.Console(plain=False)) == "de"


def test_eof_and_ctrl_c_still_cancel_in_both_paths(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", FakeTTY(""))
    monkeypatch.setattr(sys, "stdout", FakeTTY(""))

    def raiser(exception):
        def read(*_args, **_kwargs):
            raise exception

        return read

    for exception in (EOFError, KeyboardInterrupt):
        monkeypatch.setattr(tty_input, "read_coloured_line", raiser(exception))
        assert cli._ask("Language: ", cli.Console(plain=False)) is None

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert cli._ask("Language: ", cli.Console(plain=False)) is None


# --------------------------------------------------------- Console.error colour

def test_error_colour_follows_the_stream_it_writes_to(monkeypatch):
    """``cvs 2>file`` must not capture escape sequences (stdout may be a TTY)."""
    monkeypatch.setattr(sys, "stdout", FakeStream(True))
    stderr = FakeStream(True)
    monkeypatch.setattr(sys, "stderr", stderr)

    cli.Console(plain=False).error("boom")
    assert stderr.text == "\033[31merror: boom\033[0m\n"

    redirected = FakeStream(False)
    monkeypatch.setattr(sys, "stderr", redirected)
    cli.Console(plain=False).error("boom")
    assert redirected.text == "error: boom\n"

    quiet = FakeStream(True)
    monkeypatch.setattr(sys, "stderr", quiet)
    cli.Console(plain=True).error("boom")
    assert quiet.text == "error: boom\n"


def test_warn_and_good_still_colour_stdout(monkeypatch):
    stdout = FakeStream(True)
    monkeypatch.setattr(sys, "stdout", stdout)
    console = cli.Console(plain=False)
    console.warn("careful")
    console.good("done")
    assert stdout.text == "\033[33mwarn: careful\033[0m\n\033[32mdone\033[0m\n"


# ---------------------------------------------------------------- pty smoke test

def _run_prompt_in_a_pty(prompt: str, typed: bytes, timeout: float = 20.0):
    """Drive :func:`cli._ask` on a real pty; returns (result dict, raw bytes).

    A real terminal is required to prove the colour decision; the pty is given a
    fixed 80x24 window so prompt_toolkit renders without a terminal emulator.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    stdin = os.fdopen(os.dup(slave), "r")
    stdout = os.fdopen(os.dup(slave), "w")
    result: dict = {}
    chunks: list[bytes] = []
    previous_stdin, previous_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stdin, stdout
    try:
        def runner() -> None:
            try:
                result["value"] = cli._ask(prompt, cli.Console(plain=False))
            except BaseException as exc:  # pragma: no cover - reported as evidence
                result["error"] = repr(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        time.sleep(0.5)  # let prompt_toolkit install its terminal handler
        os.write(master, typed)
        thread.join(timeout=timeout)
        finished = not thread.is_alive()
        os.set_blocking(master, False)
        deadline = time.monotonic() + 2.0
        while finished and time.monotonic() < deadline:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, OSError):
                time.sleep(0.02)
                continue
            if not data:
                break
            chunks.append(data)
    finally:
        sys.stdin, sys.stdout = previous_stdin, previous_stdout
        # A hung session keeps its fds (rather than reading from a closed
        # terminal); the thread is a daemon and the suite is over anyway.
        if not thread.is_alive():
            stdin.close()
            stdout.close()
            os.close(master)
            os.close(slave)
    return result, b"".join(chunks)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty support on this platform")
def test_typed_text_is_painted_with_the_shared_answer_colour(monkeypatch):
    """pty smoke test: 38;5;214 + bold on the typed text, prompt string unchanged."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    prompt = "Language [DE/en] (blank = DE): "

    result, raw = _run_prompt_in_a_pty(prompt, b"Hello\r")

    assert result.get("value") == "Hello", result
    text = raw.decode("utf-8", "replace")
    assert "38;5;214" in text, text
    assert prompt in text, text


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty support on this platform")
def test_ctrl_d_on_a_real_terminal_still_cancels(monkeypatch):
    """Ctrl-D keeps the old EOF behaviour (``None`` = cancel) on the coloured path."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    result, _raw = _run_prompt_in_a_pty("Language [DE/en] (blank = DE): ", b"\x04")

    assert result.get("value") is None, result
