"""Artifact import: copies, versioning, path safety, and failure cleanup."""
from __future__ import annotations

import os
from pathlib import Path

from helpers import payload, rows


def test_attach_copies_without_moving_the_source(run, new_app, tmp_path):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("original posting text")
    result = run("attach", app_id, str(source), "--type", "job_posting")
    assert result.ok, result.output
    assert source.exists() and source.read_text() == "original posting text"
    stored = Path(run.root) / "applications" / app_id / "posting.txt"
    assert stored.read_text() == "original posting text"
    detail = payload(run, "show", app_id, "--json")
    assert detail["artifacts"][0]["relative_path"] == f"applications/{app_id}/posting.txt"
    assert detail["artifacts"][0]["artifact_type"] == "job_posting"


def test_repeated_attach_versions_the_filename(run, new_app, tmp_path):
    app_id = new_app()
    source = tmp_path / "cv.pdf"
    source.write_text("v1")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    source.write_text("v2")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    stored = sorted(path.name for path in (Path(run.root) / "applications" / app_id).iterdir())
    assert stored == ["cv-2.pdf", "cv.pdf"]
    assert (Path(run.root) / "applications" / app_id / "cv-2.pdf").read_text() == "v2"


def test_explicit_name_and_overwrite(run, new_app, tmp_path):
    app_id = new_app()
    first = tmp_path / "draft.txt"
    first.write_text("one")
    assert run("attach", app_id, str(first), "--name", "cover-letter.txt").ok
    second = tmp_path / "draft2.txt"
    second.write_text("two")
    assert run("attach", app_id, str(second), "--name", "cover-letter.txt").exit_code == 2
    replaced = run("attach", app_id, str(second), "--name", "cover-letter.txt", "--overwrite")
    assert replaced.ok, replaced.output
    assert (Path(run.root) / "applications" / app_id / "cover-letter.txt").read_text() == "two"
    assert len(payload(run, "show", app_id, "--json")["artifacts"]) == 1


def test_filename_traversal_is_neutralised(run, new_app, tmp_path, project_root):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("x")
    result = run("attach", app_id, str(source), "--name", "../../escape.txt")
    assert result.ok, result.output
    assert not (project_root / "escape.txt").exists()
    assert (project_root / "applications" / app_id / "escape.txt").is_file()


def test_symlinked_application_directory_is_refused(run, new_app, tmp_path, project_root):
    app_id = new_app()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project_root / "applications").mkdir(exist_ok=True)
    app_dir = project_root / "applications" / app_id
    if app_dir.exists():
        app_dir.rmdir()
    os.symlink(outside, app_dir)
    source = tmp_path / "posting.txt"
    source.write_text("x")
    result = run("attach", app_id, str(source), "--type", "job_posting")
    assert result.exit_code == 2
    assert "escapes" in result.output or "escape" in result.output
    assert list(outside.iterdir()) == []


def test_missing_source_and_directories_are_rejected(run, new_app, tmp_path):
    app_id = new_app()
    assert run("attach", app_id, str(tmp_path / "nope.txt")).exit_code == 2
    directory = tmp_path / "folder"
    directory.mkdir()
    assert run("attach", app_id, str(directory)).exit_code == 2
    assert run("attach", "unknown-company", str(directory)).exit_code == 2


def test_invalid_artifact_type_is_rejected(run, new_app, tmp_path):
    app_id = new_app()
    source = tmp_path / "x.txt"
    source.write_text("x")
    result = run("attach", app_id, str(source), "--type", "not-a-type")
    assert result.exit_code == 2 and "unknown" in result.output


def test_failed_registration_leaves_no_staged_files(run, new_app, tmp_path, project_root):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("x")
    assert run("attach", app_id, str(source), "--type", "job_posting").ok
    refused = run("attach", app_id, str(source), "--type", "job_posting", "--name", "posting.txt")
    assert refused.exit_code == 2
    directory = project_root / "applications" / app_id
    leftovers = [path.name for path in directory.iterdir() if path.name.startswith(".tmp-import-")]
    assert leftovers == []
    assert sorted(path.name for path in directory.iterdir()) == ["posting.txt"]


def test_doctor_flags_missing_file_and_unregistered_file(run, new_app, tmp_path, project_root):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("x")
    assert run("attach", app_id, str(source), "--type", "job_posting").ok
    stored = project_root / "applications" / app_id / "posting.txt"
    stored.unlink()
    (project_root / "applications" / app_id / "stray.txt").write_text("y")
    report = payload(run, "db", "doctor", "--json")
    assert any(path.endswith("posting.txt") for path in report["missing_files"])
    assert any(path.endswith("stray.txt") for path in report["unregistered_files"])
