"""Local, data-driven CV generator (ReportLab PDF).

Rendering goes through pluggable layouts (:mod:`cv_generator.layouts`): the
shipped ``classic`` template is the reference design, templates in the
directories named by ``cvgen.toml`` add or replace layouts, and a third party
can ship one through the ``cv_generator.layouts`` entry-point group.
:func:`render_pdf` is the backward-compatible wrapper for the shipped layout.
"""

from .layouts import (
    DEFAULT_LAYOUT,
    ENTRY_POINT_GROUP,
    Layout,
    LayoutError,
    LayoutOptions,
    LayoutOverflowError,
    LayoutRegistrationError,
    RegisteredLayout,
    UnknownLayoutError,
    available_layouts,
    discover_layouts,
    get_layout,
    register_layout,
    render_layout,
    unregister_layout,
)
from .render import render_pdf
from .schema import (
    CVData,
    Certificate,
    Contact,
    ContactKind,
    EducationEntry,
    ExperienceEntry,
    LanguageSkill,
    PhotoCrop,
    Signature,
    SkillGroup,
    SkillSegment,
)

__all__ = [
    "CVData",
    "Certificate",
    "Contact",
    "ContactKind",
    "DEFAULT_LAYOUT",
    "ENTRY_POINT_GROUP",
    "EducationEntry",
    "ExperienceEntry",
    "LanguageSkill",
    "Layout",
    "LayoutError",
    "LayoutOptions",
    "LayoutOverflowError",
    "LayoutRegistrationError",
    "PhotoCrop",
    "RegisteredLayout",
    "Signature",
    "SkillGroup",
    "SkillSegment",
    "UnknownLayoutError",
    "available_layouts",
    "discover_layouts",
    "get_layout",
    "register_layout",
    "render_layout",
    "render_pdf",
    "unregister_layout",
]

__version__ = "0.1.0"
