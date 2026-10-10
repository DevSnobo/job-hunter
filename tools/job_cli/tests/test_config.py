"""The config file of the tracker (``config/job.toml``).

Every value has one precedence rule: CLI flag -> environment variable -> config
file -> built-in default.  A missing file is silent, an invalid one is a usage
error (exit 2) that names the file and the problem, and a relative path inside
the file belongs to the job root, never to the current directory.

All tests build their own temporary root: the real ``config/job.toml`` of the
checkout is never read or written.
"""
from __future__ import annotations

from pathlib import Path

import json

import pytest
from typer.testing import CliRunner

from job_cli.cli import app
from job_cli.errors import DatabaseError
from job_cli.paths import build_layout

CONFIG_RELPATH = Path("config") / "job.toml"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Neither the database nor the config may come from the test runner."""
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.delenv("JOB_CONFIG", raising=False)
    monkeypatch.delenv("JOB_ROOT", raising=False)


def write_config(root: Path, text: str) -> Path:
    path = root / CONFIG_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# a missing file is normal
# --------------------------------------------------------------------------
def test_a_missing_config_file_keeps_every_built_in_default(project_root: Path):
    layout = build_layout(None, str(project_root))
    assert layout.db_path == project_root / "data" / "job.sqlite3"
    assert layout.applications_dir == project_root / "applications"
    assert layout.exports_dir == project_root / "exports"
    assert layout.backups_dir == project_root / "data" / "backups"
    assert layout.data_dir == project_root / "data"
    assert layout.default_language == "de"
    # Silent also means "nothing was created".
    assert not (project_root / "config").exists()


def test_a_missing_config_file_named_by_the_environment_is_silent_too(project_root: Path, monkeypatch):
    monkeypatch.setenv("JOB_CONFIG", str(project_root.parent / "does-not-exist.toml"))
    layout = build_layout(None, str(project_root))
    assert layout.db_path == project_root / "data" / "job.sqlite3"


# --------------------------------------------------------------------------
# the file supplies the values
# --------------------------------------------------------------------------
def test_every_key_comes_from_the_config_file(project_root: Path, tmp_path: Path):
    outside = tmp_path / "absolute-exports"
    write_config(project_root, f'''db = "data/other.sqlite3"
applications_dir = "bewerbungen"
exports_dir = "{outside}"
backups_dir = "data/sicherungen"
default_language = "en"
''')
    layout = build_layout(None, str(project_root))
    assert layout.db_path == project_root / "data" / "other.sqlite3"
    assert layout.applications_dir == project_root / "bewerbungen"
    assert layout.exports_dir == outside
    assert layout.backups_dir == project_root / "data" / "sicherungen"
    assert layout.default_language == "en"


def test_a_relative_path_resolves_against_the_job_root_not_the_cwd(project_root: Path, monkeypatch):
    write_config(project_root, 'db = "data/other.sqlite3"\napplications_dir = "bewerbungen"\n')
    monkeypatch.chdir(project_root.parent)
    layout = build_layout(None, str(project_root))
    assert layout.db_path == project_root / "data" / "other.sqlite3"
    assert layout.applications_dir == project_root / "bewerbungen"


def test_the_config_file_can_be_named_by_the_environment(project_root: Path, tmp_path: Path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text('db = "custom.sqlite3"\n', encoding="utf-8")
    write_config(project_root, 'db = "data/ignored.sqlite3"\n')
    monkeypatch.setenv("JOB_CONFIG", str(elsewhere))
    layout = build_layout(None, str(project_root))
    assert layout.db_path == project_root / "custom.sqlite3"


def test_unknown_keys_are_ignored(project_root: Path):
    write_config(project_root,
                 'applications_dir = "bewerbungen"\n'
                 'zukunft = [1, 2]\n'
                 '[section]\n'
                 'key = "value"\n')
    layout = build_layout(None, str(project_root))
    assert layout.applications_dir == project_root / "bewerbungen"
    assert layout.default_language == "de"


# --------------------------------------------------------------------------
# precedence: flag -> environment -> file -> default
# --------------------------------------------------------------------------
def test_the_flag_and_the_environment_beat_the_config_file(project_root: Path, tmp_path: Path, monkeypatch):
    write_config(project_root, 'db = "data/from-file.sqlite3"\n')
    from_file = build_layout(None, str(project_root))
    assert from_file.db_path == project_root / "data" / "from-file.sqlite3"

    monkeypatch.setenv("JOB_DB", str(tmp_path / "from-env.sqlite3"))
    from_env = build_layout(None, str(project_root))
    assert from_env.db_path == tmp_path / "from-env.sqlite3"

    from_flag = build_layout(str(tmp_path / "from-flag.sqlite3"), str(project_root))
    assert from_flag.db_path == tmp_path / "from-flag.sqlite3"


# --------------------------------------------------------------------------
# an invalid file is a usage error, never a silent fallback
# --------------------------------------------------------------------------
def test_invalid_toml_is_a_usage_error_that_names_the_file(project_root: Path):
    path = write_config(project_root, 'db = "unterminated\n')
    with pytest.raises(DatabaseError) as exc:
        build_layout(None, str(project_root))
    message = str(exc.value)
    assert str(path) in message
    assert "TOML" in message


def test_a_wrong_value_type_is_a_usage_error_that_names_the_key(project_root: Path):
    path = write_config(project_root, 'db = 5\n')
    with pytest.raises(DatabaseError) as exc:
        build_layout(None, str(project_root))
    message = str(exc.value)
    assert str(path) in message
    assert "db must be a string" in message


def test_an_empty_path_value_is_a_usage_error(project_root: Path):
    path = write_config(project_root, 'applications_dir = ""\n')
    with pytest.raises(DatabaseError) as exc:
        build_layout(None, str(project_root))
    assert str(path) in str(exc.value)
    assert "applications_dir" in str(exc.value)


def test_unreadable_config_path_is_a_usage_error(project_root: Path):
    # A directory where the file is expected cannot be read as a config file;
    # the error must name the path instead of pretending there is no config.
    path = project_root / CONFIG_RELPATH
    path.mkdir(parents=True)
    with pytest.raises(DatabaseError) as exc:
        build_layout(None, str(project_root))
    assert str(path) in str(exc.value)


# --------------------------------------------------------------------------
# the CLI: exit 2, and nothing else runs
# --------------------------------------------------------------------------
def test_the_cli_exits_2_on_an_invalid_config_and_runs_nothing_else(run, project_root: Path):
    path = write_config(project_root, 'db = "unterminated\n')
    result = run("list", "--json")
    assert result.exit_code == 2
    assert "error:" in result.output
    assert str(path) in result.output
    # The command itself never ran: no table, no JSON, no database touch.
    assert "[]" not in result.output
    assert not (project_root / "data").exists()


def test_an_invalid_config_file_fails_even_when_the_flag_supplies_the_value(run, project_root: Path):
    write_config(project_root, 'applications_dir = 5\n')
    result = run("list")  # the fixture passes --db explicitly
    assert result.exit_code == 2
    assert "applications_dir must be a string" in result.output


def test_the_cli_uses_the_database_of_the_config_file(runner: CliRunner, project_root: Path):
    write_config(project_root, 'db = "data/other.sqlite3"\n')
    result = runner.invoke(app, ["--root", str(project_root), "list", "--json"],
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert (project_root / "data" / "other.sqlite3").exists()
    assert not (project_root / "data" / "job.sqlite3").exists()


def test_the_cli_uses_the_application_directory_of_the_config_file(run, project_root: Path):
    write_config(project_root, 'applications_dir = "bewerbungen"\n')
    folder = project_root / "bewerbungen" / "Aus Konfig"
    folder.mkdir(parents=True)
    (folder / "posting.txt").write_text("Text\n", encoding="utf-8")
    (folder / "source.txt").write_text("https://example.com/x\n", encoding="utf-8")
    result = run("intake", "--json")
    assert result.exit_code == 0, result.output
    assert [row["folder"] for row in json.loads(result.output)] == ["Aus Konfig"]
    assert not (project_root / "applications").exists()


def test_default_language_is_loaded_but_changes_no_output(run, project_root: Path):
    before = run("list", "--json").output
    write_config(project_root, 'default_language = "en"\n')
    after = run("list", "--json").output
    assert before == after
    assert build_layout(None, str(project_root)).default_language == "en"
