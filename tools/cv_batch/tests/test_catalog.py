import json
import os
from pathlib import Path

import pytest

from cv_batch.catalog import CatalogError, build_catalog, sidebar_summary


def build(project, language="de"):
    return build_catalog(
        language=language,
        baseline_path=project.inputs / f"current_{language}.json",
        backlog_path=project.backlog_de if language == "de" else project.backlog_en,
    )


def test_entries_follow_the_baseline_and_keep_role_and_meta(project):
    catalog = build(project)
    assert [entry.id for entry in catalog.entries] == [
        "exp/analytical-engines-ltd-2025-11",
        "exp/numerical-machines-gmbh-2022-09",
    ]
    first = catalog.entries[0]
    assert first.role == "Software Developer"
    assert first.meta.endswith("verfügbar ab 01.2027")


def test_structured_backlog_items_replace_baseline_bullets(project):
    catalog = build(project)
    element_logic = catalog.entries[0]
    ids = [cand.id for cand in element_logic.candidates]
    assert ids == [
        "exp/analytical-engines-ltd-2025-11/el1-short",
        "exp/analytical-engines-ltd-2025-11/el2-short",
        "exp/analytical-engines-ltd-2025-11/el3-short",
    ]
    # HARD RULE: the long-form backlog text is never a candidate.
    assert not [cand for cand in element_logic.candidates if cand.id.endswith("-detailed")]
    groups = {cand.group for cand in element_logic.candidates}
    assert groups == {
        "exp/analytical-engines-ltd-2025-11/el1",
        "exp/analytical-engines-ltd-2025-11/el2",
        "exp/analytical-engines-ltd-2025-11/el3",
    }
    assert "Baseline bullet A" not in [cand.text for cand in element_logic.candidates]


def test_entries_without_structured_items_use_baseline_bullets(project):
    catalog = build(project)
    numerical = catalog.entries[1]
    assert [cand.source for cand in numerical.candidates] == ["baseline", "baseline"]
    assert numerical.candidates[0].text == "Maven Builds"
    assert numerical.candidates[0].group is None
    assert any("no bullet-form items" in note for note in catalog.notes)


def test_both_languages_and_sidebar_summary(project):
    for language, expected in (("de", "Sprachen & Frameworks"), ("en", "Languages")):
        catalog = build(project, language)
        assert catalog.language == language
        assert catalog.headline == "Software Developer"
        assert sidebar_summary(catalog)[0].startswith(expected)


def test_sidebar_skills_come_from_the_backlog(project):
    """The CV sidebar is maintained in the backlog, so its edits must land."""
    catalog = build(project)
    skills = catalog.fixed_context["skills"]
    assert [group["label"] for group in skills] == ["Sprachen & Frameworks:", "Build & Tools:"]
    assert skills[0]["segments"] == [
        {"text": "Java, Spring Boot", "bold": True},
        {"text": ", JakartaEE", "bold": False},
    ]
    assert skills[1]["segments"] == [{"text": "Git, Maven, Gradle", "bold": False}]
    # the baseline skills are replaced, not merged
    assert sidebar_summary(catalog) == ["Sprachen & Frameworks: Java, Spring Boot, JakartaEE", "Build & Tools: Git, Maven, Gradle"]


def test_missing_skills_section_falls_back_to_the_baseline(tmp_path):
    baseline = tmp_path / "current_de.json"
    baseline.write_text(json.dumps({"language": "de", "headline": "Software Developer", "experience": [], "skills": [{"label": "Aus Basis:", "segments": [{"text": "Alt", "bold": True}]}]}), encoding="utf-8")
    backlog = tmp_path / "backlog.md"
    backlog.write_text("# Backlog\n\n## 1. Werdegang\n\n- nichts\n", encoding="utf-8")
    catalog = build_catalog(language="de", baseline_path=baseline, backlog_path=backlog)
    assert catalog.fixed_context["skills"][0]["label"] == "Aus Basis:"
    assert any("no readable CV-sidebar skills section" in note for note in catalog.notes)


def test_missing_backlog_is_reported_not_fatal(project, tmp_path):
    catalog = build_catalog(
        language="de",
        baseline_path=project.inputs / "current_de.json",
        backlog_path=tmp_path / "nope.md",
    )
    assert catalog.candidate_count() == 4  # two baseline entries x two bullets
    assert any("backlog not found" in note for note in catalog.notes)


def test_language_mismatch_is_rejected(project):
    with pytest.raises(CatalogError):
        build_catalog(
            language="en",
            baseline_path=project.inputs / "current_de.json",
            backlog_path=project.backlog_en,
        )


def test_missing_baseline_is_rejected(project, tmp_path):
    with pytest.raises(CatalogError):
        build_catalog(
            language="de",
            baseline_path=tmp_path / "missing.json",
            backlog_path=project.backlog_de,
        )


@pytest.mark.integration
def test_real_backlogs_parse_into_structured_element_logic_items():
    # Nothing personal is hardcoded: the private profile is either
    # $CVS_PROFILE_DIR or ``<repo>/profile``; a machine without that data skips.
    repo_root = Path(__file__).resolve().parents[3]
    profile = Path(os.environ["CVS_PROFILE_DIR"]) if "CVS_PROFILE_DIR" in os.environ else repo_root / "profile"
    baseline_de = profile / "inputs" / "current_de.json"
    baseline_en = profile / "inputs" / "current_en.json"
    backlog_de = profile / "backlogs" / "career-knowledge-backlog-DE.md"
    backlog_en = profile / "backlogs" / "career-knowledge-backlog-EN.md"
    for path in (baseline_de, baseline_en, backlog_de, backlog_en):
        if not path.is_file():
            pytest.skip(f"user data not available: {path}")
    for language, baseline, backlog in (
        ("de", baseline_de, backlog_de),
        ("en", baseline_en, backlog_en),
    ):
        catalog = build_catalog(language=language, baseline_path=baseline, backlog_path=backlog)
        element_logic = catalog.entries[0]
        short = [c for c in element_logic.candidates if c.id.endswith("-short")]
        detailed = [c for c in element_logic.candidates if c.id.endswith("-detailed")]
        assert len(short) == 9, [c.id for c in short]
        # HARD RULE: no long-form text is ever offered to the selector.
        assert detailed == []
        assert all(c.group for c in element_logic.candidates)
        # 9 bullet-form Analytical Engines Ltd items + 5 Numerical Machines + 1 thesis baseline bullets.
        assert catalog.candidate_count() == 15
