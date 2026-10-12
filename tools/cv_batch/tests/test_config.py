"""The cv.toml configuration layer: precedence, defaults and failure modes."""

from __future__ import annotations

import json

import pytest

from cv_batch.cli import main
from cv_batch.config import (
    CONFIG_AGENT_COMMAND,
    CONFIG_AGENT_TABLE,
    DEFAULT_LAYOUT,
    DEFAULT_MODEL,
    DEFAULT_TARGET_TEMPLATE,
    Config,
    ConfigError,
    TargetNaming,
    agent_command_from_env,
    build_paths,
    config_path,
    load_config,
    name_slug,
)

from conftest import target_name
from test_cli import base_args


def write_config(root, body: str, name: str = "cv.toml"):
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Loading: optional, silent when missing, a usage error when broken.
# ---------------------------------------------------------------------------
def test_a_missing_config_file_is_silent_and_gives_the_built_in_defaults(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    config = load_config(root)
    assert not config.exists
    assert config.text("model", DEFAULT_MODEL) == DEFAULT_MODEL
    assert config.text("generator_template", DEFAULT_LAYOUT) == DEFAULT_LAYOUT
    paths = build_paths(job_root=root)
    assert paths.applications == root / "applications"
    assert paths.profile_dir == root / "profile"
    assert paths.state_dir == root / "state"
    assert paths.generator_project == root / "tools" / "cv_generator"
    assert paths.backlog_de == root / "profile" / "backlogs" / "career-knowledge-backlog-DE.md"


def test_config_file_is_found_in_config_below_the_job_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    path = write_config(root, 'model = "auto"\n')
    assert config_path(root) == path
    assert load_config(root).text("model") == "auto"


def test_config_file_location_precedence(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    default = write_config(root, 'model = "auto"\n')
    elsewhere = tmp_path / "other" / "cv.toml"
    elsewhere.parent.mkdir()
    elsewhere.write_text('model = "astra"\n', encoding="utf-8")

    assert config_path(root) == default
    monkeypatch.setenv("CV_CONFIG", str(elsewhere))
    assert config_path(root) == elsewhere          # the environment beats the default
    assert load_config(root).text("model") == "astra"
    assert load_config(root, elsewhere).text("model") == "astra"
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('model = "deepseek"\n', encoding="utf-8")
    assert config_path(root, explicit) == explicit  # --config beats the environment
    assert load_config(root, explicit).text("model") == "deepseek"


def test_invalid_toml_is_an_error_naming_the_file(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    path = write_config(root, "model = \n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(root)
    assert str(path) in str(excinfo.value)
    assert "not valid TOML" in str(excinfo.value)


def test_wrong_value_type_is_an_error_naming_the_key(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    write_config(root, 'target_template = 5\n')
    config = load_config(root)
    with pytest.raises(ConfigError, match="target_template must be a string"):
        config.text("target_template", DEFAULT_TARGET_TEMPLATE)
    write_config(root, 'languages = "de"\n')
    with pytest.raises(ConfigError, match="must be a list of strings"):
        load_config(root).text_list("languages")
    write_config(root, "[limits]\nmodel_timeout = \"soon\"\n")
    with pytest.raises(ConfigError, match=r"\[limits\] model_timeout must be an integer"):
        load_config(root).integer("model_timeout", 240, section="limits")


def test_the_agent_command_comes_from_the_config_or_the_environment(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    write_config(root, '[agent]\ncommand = ["my-agent", "--go", "{prompt}"]\n')
    config = load_config(root)
    assert config.string_list(CONFIG_AGENT_COMMAND, section=CONFIG_AGENT_TABLE) == (
        "my-agent",
        "--go",
        "{prompt}",
    )
    assert load_config(root).string_list("command", section="agent") == ("my-agent", "--go", "{prompt}")

    write_config(root, "[agent]\ncommand = 5\n")
    with pytest.raises(ConfigError, match=r"\[agent\] command must be a list of strings"):
        load_config(root).string_list("command", section="agent")

    monkeypatch.setenv("CV_AGENT_COMMAND", 'claude -p "{prompt}"')
    assert agent_command_from_env() == ["claude", "-p", "{prompt}"]
    monkeypatch.setenv("CV_AGENT_COMMAND", "   ")
    assert agent_command_from_env() is None


def test_a_missing_agent_command_is_absent(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    assert load_config(root).string_list("command", section="agent") is None
    assert agent_command_from_env() is None


def test_a_malformed_agent_command_env_value_is_a_usage_error(monkeypatch):
    monkeypatch.setenv("CV_AGENT_COMMAND", "unbalanced 'quote")
    with pytest.raises(ConfigError, match="CV_AGENT_COMMAND"):
        agent_command_from_env()


def test_unknown_keys_are_ignored(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    write_config(root, 'future_option = "x"\n[future_table]\nkey = 1\n')
    assert load_config(root).text("model", DEFAULT_MODEL) == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Paths: config file in the middle, CLI flag on top, defaults below.
# ---------------------------------------------------------------------------
def test_paths_come_from_the_config_file_and_resolve_against_the_root(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    write_config(
        root,
        "[paths]\n"
        'profile_dir = "private/profile"\n'
        'applications_dir = "apps"\n'
        'state_dir = "run/state"\n'
        'backlog_de = "private/backlog-de.md"\n'
        'backlog_en = "private/backlog-en.md"\n',
    )
    paths = build_paths(job_root=root)
    assert paths.profile_dir == root / "private" / "profile"
    assert paths.applications == root / "apps"
    assert paths.state_dir == root / "run" / "state"
    assert paths.backlog_de == root / "private" / "backlog-de.md"


def test_absolute_paths_in_the_config_file_are_used_as_given(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    absolute = tmp_path / "somewhere" / "backlog.md"
    write_config(root, f'[paths]\nbacklog_de = "{absolute}"\n')
    assert build_paths(job_root=root).backlog_de == absolute


def test_explicit_arguments_beat_the_config_file(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    write_config(root, '[paths]\napplications_dir = "apps"\nprofile_dir = "p"\n')
    paths = build_paths(
        job_root=root, applications=tmp_path / "explicit-apps", profile_dir=tmp_path / "explicit-profile"
    )
    assert paths.applications == (tmp_path / "explicit-apps").resolve()
    assert paths.profile_dir == (tmp_path / "explicit-profile").resolve()


def test_generator_project_can_be_moved_by_the_config_file(tmp_path):
    root = tmp_path / "jobs"
    (root / "vendor" / "gen").mkdir(parents=True)
    write_config(root, 'generator_project = "vendor/gen"\n')
    assert build_paths(job_root=root).generator_project == root / "vendor" / "gen"


# ---------------------------------------------------------------------------
# CLI: config values reach the run, CLI flags win, a broken file exits 2.
# ---------------------------------------------------------------------------
def test_cli_uses_the_config_target_template_for_the_planned_names(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, 'target_template = "cv-{language}-{name}-{folder}.pdf"\n')
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "cv-DE-AdaLovelace-Alpha.pdf" in out


def test_cli_config_template_with_an_unknown_field_exits_2(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, 'target_template = "cv-{surname}-{folder}.pdf"\n')
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])
    assert code == 2
    assert "cannot be filled in" in capsys.readouterr().err


def test_cli_invalid_config_exits_2_and_runs_nothing(project, fake_agent, fake_renderer, capsys):
    path = write_config(project.root, "model = \n")
    project.add_app("Alpha")
    fake_agent.steps([])
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 2
    assert str(path) in captured.err
    assert "not valid TOML" in captured.err
    assert not target_name("Alpha", "de") in captured.out
    assert fake_agent.calls() == []


def test_cli_explicit_config_flag_wins(tmp_path, project, fake_agent, fake_renderer, capsys):
    other = tmp_path / "elsewhere.toml"
    other.write_text('target_template = "chosen-{folder}.pdf"\n', encoding="utf-8")
    write_config(project.root, 'target_template = "ignored-{folder}.pdf"\n')
    project.add_app("Alpha")
    code = main([
        *base_args(project, fake_agent, fake_renderer),
        "--config", str(other),
        "--language", "de", "--all", "--dry-run",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "chosen-Alpha.pdf" in out and "ignored-Alpha.pdf" not in out


def test_cli_language_is_restricted_to_the_configured_languages(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, 'languages = ["de"]\n')
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "en", "--all", "--dry-run"])
    assert code == 2
    assert "--language accepts only 'de'" in capsys.readouterr().err


def test_cli_limits_come_from_the_config_file(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, "[limits]\nbullets_per_entry = 99\n")
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])
    assert code == 2
    assert "--max-bullets-per-entry must be an integer between 1 and 50" in capsys.readouterr().err


def test_cli_a_command_agent_replaces_the_model_plan(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, '[agent]\ncommand = ["claude", "-p", "{prompt}"]\n')
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Agent          : claude -p '{prompt}'" in out
    assert "Model          :" not in out
    assert "Session mode" not in out
    assert "Parallel jobs  : 1" in out


def test_cli_agent_command_flag_beats_the_config_file(project, fake_agent, fake_renderer, capsys):
    write_config(project.root, '[agent]\ncommand = ["ignored", "{prompt}"]\n')
    project.add_app("Alpha")
    code = main([
        *base_args(project, fake_agent, fake_renderer),
        "--agent-command", "chosen {prompt}",
        "--language", "de", "--all", "--dry-run",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Agent          : chosen '{prompt}'" in out
    assert "Agent          : ignored" not in out


def test_cli_agent_command_needs_the_prompt_placeholder(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    code = main([
        *base_args(project, fake_agent, fake_renderer),
        "--agent-command", "claude -p",
        "--language", "de", "--all", "--dry-run",
    ])
    assert code == 2
    assert "{prompt}" in capsys.readouterr().err


def test_cv_toml_and_the_example_stay_in_step(project):
    """The private file must not silently differ from the shipped example."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    real = repo / "config" / "cv.toml"
    example = repo / "config" / "cv.toml.example"
    if not real.is_file():
        pytest.skip("the private config/cv.toml is not present in this checkout")
    assert real.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_target_naming_helper_matches_the_tool_default():
    naming = TargetNaming(name=name_slug("Ada", "Lovelace"))
    assert naming.filename("Alpha", "de") == target_name("Alpha", "de")
    assert naming.template == DEFAULT_TARGET_TEMPLATE
