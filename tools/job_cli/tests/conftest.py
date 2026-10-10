"""Shared test fixtures.

Every test runs against a temporary project root and a temporary database.
The personal data/job.sqlite3 and the real applications/ folder are never touched.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_cli.cli import app
from job_cli.errors import Cancelled, JobError

from helpers import make_app

REAL_ROOT = Path(__file__).resolve().parents[3]  # the repository root (job-hunter)


@dataclass
class Result:
    exit_code: int
    output: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copytree(REAL_ROOT / "sql", root / "sql")
    return root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def run(project_root: Path, runner: CliRunner):
    """Invoke the CLI the way main() does: JobError becomes exit code 2."""
    default_db = project_root / "data" / "job.sqlite3"

    def _run(*args: str, db: Path | None = None) -> Result:
        argv = ["--root", str(project_root), "--db", str(db or default_db)]
        argv += [str(a) for a in args]
        try:
            result = runner.invoke(app, argv, catch_exceptions=False)
        except Cancelled as exc:
            return Result(130, f"cancelled: {exc}")
        except JobError as exc:
            return Result(2, f"error: {exc}")
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", "") or ""
        return Result(result.exit_code, result.output if stdout is None else stdout, stderr)

    _run.db = default_db  # type: ignore[attr-defined]
    _run.root = project_root  # type: ignore[attr-defined]
    _run.tmp = project_root.parent  # type: ignore[attr-defined]
    return _run


@pytest.fixture
def new_app(run):
    def _new(company: str = "Acme GmbH", role: str = "Backend Engineer",
             submitted_on: str = "2026-01-05", *extra: str) -> str:
        return make_app(run, company, role, submitted_on, *extra)
    return _new


@pytest.fixture
def log_ok(run):
    def _log(*args: str):
        result = run("log", *args)
        assert result.ok, result.output
        return result
    return _log


@pytest.fixture
def new_round(run):
    """Create a round through the CLI by logging an invitation."""
    def _new(company: str, date: str, phase: str = "interview", round_type: str = "general",
             fmt: str = "video", *extra: str):
        result = run("log", company, "--event", "invited", "--date", date, "--round", "new",
                     "--phase", phase, "--round-type", round_type, "--format", fmt, *extra)
        assert result.ok, result.output
        return result
    return _new
