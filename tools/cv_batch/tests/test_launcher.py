"""Integration: the ``./cv`` launcher works from any directory and with spaces."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import el_entry_id, prime_agent_command, selection_answer

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = REPO_ROOT / "cv"

EL = el_entry_id()
ANSWER = selection_answer({EL: [f"{EL}/el1-short"]})


def test_launcher_runs_from_a_path_with_spaces(project, fake_agent, fake_renderer, tmp_path):
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Launcher Job")

    workdir = tmp_path / "a dir with spaces"
    workdir.mkdir()
    env = dict(os.environ)
    env.pop("CV_JOB_ROOT", None)
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "--root",
            str(project.root),
            "--agent-command",
            prime_agent_command(fake_agent.exe),
            "--agent-format",
            "prime-agent-json",
            "--generator-exe",
            fake_renderer.exe,
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--plain",
        ],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    target = project.target("Launcher Job", "de")
    assert target.is_file() and target.read_bytes().startswith(b"%PDF-")
    assert "summary: ok 1" in completed.stdout
    # nothing was written into the caller's directory
    assert list(workdir.iterdir()) == []


def test_launcher_help_works_without_a_terminal(tmp_path):
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    completed = subprocess.run(
        [str(LAUNCHER), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0
    assert "Tailor one one-page CV" in completed.stdout


def test_launcher_regenerates_an_invalid_target_only_with_the_optin(project, fake_agent, fake_renderer, tmp_path):
    """F1 through the real launcher: the opt-in makes an invalid target selectable."""
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Launcher Invalid")
    target = (
        project.target("Launcher Invalid", "de")
    )
    target.write_bytes(b"junk that is not a pdf")

    workdir = tmp_path / "another dir"
    workdir.mkdir()
    env = dict(os.environ)
    env.pop("CV_JOB_ROOT", None)
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "--root",
            str(project.root),
            "--agent-command",
            prime_agent_command(fake_agent.exe),
            "--agent-format",
            "prime-agent-json",
            "--generator-exe",
            fake_renderer.exe,
            "--language",
            "de",
            "--regenerate-invalid",
            "--select",
            "1",
            "--yes",
            "--plain",
        ],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert "[REPLACE: target is not a PDF (bad header)]" in completed.stdout
    assert "summary: ok 1" in completed.stdout
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list((project.root / "state" / "replaced").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"junk that is not a pdf"
    assert list(workdir.iterdir()) == []


def test_launcher_replaces_an_existing_valid_cv_only_with_overwrite(project, fake_agent, fake_renderer, tmp_path):
    """Through the real launcher: an existing valid CV needs --overwrite."""
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Launcher Existing")
    target = project.target("Launcher Existing", "de")
    target.write_bytes(b"%PDF-1.4 an older tailored CV")
    argv = [
        str(LAUNCHER),
        "--root",
        str(project.root),
        "--agent-command",
        prime_agent_command(fake_agent.exe),
        "--agent-format",
        "prime-agent-json",
        "--generator-exe",
        fake_renderer.exe,
        "--language",
        "de",
        "--plain",
    ]

    default = subprocess.run(
        argv + ["--select", "1", "--dry-run"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert default.returncode == 0
    assert "nothing to do: every usable posting folder already has a valid DE CV" in default.stdout
    assert "--overwrite" in default.stdout
    assert target.read_bytes() == b"%PDF-1.4 an older tailored CV"
    assert fake_agent.calls() == []

    workdir = tmp_path / "overwrite dir"
    workdir.mkdir()
    env = dict(os.environ)
    env.pop("CV_JOB_ROOT", None)
    completed = subprocess.run(
        argv + ["--overwrite", "--select", "1", "--yes"],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert "[OVERWRITE: valid CV already exists]" in completed.stdout
    assert "summary: ok 1" in completed.stdout
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list((project.root / "state" / "replaced").glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"%PDF-1.4 an older tailored CV"
    assert list(workdir.iterdir()) == []


def test_launcher_never_numbers_an_invalid_target_by_default(project, fake_agent, fake_renderer, tmp_path):
    """F1: without the opt-in the launcher reports the invalid target honestly."""
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    project.add_app("Launcher Invalid")
    target = (
        project.target("Launcher Invalid", "de")
    )
    target.write_bytes(b"junk")

    completed = subprocess.run(
        [
            str(LAUNCHER),
            "--root",
            str(project.root),
            "--agent-command",
            prime_agent_command(fake_agent.exe),
            "--agent-format",
            "prime-agent-json",
            "--language",
            "de",
            "--plain",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert "already has a valid" not in completed.stdout
    assert "nothing to do by default" in completed.stdout
    assert "--regenerate-invalid" in completed.stdout
    assert target.read_bytes() == b"junk"


def test_launcher_folder_scope_tailors_only_that_folder(project, fake_agent, fake_renderer, tmp_path):
    """The postings tool's call shape: ``./cv --folder <name> --language de --all --yes``."""
    if not LAUNCHER.is_file():
        pytest.skip(f"launcher not found: {LAUNCHER}")
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Launcher Scope")
    project.add_app("Launcher Other")

    workdir = tmp_path / "folder scope dir"
    workdir.mkdir()
    env = dict(os.environ)
    env.pop("CV_JOB_ROOT", None)
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "--root",
            str(project.root),
            "--agent-command",
            prime_agent_command(fake_agent.exe),
            "--agent-format",
            "prime-agent-json",
            "--generator-exe",
            fake_renderer.exe,
            "--folder",
            "Launcher Scope",
            "--language",
            "de",
            "--all",
            "--yes",
            "--plain",
        ],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Folder filter  : Launcher Scope   (1 of 2 usable folders in scope)" in completed.stdout
    assert "summary: ok 1, skipped 0, failed 0" in completed.stdout
    assert project.target("Launcher Scope", "de").is_file()
    assert not project.target("Launcher Other", "de").exists()
    assert list(workdir.iterdir()) == []
