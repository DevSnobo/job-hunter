"""The sibling posting capture: launcher discovery and the run wrapper."""
from __future__ import annotations

from pathlib import Path

from job_cli import capture


class _Ui:
    """A stand-in for the ui module that records the notes it is given."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)


def _make_launcher(root: Path, *, executable: bool = True) -> Path:
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "posting"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755 if executable else 0o644)
    return script


def test_launcher_for_needs_the_executable_bin_posting(tmp_path):
    assert capture.launcher_for(tmp_path) is None
    script = _make_launcher(tmp_path)
    assert capture.launcher_for(tmp_path) == script


def test_launcher_for_refuses_a_non_executable_file(tmp_path):
    _make_launcher(tmp_path, executable=False)
    assert capture.launcher_for(tmp_path) is None


def test_run_reports_a_missing_launcher_without_starting_anything(tmp_path):
    ui = _Ui()
    started: list = []
    assert capture.run(ui, tmp_path, runner=lambda *a, **k: started.append(a)) is False
    assert started == []
    assert any("missing or not executable" in note for note in ui.notes)


def test_run_starts_the_launcher_from_the_job_root_with_job_root_set(tmp_path):
    launcher = _make_launcher(tmp_path)
    ui = _Ui()
    calls: list[dict] = []

    class Completed:
        returncode = 0

    def runner(argv, cwd=None, env=None):
        calls.append({"argv": argv, "cwd": cwd, "env": env})
        return Completed()

    assert capture.run(ui, tmp_path, runner=runner) is True
    assert calls[0]["argv"] == [str(launcher)]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["env"]["JOB_ROOT"] == str(tmp_path)


def test_run_warns_on_a_nonzero_exit_but_still_says_it_ran(tmp_path):
    _make_launcher(tmp_path)
    ui = _Ui()

    class Completed:
        returncode = 1

    assert capture.run(ui, tmp_path, runner=lambda *a, **k: Completed()) is True
    assert any("stopped (exit 1)" in note for note in ui.notes)


def test_run_reports_a_launcher_that_cannot_start(tmp_path):
    _make_launcher(tmp_path)
    ui = _Ui()

    def runner(*args, **kwargs):
        raise OSError("no interpreter")

    assert capture.run(ui, tmp_path, runner=runner) is False
    assert any("could not start" in note for note in ui.notes)
