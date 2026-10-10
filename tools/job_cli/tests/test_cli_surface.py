"""Launcher, isolation, exit codes, and the no-DDL-in-Python contract."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import payload, rows

REAL_ROOT = Path(__file__).resolve().parents[3]  # the repository root (job-hunter)
TOOLING = REAL_ROOT / "tools" / "job_cli"
# The root convenience entry is a symlink to bin/job; both forms must work.
LAUNCHER = REAL_ROOT / "job"
BIN_LAUNCHER = REAL_ROOT / "bin" / "job"
PYTHON = sys.executable


def test_bare_invocation_prints_help_without_a_terminal(run):
    result = run()
    assert result.exit_code == 2
    assert "Usage:" in result.output and "COMMAND" in result.output


def test_unknown_command_and_option_fail(run):
    assert run("frobnicate").exit_code != 0
    assert run("list", "--nope").exit_code != 0


def test_no_migrate_flag_refuses_a_missing_database(run, project_root):
    result = run("--no-migrate", "list")
    assert result.exit_code == 2
    assert "database not found" in result.output


def test_databases_are_isolated(run, project_root, tmp_path):
    first = project_root / "data" / "job.sqlite3"
    second = tmp_path / "other.sqlite3"
    assert run("add", "--company", "Only here", "--role", "Dev",
               "--submitted-on", "2026-01-05").ok
    assert rows(run, "list", "--json", db=second) == []
    assert [row["company"] for row in rows(run, "list", "--json", db=second)] == []
    assert run("add", "--company", "Second db", "--role", "Dev", "--submitted-on",
               "2026-01-06", db=second).ok
    assert len(rows(run, "list", "--json", db=second)) == 1
    assert len(rows(run, "list", "--json", db=first)) == 1


def test_console_script_honours_job_root_and_job_db(project_root, tmp_path):
    env = dict(os.environ)
    env["JOB_ROOT"] = str(project_root)
    env["JOB_DB"] = str(project_root / "data" / "env.sqlite3")
    env["TERM"] = "dumb"
    script = REAL_ROOT / ".venv" / "bin" / "job"
    if not script.exists():  # pragma: no cover - environment dependent
        pytest.skip("installed console script not found")
    added = subprocess.run(
        [str(script), "add", "--company", "Env AG", "--role", "Dev",
         "--submitted-on", "2026-01-05"],
        capture_output=True, text=True, env=env, cwd="/", timeout=60,
    )
    assert added.returncode == 0, added.stderr + added.stdout
    listed = subprocess.run(
        [str(script), "list", "--json"], capture_output=True, text=True, env=env,
        cwd="/", timeout=60,
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout)[0]["company"] == "Env AG"
    assert (project_root / "data" / "env.sqlite3").exists()


def test_launcher_runs_from_any_directory(project_root, tmp_path):
    if shutil.which("uv") is None:  # pragma: no cover - environment dependent
        pytest.skip("uv is not on PATH")
    db = tmp_path / "launcher.sqlite3"
    env = dict(os.environ)
    env["TERM"] = "dumb"
    added = subprocess.run(
        [str(LAUNCHER), "--root", str(project_root), "--db", str(db),
         "add", "--company", "Launcher AG", "--role", "Dev",
         "--submitted-on", "2026-01-05"],
        capture_output=True, text=True, env=env, cwd="/", timeout=180,
    )
    assert added.returncode == 0, added.stderr + added.stdout
    listed = subprocess.run(
        [str(LAUNCHER), "--root", str(project_root), "--db", str(db), "list"],
        capture_output=True, text=True, env=env, cwd="/tmp", timeout=180,
    )
    assert listed.returncode == 0, listed.stderr + listed.stdout
    assert "Launcher AG" in listed.stdout


def test_launcher_reports_a_missing_uv(project_root, tmp_path):
    # a PATH with the shell tools the launcher needs, but without uv
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    for tool in ("sh", "dirname", "readlink"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, empty / tool)
    env = dict(os.environ)
    env["PATH"] = str(empty)
    result = subprocess.run([str(LAUNCHER), "list"], capture_output=True, text=True,
                            env=env, cwd="/", timeout=60)
    assert result.returncode == 127
    assert "uv is not installed" in result.stderr


def test_launcher_forwards_arguments_and_exit_codes(project_root):
    if shutil.which("uv") is None:  # pragma: no cover
        pytest.skip("uv is not on PATH")
    env = dict(os.environ)
    env["TERM"] = "dumb"
    db = project_root / "data" / "exitcode.sqlite3"
    result = subprocess.run(
        [str(LAUNCHER), "--root", str(project_root), "--db", str(db), "show", "missing"],
        capture_output=True, text=True, env=env, cwd="/", timeout=180,
    )
    assert result.returncode == 2
    assert "no application matches" in result.stdout + result.stderr


def test_launcher_quotes_paths_and_exports_root():
    text = BIN_LAUNCHER.read_text()
    assert 'exec uv run --quiet --project "$root" --package job-cli job "$@"' in text
    assert "JOB_ROOT=$root" in text
    assert "readlink" in text  # resolves its own location, even through a symlink
    assert "exit 127" in text  # a missing uv is still reported
    assert os.access(BIN_LAUNCHER, os.X_OK)
    # the root entry is the convenience symlink into bin/ (both must work)
    assert LAUNCHER.is_symlink()
    assert os.path.realpath(LAUNCHER) == os.path.realpath(BIN_LAUNCHER)


def test_no_ddl_outside_migrations_and_no_personal_paths():
    sources = sorted((TOOLING / "src" / "job_cli").glob("*.py"))
    assert sources
    for path in sources:
        text = path.read_text()
        for forbidden in ("CREATE TABLE", "CREATE VIEW", "CREATE TRIGGER", "/home/snobo"):
            assert forbidden not in text, f"{path.name} contains {forbidden!r}"


def test_repository_sql_is_untouched_by_the_tooling():
    migrations = sorted(path.name for path in (REAL_ROOT / "sql" / "migrations").glob("*.sql"))
    assert migrations == ["001_initial_schema.sql", "002_reporting_views.sql",
                          "003_intake_folder.sql"]
    for name in migrations:
        body = (REAL_ROOT / "sql" / "migrations" / name).read_text()
        assert "user_version" in body
    assert not list((REAL_ROOT / "sql" / "migrations").glob("004_*.sql"))
