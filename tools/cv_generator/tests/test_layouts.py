"""Pluggable layout interface: templates, registry, discovery, and CLI selection.

These tests use tiny fake layouts that return deterministic bytes; no second
real design is needed to prove that the indirection works.  The two shipped
templates (``classic``, ``minimal``) are real and are used where the discovery
and selection rules need real directories.
"""

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from cv_generator import CVData, LayoutError, render_layout
from cv_generator import paths as paths_mod
from cv_generator.cli import main, select_layout
from cv_generator.layouts import (
    DEFAULT_LAYOUT,
    ENTRY_POINT_GROUP,
    LayoutOptions,
    LayoutRegistrationError,
    UnknownLayoutError,
    _REGISTRY,
    available_layouts,
    discover_layouts,
    discover_templates,
    discovery_warnings,
    get_layout,
    get_layout_info,
    list_templates,
    register_layout,
    reset_layouts,
    template_search_dirs,
    unregister_layout,
)
from cv_generator.paths import PROFILE_PIC_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# One shared workspace venv: use the interpreter that runs this suite.
CLI_PYTHON = Path(sys.executable)

TEST_PAYLOAD = b"%PDF-1.4\n% fake layout bytes\n"

SHIPPED_TEMPLATES = ("classic", "minimal")


def write_config(directory: Path, text: str = '[template]\nname = "classic"\n') -> Path:
    """Write a ``cvgen.toml`` for one test run (it is private, so never the repo's)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cvgen.toml"
    path.write_text(text, encoding="utf-8")
    return path


def make_template(directory: Path, name: str, *, with_layout_code: bool = True, description=None):
    """Create a template directory with its own ``layout.py`` and ``template.toml``."""
    template_dir = directory / name
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "template.toml").write_text(
        f'name = "{name}"\ndescription = "{description or name}"\n', encoding="utf-8"
    )
    if with_layout_code:
        (template_dir / "layout.py").write_text(
            "from cv_generator.layouts import LayoutOptions, register_layout\n"
            "\n"
            "\n"
            "class ForeignLayout:\n"
            f"    name = {name!r}\n"
            f"    description = {description or name!r}\n"
            "\n"
            "    def render(self, cv, options):\n"
            f"        return {TEST_PAYLOAD!r}\n"
            "\n"
            "\n"
            "LAYOUT = ForeignLayout()\n"
            'register_layout(LAYOUT, origin=f"template {LAYOUT.name}")\n',
            encoding="utf-8",
        )
    return template_dir


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


class FixedLayout:
    """Minimal layout: records its call and returns a fixed payload."""

    def __init__(self, name="fixed", description="Deterministic test layout.", payload=TEST_PAYLOAD):
        self.name = name
        self.description = description
        self.payload = payload
        self.calls = []

    def render(self, cv, options):
        self.calls.append((cv, options))
        return self.payload


class FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint (name + load + dist)."""

    def __init__(self, name, obj=None, error=None, distribution="cv-layouts-demo"):
        self.name = name
        self._obj = obj
        self._error = error
        self.dist = types.SimpleNamespace(name=distribution)

    def load(self):
        if self._error is not None:
            raise self._error
        return self._obj


def fake_entry_points(entry_points, *, raise_error=None):
    """Replacement for ``importlib.metadata.entry_points(group=...)``."""

    def _entry_points(*, group=None, **kwargs):
        if raise_error is not None:
            raise raise_error
        return list(entry_points)

    return _entry_points


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with only the shipped templates registered."""
    reset_layouts()
    yield
    reset_layouts()


@pytest.fixture(autouse=True)
def restore_paths_module():
    """``--config``/``--profile-dir`` mutate the paths module; undo that per test."""
    saved = {
        name: getattr(paths_mod, name)
        for name in (
            "CONFIG",
            "CONFIG_ERROR",
            "PROFILE_DIR",
            "PROFILE_PIC_DIR",
            "INPUTS_DIR",
            "DEFAULT_OUTPUT_DIR",
            "TEMPLATES_DIR",
        )
    }
    yield
    for name, value in saved.items():
        setattr(paths_mod, name, value)


@pytest.fixture
def single_template_dir(tmp_path, monkeypatch):
    """Only one template in scope: the shipped directory is replaced for this test."""
    directory = tmp_path / "templates"
    make_template(directory, "solo", description="The only template.")
    monkeypatch.setattr(paths_mod, "TEMPLATES_DIR", directory)
    reset_layouts()
    return directory


def run_cli(args, cwd, config=None):
    cmd = [str(CLI_PYTHON), "-m", "cv_generator.cli", *args]
    env = dict(os.environ)
    if config is not None:
        env["CVGEN_CONFIG"] = str(config)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)


def write_input(tmp_path: Path, data: dict, name: str = "layout_input.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return re.sub(r"\s+", " ", "\n".join(page.extract_text() or "" for page in reader.pages))


# ---------------------------------------------------------------------------
# Registry semantics.
# ---------------------------------------------------------------------------
def test_shipped_templates_are_registered():
    infos = {info.name: info for info in available_layouts()}
    assert set(SHIPPED_TEMPLATES) <= set(infos)
    classic = infos["classic"]
    assert classic.origin == f"template {DEFAULT_LAYOUT}"
    assert classic.entry_point is None
    assert classic.path == paths_mod.TEMPLATES_DIR / "classic"
    assert "reference" in classic.description.lower()
    assert infos["minimal"].path == paths_mod.TEMPLATES_DIR / "minimal"


def test_list_templates_reports_name_description_origin_and_path():
    templates = {entry[0]: entry for entry in list_templates()}
    assert set(SHIPPED_TEMPLATES) <= set(templates)
    name, description, origin, path = templates["classic"]
    assert name == "classic"
    assert description
    assert origin == "template classic"
    assert path == paths_mod.TEMPLATES_DIR / "classic"
    # A layout that is not a template (a distribution entry point) is not listed.
    register_layout(FixedLayout("manual-layout"))
    assert "manual-layout" not in {entry[0] for entry in list_templates()}


def test_available_layouts_is_sorted_and_without_duplicates():
    register_layout(FixedLayout("zeta-layout"))
    register_layout(FixedLayout("alpha-layout"))
    names = [info.name for info in available_layouts()]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert {"alpha-layout", "classic", "zeta-layout"} <= set(names)


def test_duplicate_name_is_rejected_but_override_replaces():
    first = FixedLayout("dup", description="first")
    second = FixedLayout("dup", description="second")
    register_layout(first)
    with pytest.raises(LayoutRegistrationError, match="already registered"):
        register_layout(second)
    assert get_layout("dup") is first

    register_layout(second, override=True)
    assert get_layout("dup") is second


@pytest.mark.parametrize(
    "layout, message",
    [
        (types.SimpleNamespace(description="x", render=lambda cv, options: b""), "name"),
        (types.SimpleNamespace(name="bad name", description="x", render=lambda cv, options: b""), "invalid"),
        (types.SimpleNamespace(name="no-desc", render=lambda cv, options: b""), "description"),
        (types.SimpleNamespace(name="no-render", description="x"), "render"),
    ],
)
def test_invalid_layout_registration_raises(layout, message):
    with pytest.raises(LayoutRegistrationError, match=message):
        register_layout(layout)


def test_unregister_layout():
    register_layout(FixedLayout("temporary"))
    assert unregister_layout("temporary").name == "temporary"
    with pytest.raises(UnknownLayoutError):
        get_layout("temporary")
    assert unregister_layout("temporary", missing_ok=True) is None


def test_unknown_layout_error_lists_known_names():
    with pytest.raises(UnknownLayoutError) as excinfo:
        get_layout("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
    assert "classic" in str(excinfo.value)
    assert excinfo.value.known == sorted(SHIPPED_TEMPLATES)


def test_get_layout_returns_the_installed_object():
    layout = FixedLayout("direct")
    register_layout(layout)
    assert get_layout("direct") is layout


def test_render_layout_rejects_non_bytes_result():
    register_layout(FixedLayout("broken-payload", payload="not bytes"))
    with pytest.raises(LayoutError, match="expected PDF bytes"):
        render_layout("broken-payload", CVData.model_validate(minimal_cv()))


def test_render_layout_passes_options_through():
    layout = FixedLayout("options-probe")
    register_layout(layout)
    cv = CVData.model_validate(minimal_cv())
    options = LayoutOptions(photo_path="custom.jpg")
    render_layout("options-probe", cv, options)
    assert layout.calls == [(cv, options)]
    render_layout("options-probe", cv)  # default options
    assert layout.calls[-1][1] == LayoutOptions()


def test_layout_options_resolve_photo_and_crop(tmp_path, monkeypatch):
    """A relative photo resolves in the profile, else in the shipped fixtures."""
    cv = CVData.model_validate(minimal_cv())
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "mine.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(paths_mod, "PROFILE_PIC_DIR", photos)

    # the private profile always wins when it holds the named file
    assert LayoutOptions(photo_path="mine.jpg").photo_for(cv) == (photos / "mine.jpg", None)
    # the shipped fixtures name a placeholder, so a clone without profile/ works
    path, crop = LayoutOptions().photo_for(cv)
    assert path == paths_mod.SHIPPED_FIXTURES_DIR / "placeholder_photo.jpg"
    assert crop is None

    override = LayoutOptions(photo_path="/tmp/other.jpg")
    assert override.photo_for(cv) == (Path("/tmp/other.jpg"), None)


def test_no_photo_gives_no_reference():
    cv = CVData.model_validate({**minimal_cv(), "photo": None})
    assert LayoutOptions().photo_for(cv) == (None, None)


# ---------------------------------------------------------------------------
# Entry-point discovery.
# ---------------------------------------------------------------------------
def test_entry_point_layout_is_discovered(monkeypatch):
    entry_point = FakeEntryPoint("custom", FixedLayout("custom"))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([entry_point]))

    assert discover_layouts() == ["custom"]
    info = get_layout_info("custom")
    assert info.origin == "distribution cv-layouts-demo (entry point 'custom')"
    assert info.entry_point == f"{ENTRY_POINT_GROUP}:custom"
    assert [item.name for item in available_layouts()] == ["classic", "custom", "minimal"]


def test_entry_point_may_be_a_zero_argument_factory(monkeypatch):
    factory = lambda: FixedLayout("via-factory")
    monkeypatch.setattr(
        importlib.metadata, "entry_points", fake_entry_points([FakeEntryPoint("via-factory", factory)])
    )
    discover_layouts()
    assert get_layout("via-factory").name == "via-factory"


def test_entry_point_may_be_a_layout_class(monkeypatch):
    class ClassLayout:
        name = "via-class"
        description = "Layout provided as a class."

        def render(self, cv, options):
            return TEST_PAYLOAD

    monkeypatch.setattr(
        importlib.metadata, "entry_points", fake_entry_points([FakeEntryPoint("via-class", ClassLayout)])
    )
    discover_layouts()
    assert get_layout("via-class").description == "Layout provided as a class."
    assert render_layout("via-class", CVData.model_validate(minimal_cv())) == TEST_PAYLOAD


def test_discovery_is_idempotent(monkeypatch):
    entry_point = FakeEntryPoint("once", FixedLayout("once"))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([entry_point]))
    assert discover_layouts() == ["once"]
    assert discover_layouts() == []  # already scanned
    assert get_layout("once").name == "once"
    # A forced rescan re-reads the same entry point, which then conflicts with
    # the registration it produced -> skipped with a warning, no crash.
    with pytest.warns(RuntimeWarning):
        assert discover_layouts(force=True) == []
    assert any("already registered" in message for message in discovery_warnings())


def test_broken_entry_point_is_skipped_with_a_warning(monkeypatch):
    broken = FakeEntryPoint("broken", error=ImportError("no module named 'nope'"))
    good = FakeEntryPoint("working", FixedLayout("working"), distribution="other-pkg")
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([broken, good]))

    with pytest.warns(RuntimeWarning):
        assert discover_layouts() == ["working"]  # skipped, not raised
    assert get_layout("working").name == "working"
    warnings_seen = discovery_warnings()
    assert len(warnings_seen) == 1
    assert "broken" in warnings_seen[0] and "no module named" in warnings_seen[0]
    assert [item.name for item in available_layouts()] == ["classic", "minimal", "working"]


def test_entry_point_with_invalid_layout_is_skipped(monkeypatch):
    invalid = FakeEntryPoint("invalid", types.SimpleNamespace(name="", description=""))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([invalid]))
    with pytest.warns(RuntimeWarning):
        assert discover_layouts() == []
    assert discovery_warnings()


def test_entry_point_claiming_a_template_name_is_skipped(monkeypatch):
    clash = FakeEntryPoint("classic", FixedLayout("classic", description="impostor"))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([clash]))
    with pytest.warns(RuntimeWarning):
        discover_layouts()
    info = get_layout_info("classic")
    assert info.origin == "template classic"
    assert info.description != "impostor"
    assert any("classic" in message for message in discovery_warnings())


def test_discovery_failure_of_the_api_itself_is_reported(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata, "entry_points", fake_entry_points([], raise_error=RuntimeError("metadata boom"))
    )
    with pytest.warns(RuntimeWarning):
        assert discover_layouts() == []
    assert get_layout("classic").name == "classic"  # templates still work
    assert any("metadata boom" in message for message in discovery_warnings())


# ---------------------------------------------------------------------------
# CLI: selection, listing, and a real end-to-end render with a fake layout.
# ---------------------------------------------------------------------------
def test_cli_unknown_layout_is_a_clean_usage_error(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "out.pdf"
    r = run_cli(
        ["render", str(inp), "--layout", "nope", "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
    )
    assert r.returncode == 2
    assert "unknown layout 'nope'" in r.stderr
    assert "classic" in r.stderr  # lists the known names
    assert "Traceback" not in r.stderr
    assert not out.exists()


def test_cli_explicit_layout_matches_the_configured_default(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    config = write_config(tmp_path, '[template]\nname = "classic"\n')
    default_out = tmp_path / "default.pdf"
    explicit_out = tmp_path / "explicit.pdf"
    r1 = run_cli(["render", str(inp), "--output", str(default_out), "--overwrite", "--allow-outside-output"], cwd=tmp_path, config=config)
    r2 = run_cli(
        ["render", str(inp), "--layout", "classic", "--output", str(explicit_out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
        config=config,
    )
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert pdf_text(default_out.read_bytes()) == pdf_text(explicit_out.read_bytes())
    assert "EXPERIENCE" in pdf_text(explicit_out.read_bytes())


def test_single_template_is_selected_automatically(single_template_dir, tmp_path, capsys):
    assert [name for name, *_rest in list_templates()] == ["solo"]
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "solo.pdf"
    rc = main(["render", str(inp), "--output", str(out), "--overwrite", "--allow-outside-output"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert out.read_bytes() == TEST_PAYLOAD  # the only template was used


def test_several_templates_and_no_name_lists_them_and_the_config_line(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "ambiguous.pdf"
    config = write_config(tmp_path, "# no [template] table here\n")
    r = run_cli(["render", str(inp), "--output", str(out)], cwd=tmp_path, config=config)
    assert r.returncode == 2
    assert "classic" in r.stderr and "minimal" in r.stderr  # the available templates
    assert "templates are available" in r.stderr
    assert "[template]" in r.stderr
    assert 'name = "classic"' in r.stderr  # the exact line to add
    assert str(config) in r.stderr
    assert "Traceback" not in r.stderr
    assert not out.exists()


def test_active_template_comes_from_the_config(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    classic_out = tmp_path / "classic.pdf"
    minimal_out = tmp_path / "minimal.pdf"
    classic_config = write_config(tmp_path / "classic-config", '[template]\nname = "classic"\n')
    minimal_config = write_config(tmp_path / "minimal-config", '[template]\nname = "minimal"\n')
    r1 = run_cli(["render", str(inp), "--output", str(classic_out), "--overwrite", "--allow-outside-output"], cwd=tmp_path, config=classic_config)
    r2 = run_cli(["render", str(inp), "--output", str(minimal_out), "--overwrite", "--allow-outside-output"], cwd=tmp_path, config=minimal_config)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    # The two templates are visibly different designs.
    assert pdf_text(classic_out.read_bytes()) != pdf_text(minimal_out.read_bytes())
    assert "TECHNICAL SKILLS" in pdf_text(classic_out.read_bytes())


def test_layout_wins_over_the_config(tmp_path):
    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "override.pdf"
    config = write_config(tmp_path, '[template]\nname = "minimal"\n')
    r = run_cli(
        ["render", str(inp), "--layout", "classic", "--output", str(out), "--overwrite", "--allow-outside-output"],
        cwd=tmp_path,
        config=config,
    )
    assert r.returncode == 0, r.stderr
    assert "TECHNICAL SKILLS" in pdf_text(out.read_bytes())  # the classic sidebar


def test_cli_lists_layouts(tmp_path):
    r = run_cli(["layouts"], cwd=tmp_path, config=None)
    assert r.returncode == 0, r.stderr
    for name in SHIPPED_TEMPLATES:
        assert name in r.stdout
    assert "template classic" in r.stdout
    assert "reference" in r.stdout.lower()
    assert str(paths_mod.TEMPLATES_DIR / "classic") in r.stdout  # the template path
    assert ENTRY_POINT_GROUP in r.stdout  # hint how to add one


def test_cli_lists_layouts_as_json(tmp_path):
    r = run_cli(["layouts", "--json"], cwd=tmp_path, config=None)
    assert r.returncode == 0, r.stderr
    entries = json.loads(r.stdout)
    classic = next(entry for entry in entries if entry["name"] == "classic")
    assert classic["origin"] == "template classic"
    assert classic["template"] is True
    assert classic["entry_point"] is None
    assert classic["path"] == str(paths_mod.TEMPLATES_DIR / "classic")
    assert classic["description"]


def test_cli_renders_with_a_discovered_layout_end_to_end(tmp_path, monkeypatch, capsys):
    payload = b"%PDF-1.4\n% custom layout payload\n"
    entry_point = FakeEntryPoint("custom", FixedLayout("custom", payload=payload))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([entry_point]))

    inp = write_input(tmp_path, minimal_cv())
    out = tmp_path / "custom.pdf"
    rc = main(
        ["render", str(inp), "--layout", "custom", "--output", str(out), "--overwrite", "--allow-outside-output"]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert out.read_bytes() == payload  # the custom renderer produced the file
    layout = get_layout("custom")
    assert isinstance(layout.calls[0][0], CVData)  # got the validated model
    assert layout.calls[0][1] == LayoutOptions()  # and explicit options


def test_cli_lists_a_discovered_layout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        fake_entry_points([FakeEntryPoint("custom", FixedLayout("custom"), distribution="third-party-cv")]),
    )
    rc = main(["layouts"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "custom" in captured.out
    assert "distribution third-party-cv (entry point 'custom')" in captured.out
    assert "Deterministic test layout." in captured.out


def test_cli_reports_broken_layout_entry_points(tmp_path, monkeypatch, capsys):
    broken = FakeEntryPoint("broken", error=ImportError("no module named 'nope'"))
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points([broken]))
    with pytest.warns(RuntimeWarning):
        rc = main(["layouts"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "classic" in captured.out
    assert "warning:" in captured.err
    assert "broken" in captured.err


# ---------------------------------------------------------------------------
# Template discovery (the directory scan, not the entry points).
# ---------------------------------------------------------------------------
def test_templates_need_no_entry_point_metadata(monkeypatch):
    """P24: the shipped templates are found by the directory scan alone."""
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        fake_entry_points([], raise_error=RuntimeError("metadata unavailable")),
    )
    with pytest.warns(RuntimeWarning):
        names = [info.name for info in available_layouts()]
    assert set(SHIPPED_TEMPLATES) <= set(names)
    assert {entry[0] for entry in list_templates()} >= set(SHIPPED_TEMPLATES)


def test_discover_templates_registers_a_personal_directory(tmp_path):
    directory = tmp_path / "personal"
    make_template(directory, "personal-one", description="From my own directory.")
    assert discover_templates([directory]) == ["personal-one"]
    info = get_layout_info("personal-one")
    assert info.origin == "template personal-one"
    assert info.path == directory / "personal-one"
    assert info.description == "From my own directory."
    assert render_layout("personal-one", CVData.model_validate(minimal_cv())) == TEST_PAYLOAD


def test_discover_templates_is_idempotent_and_resettable(tmp_path):
    directory = tmp_path / "personal"
    make_template(directory, "once")
    assert discover_templates([directory]) == ["once"]
    assert discover_templates([directory]) == []  # already scanned
    with pytest.warns(RuntimeWarning):
        assert discover_templates([directory], force=True) == []  # clash, skipped
    assert any("already registered" in message for message in discovery_warnings())
    assert get_layout("once").name == "once"


def test_template_directory_without_layout_py_is_skipped_with_a_warning(tmp_path):
    directory = tmp_path / "personal"
    template_dir = make_template(directory, "no-code", with_layout_code=False)
    (template_dir / "README.md").write_text("not a template yet\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        assert discover_templates([directory]) == []
    assert any("has no layout.py" in message for message in discovery_warnings())
    assert "no-code" not in {info.name for info in available_layouts()}


def test_broken_template_module_is_skipped_with_a_warning(tmp_path):
    directory = tmp_path / "personal"
    template_dir = directory / "broken"
    template_dir.mkdir(parents=True)
    (template_dir / "template.toml").write_text('name = "broken"\n', encoding="utf-8")
    (template_dir / "layout.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        assert discover_templates([directory]) == []
    assert any("boom" in message for message in discovery_warnings())


def test_template_without_layout_attribute_is_skipped(tmp_path):
    directory = tmp_path / "personal"
    template_dir = directory / "empty"
    template_dir.mkdir(parents=True)
    (template_dir / "layout.py").write_text("# nothing here\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        assert discover_templates([directory]) == []
    assert any("defines no LAYOUT" in message for message in discovery_warnings())


def test_personal_template_replaces_a_shipped_one(tmp_path, monkeypatch):
    directory = tmp_path / "personal"
    make_template(directory, "classic", description="My own classic.")
    monkeypatch.setattr(paths_mod, "CONFIG", _config_with_template_dirs(directory))
    reset_layouts()
    info = get_layout_info("classic")
    assert info.description == "My own classic."
    assert info.path == directory / "classic"
    # The shipped directory is still scanned; its clash is not a failure.
    assert set(SHIPPED_TEMPLATES) <= {name for name, *_rest in list_templates()}


def _config_with_template_dirs(directory: Path):
    """A fake ``cvgen.toml`` table naming extra template directories."""
    from cv_generator.config import Config as _Config

    return _Config(path=Path("/tmp/fake-cvgen.toml"), data={"template_dirs": [str(directory)]})


def test_template_search_order_is_personal_then_shipped(tmp_path, monkeypatch):
    directory = tmp_path / "personal"
    monkeypatch.setattr(paths_mod, "CONFIG", _config_with_template_dirs(directory))
    dirs = template_search_dirs()
    assert dirs[0] == directory
    assert dirs[-1] == paths_mod.TEMPLATES_DIR


def test_template_toml_name_mismatch_is_reported(tmp_path):
    directory = tmp_path / "personal"
    template_dir = make_template(directory, "declared")
    (template_dir / "template.toml").write_text('name = "something-else"\n', encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        assert discover_templates([directory]) == ["declared"]
    assert any("something-else" in message for message in discovery_warnings())


def test_cli_reports_the_template_selection_when_it_is_ambiguous(tmp_path):
    # A config file without a [template] table: two templates, no active one.
    config = write_config(tmp_path, "# no [template] table here\n")
    r = run_cli(["layouts"], cwd=tmp_path, config=config)
    assert r.returncode == 0
    assert "templates found" in r.stdout
    assert "[template] name = ..." in r.stdout
