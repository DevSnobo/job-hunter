"""Rendering and PDF-content tests (uses only locked project dependencies)."""

import copy
import io
import re

import pytest
from pypdf import PdfReader

from cv_generator import CVData, LayoutOverflowError, render_pdf
from cv_generator.photo import PhotoError


def norm(text: str) -> str:
    """Collapse all whitespace so tracked/synthetic spaces do not matter."""
    return re.sub(r"\s+", " ", text)


def extract(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def render(cv_data: dict) -> bytes:
    return render_pdf(CVData.model_validate(cv_data))


# ---------------------------------------------------------------------------
# Historical English recreation.
# ---------------------------------------------------------------------------
EN_SUBSTANTIVE_STRINGS = [
    "Ada", "Lovelace", "Software Developer", "EXPERIENCE",
    "Software Developer Backend",
    "Analytical Engines, Berlin | 09.2022 - present",
    "Maintenance, further development and new implementation of features for an EAI software solution; integration of third-party systems (ESB)",
    "Designed and implemented reusable internal mini-frameworks as part of an evolving SDK for the EAI platform, used internally and made available for SDK users",
    "Optimization of Maven builds by introducing suitable interfaces",
    "Refactored from a monolithic structure to a plugin-based architecture supporting independently deployable and pluggable modules",
    "Maintenance, customisation and enhancement of Maven builds through the use of existing and custom-implemented Maven plugins",
    "Maintenance and improvement of build pipelines",
    "Deployment management in WildFly Application Server",
    "Bachelor Thesis — Software Developer Backend",
    "Analytical Engines, Berlin | 03.2022 – 08.2022",
    "Evaluation of hexagonal architecture in a monolithic application to implement decoupled plugins",
    "Working student — Software Developer Backend / Quality Assurance",
    "Analytical Engines, Berlin | 07.2018 - 02.2022 / 06.2017 – 06.2018",
    "Extension of the SDK for the EAI software",
    "Integration of new third-party systems into the EAI software",
    "Execution of manual tests and automation of UI tests using Selenium",
    "LANGUAGE SKILLS", "German", "Native", "English", "Proficient (C1)",
    "Japanese", "Intermediate", "French", "Basic",
    "TECHNICAL SKILLS", "Languages & Frameworks:", "Java 8/11, JakartaEE, Guice",
    "Build & Tools:", "Git, Maven, IntelliJ, Eclipse, Shell, Linux",
    "Testing & Quality:", "JUnit 4/5, SonarQube",
    "DevOps & Deployment:", "Docker, Docker Compose, Bitbucket Pipelines, Sonatype Nexus",
    "Methods:", "Scrum, Agile, Jira", "Soft Skills:",
    "Mentoring, analytical & objectoriented thinking",
    "EDUCATION",
    "University of Example | Bachelor of Science - Computer Science",
    "CERTIFICATES",
    "Certified Professional for Software Architecture - Foundation Level",
    "London, 10.05.2025", "Ada Lovelace",
    "10.12.1815 in London", "ada@example.com", "+44 7700 900123",
    "Rechenwerkstraße 12", "14467 Potsdam",
]


def test_en_page_is_one_a4(en_data):
    data = render(en_data)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert (float(box.width), float(box.height)) == (595.0, 842.0)


def test_en_has_selectable_text_and_all_substantive_strings(en_data):
    text = norm(extract(render(en_data)))
    missing = [s for s in EN_SUBSTANTIVE_STRINGS if norm(s) not in text]
    assert not missing, f"missing source strings: {missing}"


def test_en_repairs_ligatures_and_keeps_uk_spelling(en_data):
    text = norm(extract(render(en_data)))
    assert "Proficient (C1)" in text
    assert "Pro cient" not in text
    assert "Certified Professional" in text
    assert "Certi ed" not in text
    assert "customisation" in text  # UK spelling preserved


def test_en_omits_education_dates(en_data):
    text = norm(extract(render(en_data)))
    assert "2017 – 2022" not in text
    assert "2017 - 2022" not in text


def test_en_bullets_extract_as_real_bullets(en_data):
    raw = extract(render(en_data))
    assert raw.count("\u2022") == 11  # 7 + 1 + 3 bullet markers
    assert "\x7f" not in raw


def test_en_embeds_actual_image(en_data):
    data = render(en_data)
    reader = PdfReader(io.BytesIO(data))
    xobjects = []
    page = reader.pages[0]
    res = page.get("/Resources", {})
    xobj = res.get("/XObject", {})
    if xobj:
        xobjects = list(xobj.keys())
    assert xobjects, "no image XObject embedded"


# ---------------------------------------------------------------------------
# German proof.
# ---------------------------------------------------------------------------
def test_de_renders_and_extracts_umlauts(de_data):
    text = norm(extract(render(de_data)))
    for needle in [
        "SPRACHEN", "TECHNISCHE SKILLS", "BILDUNG",
        "Deutsch", "Muttersprache", "Fließend (C1)",
        "Gute Grundkenntnisse", "Grundkenntnisse",
        "Rechenwerkstraße 12", "Qualität", "objektorientiertes Denken",
        "09.2022 - heute", "2017 – 2022",
        "Universität Potsdam | Bachelor of Science - Informatik",
    ]:
        assert needle in text, f"missing German string: {needle}"
    for char in ["ä", "ö", "ü", "ß", "–"]:
        assert char in text, f"missing German character: {char}"


def test_de_omits_optional_sections(de_data):
    text = norm(extract(render(de_data)))
    assert "ZERTIFIKATE" not in text
    assert "London, 10.05.2025" not in text


# ---------------------------------------------------------------------------
# Data-driven behaviour.
# ---------------------------------------------------------------------------
def test_modified_name_changes_output(en_data):
    a = render(en_data)
    changed = copy.deepcopy(en_data)
    changed["first_name"] = "Robert"
    b = render(changed)
    assert a != b
    assert "Robert" in norm(extract(b))


def test_modified_bullet_changes_output(en_data):
    a = render(en_data)
    changed = copy.deepcopy(en_data)
    changed["experience"][0]["bullets"][0] = "Completely different bullet text."
    b = render(changed)
    assert a != b
    assert "Completely different bullet text" in norm(extract(b))


def test_section_label_override_changes_output(en_data):
    a = render(en_data)
    changed = copy.deepcopy(en_data)
    changed["section_labels"] = {"experience": "WORK HISTORY"}
    b = render(changed)
    assert a != b
    assert "WORK HISTORY" in norm(extract(b))


def test_optional_certificates_and_signature_can_be_omitted(en_data):
    changed = copy.deepcopy(en_data)
    changed["certificates"] = []
    changed["signature"] = None
    text = norm(extract(render(changed)))
    assert "CERTIFICATES" not in text
    assert "London, 10.05.2025" not in text


# ---------------------------------------------------------------------------
# Overflow: must fail clearly, never emit a partial page.
# ---------------------------------------------------------------------------
def test_experience_overflow_raises(en_data):
    changed = copy.deepcopy(en_data)
    changed["experience"][0]["bullets"] = [
        "A much longer bullet that wraps over several lines of the experience section."
        for _ in range(20)
    ]
    with pytest.raises(LayoutOverflowError, match="experience"):
        render(changed)


def test_sidebar_skill_overflow_raises(en_data):
    changed = copy.deepcopy(en_data)
    changed["skills"] = [
        {"label": f"Group {i}:", "segments": [{"text": "item", "bold": True}]}
        for i in range(40)
    ]
    with pytest.raises(LayoutOverflowError):
        render(changed)


def test_unbreakable_token_raises(en_data):
    changed = copy.deepcopy(en_data)
    changed["experience"][0]["bullets"] = ["X" * 400]
    with pytest.raises(LayoutOverflowError):
        render(changed)


def test_long_skill_label_raises(en_data):
    changed = copy.deepcopy(en_data)
    changed["skills"][0]["label"] = "This label is far too long for the narrow sidebar column."
    with pytest.raises(LayoutOverflowError, match="technical skills"):
        render(changed)


def test_long_section_header_raises(en_data):
    changed = copy.deepcopy(en_data)
    changed["section_labels"] = {"experience": "X" * 80}
    with pytest.raises(LayoutOverflowError):
        render(changed)


# ---------------------------------------------------------------------------
# Photo handling.
# ---------------------------------------------------------------------------
def test_missing_photo_fails(en_data):
    changed = copy.deepcopy(en_data)
    changed["photo"] = "does_not_exist.jpg"
    with pytest.raises(PhotoError):
        render(changed)


def test_corrupt_photo_fails(tmp_path, en_data):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg at all")
    changed = copy.deepcopy(en_data)
    changed["photo"] = str(bad)
    with pytest.raises(PhotoError):
        render(changed)


def test_photo_crop_out_of_range_fails(en_data):
    changed = copy.deepcopy(en_data)
    changed["photo_crop"] = {"left": 99999, "top": 0, "size": 10}
    with pytest.raises(PhotoError):
        render(changed)


# ---------------------------------------------------------------------------
# Regression: dynamic band limits and empty-section suppression.
# ---------------------------------------------------------------------------
def test_experience_uses_freed_space_when_lower_sections_absent(en_data):
    changed = copy.deepcopy(en_data)
    changed["experience"][0]["bullets"].append(
        "Code reviews focused on data integrity, API design and maintainability."
    )
    changed["education"] = []
    changed["certificates"] = []
    changed["signature"] = None
    text = norm(extract(render(changed)))  # must not raise LayoutOverflowError
    assert "Code reviews focused on data integrity, API design and maintainability" in text
    assert "EDUCATION" not in text and "CERTIFICATES" not in text


def test_complete_german_source_fits(de_full_data):
    text = norm(extract(render(de_full_data)))
    assert "Selenium" in text
    assert "2017 – 2022" in text
    assert "ZERTIFIKATE" in text
    assert "Certified Professional for Software Architecture - Foundation Level" in text
    assert "London, 10.05.2025" in text


def test_empty_experience_suppresses_heading(en_data):
    changed = copy.deepcopy(en_data)
    changed["experience"] = []
    text = norm(extract(render(changed)))
    assert "EXPERIENCE" not in text


def test_empty_languages_suppresses_heading(en_data):
    changed = copy.deepcopy(en_data)
    changed["languages"] = []
    text = norm(extract(render(changed)))
    assert "LANGUAGE SKILLS" not in text


def test_empty_skills_suppresses_heading(en_data):
    changed = copy.deepcopy(en_data)
    changed["skills"] = []
    text = norm(extract(render(changed)))
    assert "TECHNICAL SKILLS" not in text


def test_education_absent_certificates_reflow_up(en_data):
    changed = copy.deepcopy(en_data)
    changed["education"] = []
    data = render(changed)
    text = norm(extract(data))
    assert "EDUCATION" not in text
    assert "CERTIFICATES" in text
    # The certificates rule must reflow up toward the experience content, not
    # stay at the historical 183.62pt (bottom-up) anchor that leaves a hole.
    from pypdf import PdfReader
    page = PdfReader(io.BytesIO(data)).pages[0]
    content = page.get_contents().get_data().decode("latin1")
    import re as _re
    main_rules = [float(m.group(1)) for m in _re.finditer(r"n 248\.5 ([\d.]+) m 560\.15", content)]
    assert len(main_rules) == 2  # experience + certificates (education gone)
    assert main_rules[1] > 200.0  # reflowed up from the historical 183.62


def test_no_photo_renders_without_image(en_data):
    changed = copy.deepcopy(en_data)
    changed["photo"] = None
    data = render(changed)
    assert data  # still renders a valid page
    assert "Ada" in norm(extract(data))
