"""Integration: the real CV generator renders the real catalog selections.

These tests are read-only with respect to the user's data: application folders,
generator inputs, backlogs and the exports directory are only read.  All output goes to
temporary directories.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cv_batch.catalog import build_catalog
from cv_batch.config import build_paths
from cv_batch.prompt import Limits
from cv_batch.model import PrimeAgentAdapter
from cv_batch.runner import JobSpec, Runner
from cv_batch.sessions import ModelPolicy, SessionPolicy

from conftest import selection_answer, target_name

# The repository this checkout lives in; nothing personal is hardcoded.  The
# private profile is either $CVS_PROFILE_DIR or ``<repo>/profile``, and the
# tests skip cleanly when a machine has no personal data.
REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = Path(os.environ["CVS_PROFILE_DIR"]) if "CVS_PROFILE_DIR" in os.environ else REPO_ROOT / "profile"
GENERATOR = REPO_ROOT / "tools" / "cv_generator"

pytestmark = pytest.mark.integration


def _require(paths: list[Path]) -> None:
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        pytest.skip("user data not available: " + ", ".join(missing))


@pytest.fixture
def real_catalog(tmp_path):
    _require(
        [
            PROFILE / "inputs" / "current_de.json",
            PROFILE / "inputs" / "current_en.json",
            PROFILE / "backlogs" / "career-knowledge-backlog-DE.md",
            PROFILE / "backlogs" / "career-knowledge-backlog-EN.md",
        ]
    )

    def load(language: str):
        return build_catalog(
            language=language,
            baseline_path=PROFILE / "inputs" / f"current_{language}.json",
            backlog_path=PROFILE / "backlogs" / f"career-knowledge-backlog-{language.upper()}.md",
        )

    return load


def _answer_from_catalog(catalog, per_entry: int = 3) -> str:
    """A valid selection: one candidate per group, at most ``per_entry`` per entry."""
    picks: dict[str, list[str]] = {}
    for entry in catalog.entries[:2]:
        chosen: list[str] = []
        seen_groups: set[str] = set()
        for cand in entry.candidates:
            if cand.group and cand.group in seen_groups:
                continue
            if cand.group:
                seen_groups.add(cand.group)
            chosen.append(cand.id)
            if len(chosen) >= per_entry:
                break
        if chosen:
            picks[entry.id] = chosen
    return selection_answer(picks)


def _run_real(project, fake_agent, language: str, catalog, tmp_path, *, allow_overwrite: bool = False, existing: bytes | None = None) -> tuple:
    fake_agent.steps([{"answer": _answer_from_catalog(catalog)}])
    # The real profile *and* its real backlogs: the selection ids the fixture
    # catalog produced must be the ids this runner's catalog accepts.
    paths = build_paths(
        job_root=project.root,
        applications=project.applications,
        profile_dir=PROFILE,
        backlog_de=PROFILE / "backlogs" / "career-knowledge-backlog-DE.md",
        backlog_en=PROFILE / "backlogs" / "career-knowledge-backlog-EN.md",
        generator_project=GENERATOR,
        state_dir=tmp_path / "state",
    )
    runner = Runner(
        paths=paths,
        language=language,
        model_policy=ModelPolicy("auto"),
        session_policy=SessionPolicy("fresh", paths.sessions),
        agent=PrimeAgentAdapter(fake_agent.exe),
        generator_exe=None,  # real: uv run --project <cv_generator> cvgen render ...
        limits=Limits(max_bullets_per_entry=5, max_bullets_total=13, max_headline=60),
        posting_chars=6000,
        model_timeout=30.0,
        render_timeout=120.0,
        trimming=True,
        trim_attempts=4,
        content_retries=0,
        allow_replace_invalid=False,
        allow_overwrite=allow_overwrite,
        log=lambda *_: None,
    )
    directory = project.add_app(f"Real Render {language.upper()}")
    if existing is not None:
        (directory / target_name(directory.name, language)).write_bytes(existing)
    job = JobSpec(
        folder=directory.name,
        directory=directory,
        posting=directory / "posting.txt",
        language=language,
        naming=project.naming(language),
    )
    return runner.run_job(job), job, paths


@pytest.mark.parametrize("language", ["de", "en"])
def test_real_generator_renders_both_languages_on_temporary_paths(
    project, fake_agent, real_catalog, tmp_path, language
):
    catalog = real_catalog(language)
    cv_outputs = REPO_ROOT / "exports" / "generator"
    before = {p.name for p in cv_outputs.iterdir()} if cv_outputs.is_dir() else set()

    result, job, paths = _run_real(project, fake_agent, language, catalog, tmp_path)

    assert result.status == "ok", result.message
    pdf = job.directory / target_name(job.folder, language)
    assert pdf.is_file() and pdf.stat().st_size > 1000
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert b"%%EOF" in pdf.read_bytes()[-2048:]
    assert result.bullet_count >= 1

    after = {p.name for p in cv_outputs.iterdir()} if cv_outputs.is_dir() else set()
    assert after == before, "the real cv_outputs directory must stay untouched"

    # the saved evidence validates against the generator's own schema
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    schema_check = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            str(REPO_ROOT),
            "--package",
            "cv-generator",
            "python",
            "-c",
            (
                "import json,sys;from cv_generator import CVData;"
                "d=json.load(open(sys.argv[1]));CVData.model_validate(d.get('generator_input', d))"
            ),
            str(result.input_json),
        ],
        capture_output=True,
        text=True,
        cwd=str(paths.generator_project),
    )
    assert schema_check.returncode == 0, schema_check.stderr
    assert saved["job"]["folder"] == job.folder
    assert saved["job"]["language"] == language
    assert paths.staging.exists() and list(paths.staging.iterdir()) == []
    # the application folder gained exactly one file
    assert sorted(p.name for p in job.directory.iterdir()) == sorted(
        ["posting.txt", "source.txt", pdf.name]
    )


def test_real_generator_replaces_an_existing_valid_cv_with_overwrite(
    project, fake_agent, real_catalog, tmp_path
):
    """--overwrite with the real renderer: old bytes backed up, new PDF in place."""
    old_bytes = b"%PDF-1.4 an older tailored CV\n%%EOF\n"
    catalog = real_catalog("de")
    exports = REPO_ROOT / "exports" / "generator"
    before = {p.name for p in exports.iterdir()} if exports.is_dir() else set()

    result, job, paths = _run_real(
        project, fake_agent, "de", catalog, tmp_path, allow_overwrite=True, existing=old_bytes
    )

    assert result.status == "ok", result.message
    pdf = job.directory / target_name(job.folder, "de")
    assert pdf.read_bytes().startswith(b"%PDF-") and pdf.stat().st_size > 1000
    assert pdf.read_bytes() != old_bytes
    backups = list(paths.replaced.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes
    assert result.backup == backups[0]
    saved = json.loads(result.input_json.read_text(encoding="utf-8"))
    assert saved["job"]["existing_target_state"] == "valid"
    assert saved["job"]["backup"] == str(backups[0])
    assert list(paths.staging.iterdir()) == []
    assert ({p.name for p in exports.iterdir()} if exports.is_dir() else set()) == before
    assert sorted(p.name for p in job.directory.iterdir()) == sorted(
        ["posting.txt", "source.txt", pdf.name]
    )
