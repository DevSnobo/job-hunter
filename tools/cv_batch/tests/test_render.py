"""Renderer command construction and the pluggable-layout pass-through.

``cv_batch`` does not own the layout registry: the name given on the command
line is forwarded verbatim to ``cvgen render --layout <name>``, so layouts added
by the generator or by a third-party package work without a cv_batch change.
These tests use the fake generator only; the real one is covered by the
``integration`` marker.
"""

from __future__ import annotations

import pytest

from cv_batch.config import DEFAULT_LAYOUT
from cv_batch.render import RenderError, render_command, render_pdf


def test_render_command_sends_the_default_layout_explicitly(tmp_path):
    """The default is a real flag value: omitting --layout means classic."""
    argv = render_command(
        generator_project=tmp_path / "generator",
        generator_exe=None,
        input_path=tmp_path / "input.json",
        output_path=tmp_path / "out.pdf",
        layout=DEFAULT_LAYOUT,
    )

    assert argv[:6] == ["uv", "run", "--quiet", "--project", str(tmp_path / "generator"), "cvgen"]
    assert argv[6:] == [
        "render",
        str(tmp_path / "input.json"),
        "--output",
        str(tmp_path / "out.pdf"),
        "--allow-outside-output",
        "--layout",
        "classic",
    ]


def test_render_command_omits_the_flag_when_no_layout_is_given(tmp_path):
    """Without a name the generator keeps its own default (no local guess)."""
    argv = render_command(
        generator_project=tmp_path / "generator",
        generator_exe=None,
        input_path=tmp_path / "input.json",
        output_path=tmp_path / "out.pdf",
    )

    assert "--layout" not in argv
    assert argv[-1] == "--allow-outside-output"


@pytest.mark.parametrize("name", ["classic", "compact-2026", "third_party/sidebar-2col", "A B"])
def test_render_command_forwards_any_name_unchanged(tmp_path, name):
    """The name is opaque: cv_batch must not normalise or validate it."""
    argv = render_command(
        generator_project=tmp_path / "generator",
        generator_exe=str(tmp_path / "cvgen"),
        input_path=tmp_path / "input.json",
        output_path=tmp_path / "out.pdf",
        layout=name,
    )

    assert argv[0] == str(tmp_path / "cvgen")
    assert argv[-3:] == ["--allow-outside-output", "--layout", name]
    assert argv[argv.index("--layout") + 1] == name


def test_render_pdf_forwards_the_layout_to_the_generator(tmp_path, fake_renderer):
    """End-to-end through a subprocess: a known non-default layout is accepted."""
    fake_renderer.configure(overflow_times=0, layouts=("classic", "compact-2026"))
    input_path = tmp_path / "input.json"
    input_path.write_text("{}", encoding="utf-8")
    output = tmp_path / "out.pdf"

    result = render_pdf(
        generator_project=tmp_path,
        generator_exe=fake_renderer.exe,
        input_path=input_path,
        output_path=output,
        layout="compact-2026",
    )

    assert result.ok
    argv = fake_renderer.calls()[0]["argv"]
    assert argv[argv.index("--layout") + 1] == "compact-2026"
    assert output.read_bytes().startswith(b"%PDF-")


def test_render_pdf_reports_an_unknown_layout_as_a_render_error(tmp_path, fake_renderer):
    """A bad name fails like any renderer error; cv_batch does not pre-validate."""
    fake_renderer.configure(layouts=("classic", "compact-2026"))
    input_path = tmp_path / "input.json"
    input_path.write_text("{}", encoding="utf-8")
    output = tmp_path / "out.pdf"

    with pytest.raises(RenderError) as excinfo:
        render_pdf(
            generator_project=tmp_path,
            generator_exe=fake_renderer.exe,
            input_path=input_path,
            output_path=output,
            layout="does-not-exist",
        )

    assert excinfo.value.category == "render"
    assert "renderer failed (exit 3)" in str(excinfo.value)
    assert "unknown layout" in str(excinfo.value)
    assert not output.exists()
