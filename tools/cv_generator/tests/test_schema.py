"""Schema validation tests."""

import json

import pytest
from pydantic import ValidationError

from cv_generator.schema import CVData


def test_fixtures_parse(en_data, de_data):
    CVData.model_validate(en_data)
    CVData.model_validate(de_data)


def test_unknown_field_fails_with_path(en_data):
    en_data["bogus_field"] = "x"
    with pytest.raises(ValidationError) as exc:
        CVData.model_validate(en_data)
    assert "bogus_field" in str(exc.value)


def test_wrong_type_fails_with_path(en_data):
    en_data["experience"] = [{"role": 123, "meta": "x", "bullets": []}]
    with pytest.raises(ValidationError) as exc:
        CVData.model_validate(en_data)
    assert "role" in str(exc.value)


def test_invalid_language_fails(en_data):
    en_data["language"] = "fr"
    with pytest.raises(ValidationError) as exc:
        CVData.model_validate(en_data)
    assert "language" in str(exc.value)


def test_empty_name_fails(en_data):
    en_data["first_name"] = "   "
    with pytest.raises(ValidationError) as exc:
        CVData.model_validate(en_data)
    assert "first_name" in str(exc.value)


def test_no_content_sections_fails(en_data):
    en_data["experience"] = []
    en_data["education"] = []
    en_data["certificates"] = []
    with pytest.raises(ValidationError):
        CVData.model_validate(en_data)


def test_unknown_section_label_fails(en_data):
    en_data["section_labels"] = {"nonsense": "X"}
    with pytest.raises(ValidationError) as exc:
        CVData.model_validate(en_data)
    assert "section label" in str(exc.value)


def test_section_label_override_works(en_data):
    en_data["section_labels"] = {"experience": "WORK HISTORY"}
    cv = CVData.model_validate(en_data)
    assert cv.section_label("experience") == "WORK HISTORY"
    assert cv.section_label("education") == "EDUCATION"


def test_contact_kind_validation(en_data):
    en_data["contacts"][0]["kind"] = "telegram"
    with pytest.raises(ValidationError):
        CVData.model_validate(en_data)


def test_invalid_json_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        CVData.from_json_file(p)


def test_top_level_non_object(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        CVData.from_json_file(p)


def test_photo_crop_bounds(tmp_path):
    p = tmp_path / "crop.json"
    p.write_text(json.dumps({
        "schema_version": 1, "language": "en",
        "first_name": "A", "last_name": "B", "headline": "C",
        "experience": [{"role": "R", "meta": "M", "bullets": ["b"]}],
        "photo_crop": {"left": -1, "top": 0, "size": 10},
    }), encoding="utf-8")
    with pytest.raises(ValidationError):
        CVData.from_json_file(p)
