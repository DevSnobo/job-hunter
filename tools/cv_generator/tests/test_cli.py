"""CLI behaviour tests (invoke the real console script via subprocess).

Two templates ship with the project (``classic`` and ``minimal``), so a render
without ``--layout`` needs a configuration file that names the active one.
Every subprocess run here gets ``CVGEN_CONFIG`` pointed at a temporary
``cvgen.toml`` for that reason - the repository's own ``config/cvgen.toml`` is
private and must never decide what these tests see.
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cv_generator.paths import DEFAULT_OUTPUT_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# One shared workspace venv: the interpreter that runs this suite is the right
# one for the subprocess, there is no per-project .venv any more.
CLI_PYTHON = Path(sys.executable)
CV_OUTPUTS = DEFAULT_OUTPUT_DIR


def _classic_config() -> Path:
    """A temporary ``cvgen.toml`` naming the shipped ``classic`` template.

    Two templates ship with the project, so a render without ``--layout`` needs
    a config that names the active one; passing this file keeps the tests
    independent of the repository's own (private) ``config/cvgen.toml``.
    """
    directory = Path(tempfile.mkdtemp(prefix="cvgen-test-config-"))
    atexit.register(shutil.rmtree, directory, True)
    path = directory / "cvgen.toml"
    path.write_text('[template]\nname = "classic"\n', encoding="utf-8")
    return path


CLASSIC_CONFIG = _classic_config()


def run_cli(args, cwd, config=CLASSIC_CONFIG):
    """Run the CLI in a subprocess; ``config=None`` means "no config file at all"."""
    cmd = [str(CLI_PYTHON), "-m", "cv_generator.cli", *args]
    env = dict(os.environ)
    if config is not None:
        env["CVGEN_CONFIG"] = str(config)
    else:
        env.pop("CVGEN_CONFIG", None)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)


def write_input(tmp_path: Path, data: dict, name: str = "input.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def minimal_cv() -> dict:
    return {
        "schema_version": 1,
        "language": "en",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "headline": "Software Developer",
        "photo": "placeholder_photo.jpg",
        "contacts": [{"kind": "email", "lines": ["a@b.c"]}],
        "languages": [{"name": "German", "level": "Native"}],
        "skills": [{"label": "L:", "segments": [{"text": "Java", "bold": True}]}],
        "experience": [{"role": "Developer", "meta": "Org | 2020 - 2021", "bullets": ["Did things."]}],
        "education": [{"title": "Uni | B.Sc."}],
        "certificates": [],
        "signature": None,
    }


def test_cli_works_from_outside_project(tmp_path):
    inp = write_input(tmp_path, minimal_cv(), name="cli_ok.json")
    out = tmp_path / "out.pdf"
    r = run_cli(["render", str(inp), "--output", str(out), "--allow-outside-output"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert out.exists()


def test_default_output_goes_to_the_exports_dir(tmp_path):
    inp = write_input(tmp_path, minimal_cv(), name="cli_default_xyz.json")
    expected = CV_OUTPUTS / "cli_default_xyz.pdf"
    try:
        r = run_cli(["render", str(inp), "--overwrite"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert expected.exists()
    finally:
        if expected.exists():
            expected.unlink()


def test_existing_output_refused_without_overwrite(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "out.pdf"
    out.write_bytes(b"existing")
    r = run_cli(["render", str(inp), "--output", str(out), "--allow-outside-output"], cwd=tmp_path)
    assert r.returncode != 0
    assert out.read_bytes() == b"existing"  # untouched


def test_overwrite_replaces(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "out.pdf"
    out.write_bytes(b"existing")
    r = run_cli(["render", str(inp), "--output", str(out), "--overwrite", "--allow-outside-output"], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() != b"existing"


def test_malformed_input_fails_cleanly(tmp_path):
    inp = tmp_path / "bad.json"
    inp.write_text("{not json", encoding="utf-8")
    out = tmp_path / "out.pdf"
    r = run_cli(["render", str(inp), "--output", str(out)], cwd=tmp_path)
    assert r.returncode != 0
    assert not out.exists()


def test_overflow_fails_without_writing(tmp_path):
    data = minimal_cv()
    data["experience"][0]["bullets"] = [
        "A much longer bullet that wraps over several lines of the experience section."
        for _ in range(20)
    ]
    inp = write_input(tmp_path, data)
    out = tmp_path / "out.pdf"
    r = run_cli(["render", str(inp), "--output", str(out)], cwd=tmp_path)
    assert r.returncode != 0
    assert "overflow" in r.stderr.lower()
    assert not out.exists()


def test_missing_photo_fails_cleanly(tmp_path):
    data = minimal_cv()
    data["photo"] = "nope.jpg"
    inp = write_input(tmp_path, data)
    out = tmp_path / "out.pdf"
    r = run_cli(["render", str(inp), "--output", str(out)], cwd=tmp_path)
    assert r.returncode != 0
    assert not out.exists()


def test_default_output_name_is_safe_from_input_dirs(tmp_path):
    # input living in a nested path with dotfiles still yields a bare stem name
    nested = tmp_path / ".."  # resolve to a harmless parent dir
    inp = tmp_path / "a.b.c.json"
    inp.write_text(json.dumps(minimal_cv(), ensure_ascii=False), encoding="utf-8")
    expected = CV_OUTPUTS / "a.b.c.pdf"
    try:
        r = run_cli(["render", str(inp), "--overwrite"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert expected.exists()
    finally:
        if expected.exists():
            expected.unlink()


def test_output_escape_rejected(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    escaped = tmp_path / "escaped_xyz.pdf"
    # '../generator/../<name>' resolves next to the output directory, outside it
    rel = f"../{CV_OUTPUTS.name}/../{escaped.name}"
    r = run_cli(["render", str(inp), "--output", rel, "--overwrite"], cwd=tmp_path)
    assert r.returncode != 0
    assert "escapes the output directory" in r.stderr
    assert not escaped.exists()


def test_absolute_output_outside_rejected_without_flag(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "out_abs.pdf"
    r = run_cli(["render", str(inp), "--output", str(out), "--overwrite"], cwd=tmp_path)
    assert r.returncode != 0
    assert "escapes the output directory" in r.stderr
    assert not out.exists()


def test_allow_outside_output_flag(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "out_opt.pdf"
    r = run_cli(
        ["render", str(inp), "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()


def test_relative_output_name_stays_in_the_output_dir(tmp_path):
    inp = write_input(tmp_path, minimal_cv(), name="rel_out.json")
    target = CV_OUTPUTS / "rel_out.pdf"
    try:
        r = run_cli(["render", str(inp), "--output", "rel_out.pdf", "--overwrite"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert target.exists()
    finally:
        if target.exists():
            target.unlink()


def test_missing_input_file_clean_error(tmp_path):
    missing = tmp_path / "definitely_missing.json"
    r = run_cli(["render", str(missing), "--overwrite"], cwd=tmp_path)
    assert r.returncode != 0
    assert "input file not found" in r.stderr
    assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# Input resolution (--language shortcut, cvgen.toml input) and config errors.
# ---------------------------------------------------------------------------
def test_language_shortcut_uses_the_profile_input(tmp_path):
    profile = tmp_path / "profile"
    (profile / "inputs").mkdir(parents=True)
    # No photo: these tests are about input resolution, not about the profile's
    # picture files.
    (profile / "inputs" / "current_de.json").write_text(
        json.dumps({**minimal_cv(), "photo": None}, ensure_ascii=False), encoding="utf-8"
    )
    config = tmp_path / "cvgen.toml"
    config.write_text(
        f'profile_dir = "{profile}"\n\n[template]\nname = "classic"\n', encoding="utf-8"
    )
    out = tmp_path / "lang.pdf"
    r = run_cli(
        ["render", "--language", "de", "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
        config=config,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()
    # The same run with a language that has no input file fails, naming it.
    r = run_cli(
        ["render", "--language", "en", "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
        config=config,
    )
    assert r.returncode == 2
    assert "current_en.json" in r.stderr


def test_input_from_the_config_file(tmp_path):
    data = tmp_path / "profile" / "inputs"
    data.mkdir(parents=True)
    (data / "current_de.json").write_text(
        json.dumps({**minimal_cv(), "photo": None}, ensure_ascii=False), encoding="utf-8"
    )
    config = tmp_path / "cvgen.toml"
    config.write_text(
        f'profile_dir = "{tmp_path / "profile"}"\ninput = "inputs/current_de.json"\n\n'
        '[template]\nname = "classic"\n',
        encoding="utf-8",
    )
    out = tmp_path / "cfg.pdf"
    r = run_cli(
        ["render", "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
        config=config,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()


def test_no_input_anywhere_is_a_usage_error(tmp_path):
    config = tmp_path / "cvgen.toml"
    config.write_text('[template]\nname = "classic"\n', encoding="utf-8")
    r = run_cli(["render"], cwd=tmp_path, config=config)
    assert r.returncode == 2
    assert "no input given" in r.stderr
    assert "--language" in r.stderr


def test_invalid_config_file_is_a_usage_error_naming_the_file(tmp_path):
    config = tmp_path / "cvgen.toml"
    config.write_text("[template\nname = 'classic'\n", encoding="utf-8")
    r = run_cli(["layouts"], cwd=tmp_path, config=config)
    assert r.returncode == 2
    assert str(config) in r.stderr
    assert "not valid TOML" in r.stderr
    assert "Traceback" not in r.stderr


def test_without_any_config_file_the_ambiguity_is_reported(tmp_path):
    r = run_cli(["layouts"], cwd=tmp_path, config=None)
    assert r.returncode == 0  # listing works without a config
    assert "classic" in r.stdout and "minimal" in r.stdout
