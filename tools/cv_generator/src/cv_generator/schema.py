"""Structured input schema for the CV generator.

The schema strictly separates *content* (what is said) from *layout* (how it is
rendered).  Everything the renderer draws comes from this model; no content is
hard-coded in the renderer.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    """Base model that rejects unknown fields so input typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class ContactKind(str, Enum):
    """Sidebar contact rows and their vector icon."""

    birthday = "birthday"
    email = "email"
    phone = "phone"
    address = "address"


class Contact(_StrictModel):
    """One contact row in the sidebar (icon + one or two text lines)."""

    kind: ContactKind
    lines: list[str] = Field(min_length=1, max_length=2)


class LanguageSkill(_StrictModel):
    name: str
    level: str


class SkillSegment(_StrictModel):
    """A run of text inside a skill group; ``bold`` marks the lead/primary items."""

    text: str
    bold: bool = False


class SkillGroup(_StrictModel):
    label: str
    segments: list[SkillSegment] = Field(default_factory=list)


class ExperienceEntry(_StrictModel):
    role: str
    meta: str
    bullets: list[str] = Field(default_factory=list)


class EducationEntry(_StrictModel):
    title: str
    dates: Optional[str] = None


class Certificate(_StrictModel):
    title: str


class Signature(_StrictModel):
    place_date: str
    name: str


class PhotoCrop(_StrictModel):
    """Deterministic square crop box in source-image pixel coordinates.

    ``left``/``top`` are the top-left corner and ``size`` the square side.
    The renderer clamps the box to the image bounds, so an out-of-range box is
    an input error rather than a crash.
    """

    left: int = Field(ge=0)
    top: int = Field(ge=0)
    size: int = Field(gt=0)


DEFAULT_SECTION_LABELS: dict[str, dict[str, str]] = {
    # Faithful to the source PDFs: the German CV keeps "EXPERIENCE" and
    # "TECHNISCHE SKILLS" in English while translating the other headings.
    "en": {
        "experience": "EXPERIENCE",
        "languages": "LANGUAGE SKILLS",
        "skills": "TECHNICAL SKILLS",
        "education": "EDUCATION",
        "certificates": "CERTIFICATES",
    },
    "de": {
        "experience": "EXPERIENCE",
        "languages": "SPRACHEN",
        "skills": "TECHNISCHE SKILLS",
        "education": "BILDUNG",
        "certificates": "ZERTIFIKATE",
    },
}


class CVData(_StrictModel):
    """Complete structured input for one CV page."""

    schema_version: Literal[1] = 1
    language: Literal["en", "de"]
    first_name: str
    last_name: str
    headline: str
    # Photo: absolute path, or a name/path resolved against the project's
    # profile_pic directory (see cli/render entry points).
    photo: Optional[str] = None
    photo_crop: Optional[PhotoCrop] = None
    contacts: list[Contact] = Field(default_factory=list)
    languages: list[LanguageSkill] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    certificates: list[Certificate] = Field(default_factory=list)
    signature: Optional[Signature] = None
    # Optional per-section heading overrides, keyed by section name
    # (experience/languages/skills/education/certificates).
    section_labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_content(self) -> "CVData":
        for field in ("first_name", "last_name", "headline"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field!r} must not be empty")
        if not self.experience and not self.education and not self.certificates:
            raise ValueError("at least one of experience/education/certificates is required")
        bad = [k for k in self.section_labels if k not in DEFAULT_SECTION_LABELS["en"]]
        if bad:
            raise ValueError(
                f"unknown section label key(s) {sorted(bad)!r}; "
                f"allowed: {sorted(DEFAULT_SECTION_LABELS['en'])!r}"
            )
        return self

    def section_label(self, key: str) -> str:
        return self.section_labels.get(key, DEFAULT_SECTION_LABELS[self.language][key])

    @classmethod
    def from_json_file(cls, path: str | Path) -> "CVData":
        import json

        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top-level JSON value must be an object")
        return cls.model_validate(raw)


__all__ = [
    "ContactKind",
    "Contact",
    "LanguageSkill",
    "SkillSegment",
    "SkillGroup",
    "ExperienceEntry",
    "EducationEntry",
    "Certificate",
    "Signature",
    "PhotoCrop",
    "CVData",
    "DEFAULT_SECTION_LABELS",
]
