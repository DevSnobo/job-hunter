"""The ``classic`` template: a ReportLab one-page A4 CV with a photo sidebar.

A *template* is a directory with ``layout.py`` (this file) and ``template.toml``
(the pure values).  The generator discovers every template below its own
``templates/`` directory and below each ``template_dirs`` entry of
``cvgen.toml``; each one registers itself under its own ``name``.  This one is
the shipped ``classic`` layout.

What lives where
----------------
Every *pure value* - page size, palette, font names, column/sidebar geometry,
the fixed anchors, the tracking and size values and the optional bullet limits -
is read once from ``template.toml`` at import.  The *relational* constants stay
in this file on purpose: the gaps between sections (``ENTRY_GAP``,
``CONTACT_GAP``, ``EDU_RULE_GAP`` ...) encode the measured relationships that
make the later sections reflow, and the test suite asserts the exact rule
coordinates they produce.

Writing your own template
-------------------------
Copy ``templates/minimal`` (the small worked example), give it a unique ``name``
in its own ``template.toml``, and keep the three contract items here: the
``LAYOUT`` attribute (a layout instance or a zero-arg factory), a one-line
``description`` and ``render(cv, options) -> bytes``.  Raise
:class:`~cv_generator.layouts.LayoutOverflowError` when the content does not fit
the page - the CLI then reports a clean error and writes no file.  A layout must
never print to stdout.

Relationship to the source PDFs
-------------------------------
The renderer is intentionally *not* pixel-perfect against the source PDFs but
reproduces the source composition: sidebar + vertical divider + photo, letter
spaced name/headline, section headers with thin grey rules, grey body text and
a lower-right signature.  Fonts and colours match the source (Helvetica,
Helvetica-Bold).

Layout model
------------
Content flows forward from source-measured anchors.  Empty sections are
suppressed (no heading/rule) and following sections reflow up.  The signature
is pinned to the bottom when present; otherwise content may use the freed space
down to a page-bottom margin.  Overflow is detected before drawing and raises
:class:`LayoutOverflowError`, so a caller never gets a clipped or partially
written PDF.
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as pdfcanvas

from cv_generator.layouts import LayoutOptions, LayoutOverflowError, register_layout
from cv_generator.photo import load_photo, to_jpeg_image
from cv_generator.schema import ContactKind, CVData
from cv_generator.template import read_template_toml, value

# --------------------------------------------------------------------------
# The values: ``template.toml`` next to this file is the single source of
# truth for every pure value, and it is read once, here.
# --------------------------------------------------------------------------
_TEMPLATE_FILE = Path(__file__).resolve().with_name("template.toml")
_VALUES = read_template_toml(_TEMPLATE_FILE)


def _v(section: str, key: str):
    """One required value from ``template.toml`` (clear error when missing)."""
    return value(_VALUES, section, key, where=str(_TEMPLATE_FILE))


NAME = str(_VALUES.get("name", "classic"))
DESCRIPTION = str(
    _VALUES.get("description", "Photo sidebar, A4, one page (the shipped classic layout).")
)

# Page geometry (A4 as in the source PDFs: 595 x 842 pt).
PAGE_W = float(_v("page", "width"))
PAGE_H = float(_v("page", "height"))

# Palette (source PDF values).
C_RULE = HexColor(_v("palette", "rule"))      # dividers/rules + light surname
C_DARK = HexColor(_v("palette", "dark"))      # name, section headers, bold labels
C_BODY = HexColor(_v("palette", "body"))      # main-column body text, bullets, dates
C_GREY = HexColor(_v("palette", "grey"))      # headline, contact text, icon glyphs

FONT = str(_v("fonts", "regular"))
FONT_BOLD = str(_v("fonts", "bold"))
FONT_SYMBOL = str(_v("fonts", "symbol"))      # renders the real bullet glyph U+2022

# Fixed horizontal layout (points).
PHOTO_SIZE = float(_v("geometry", "photo_size"))
DIVIDER_X = float(_v("geometry", "divider_x"))
SIDEBAR_TEXT_X = float(_v("geometry", "sidebar_text_x"))
SIDEBAR_RULE_LEFT = float(_v("geometry", "sidebar_rule_left"))
SIDEBAR_RULE_RIGHT = float(_v("geometry", "sidebar_rule_right"))
CONTACT_TEXT_X = float(_v("geometry", "contact_text_x"))
ICON_CENTER_X = float(_v("geometry", "icon_center_x"))
ICON_SIZE = float(_v("geometry", "icon_size"))
ICON_GLYPH_OFFSET = float(_v("geometry", "icon_glyph_offset"))
MAIN_LEFT = float(_v("geometry", "main_left"))
MAIN_RULE_RIGHT = float(_v("geometry", "main_rule_right"))
MAIN_TEXT_WIDTH = float(_v("geometry", "main_text_width"))
MAIN_BODY_WIDTH = float(_v("geometry", "main_body_width"))
BULLET_X = float(_v("geometry", "bullet_x"))
BULLET_TEXT_X = float(_v("geometry", "bullet_text_x"))
BULLET_GLYPH_DROP = float(_v("geometry", "bullet_glyph_drop"))
SIGNATURE_RIGHT = float(_v("geometry", "signature_right"))
SIDEBAR_TEXT_WIDTH = float(_v("geometry", "sidebar_text_width"))
CONTACT_TEXT_WIDTH = float(_v("geometry", "contact_text_width"))

# Fixed top anchors and bottom pin (source PDF values, top-down).
NAME1_Y = float(_v("anchors", "name1_y"))
NAME2_Y = float(_v("anchors", "name2_y"))
HEADLINE_Y = float(_v("anchors", "headline_y"))

EXPERIENCE_HEADER_Y = float(_v("anchors", "experience_header_y"))
EXPERIENCE_RULE_Y = float(_v("anchors", "experience_rule_y"))
EXPERIENCE_START_Y = float(_v("anchors", "experience_start_y"))

SIGNATURE_DATE_Y = float(_v("anchors", "signature_date_y"))
SIGNATURE_NAME_Y = float(_v("anchors", "signature_name_y"))
SIGNATURE_CLEARANCE = float(_v("anchors", "signature_clearance"))
CONTENT_BOTTOM_WITH_SIGNATURE = SIGNATURE_DATE_Y - SIGNATURE_CLEARANCE
CONTENT_BOTTOM_NO_SIGNATURE = float(_v("anchors", "content_bottom_no_signature"))

# Sidebar anchors.
CONTACT_START_Y = float(_v("anchors", "contact_start_y"))
SIDEBAR_BOTTOM = float(_v("anchors", "sidebar_bottom"))

# Typography (points / em fractions of the font size).
BODY_SIZE = float(_v("typography", "body_size"))
HEADER_SIZE = float(_v("typography", "header_size"))
NAME_SIZE = float(_v("typography", "name_size"))
SIGNATURE_NAME_SIZE = float(_v("typography", "signature_name_size"))
LANGUAGE_DASH_SIZE = float(_v("typography", "language_dash_size"))
RULE_WIDTH = float(_v("typography", "rule_width"))
NAME_TRACKING_EM = float(_v("typography", "name_tracking_em"))
HEADLINE_TRACKING_EM = float(_v("typography", "headline_tracking_em"))
HEADER_TRACKING_EM = float(_v("typography", "header_tracking_em"))

# Optional bullet limits: 0 means "no limit" (the shipped value); the geometry
# decides instead.  A template may set a hard limit to fail early.
MAX_BULLETS_PER_ENTRY = int(_v("limits", "max_bullets_per_entry"))
MAX_BULLETS_TOTAL = int(_v("limits", "max_bullets_total"))

# --------------------------------------------------------------------------
# Relational spacing (points, top-down).  These are the gaps *between* parts of
# the page, i.e. the measured relationships the layout is built on; they stay in
# code so the template values above remain pure and the reflow keeps its
# meaning.
# --------------------------------------------------------------------------
ROLE_TO_META = 14.0
META_TO_BULLET = 21.0
BULLET_LEADING = 14.0
# The source's own entry-to-entry gaps are 26.65pt and 28.0pt (not uniform).
# Using their average keeps the final experience baseline - and therefore the
# following section anchors - exactly at the source position for the
# historical fixture.
ENTRY_GAP = 27.325

CONTACT_GAP = 25.0
CONTACT_LINE2_GAP = 16.0
LANG_LEADING = 13.2
SKILL_VALUE_LEADING = 12.1
SKILL_GROUP_GAP = 21.1        # last value line -> next group label
EDUCATION_LEADING = 12.1

# Section transition gaps (source-measured; preserve historical anchors when
# every section is present and reflow when sections are omitted).
EDU_RULE_GAP = 5.28           # experience last baseline -> education rule
EDU_RULE_TO_HEADER = 22.21
CERT_RULE_GAP = 24.15         # previous content last baseline -> certificates rule
CERT_RULE_TO_HEADER = 20.26
MAIN_HEADER_TO_CONTENT = 34.46

CONTACT_TO_SECTION_GAP = 61.64   # contacts last baseline -> next sidebar header
LANG_TO_SECTION_GAP = 49.32      # languages last baseline -> skills header
LANG_HEADER_TO_RULE = 15.43
LANG_RULE_TO_CONTENT = 19.8
SKILL_HEADER_TO_RULE = 14.63
SKILL_RULE_TO_CONTENT = 19.83


# ``LayoutOverflowError`` is defined in ``cv_generator.layouts`` (it is part of
# the layout contract) and re-exported here for backward compatibility.


def _t(y_top: float) -> float:
    """Convert a top-down baseline to ReportLab bottom-up coordinate."""
    return PAGE_H - y_top


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    lines = simpleSplit(text, font, size, max_width)
    for line in lines:
        if pdfmetrics.stringWidth(line, font, size) > max_width + 0.5:
            # simpleSplit can emit an unbreakable over-long word.
            raise LayoutOverflowError(
                "text", 0.0, 0.0,
                hint=f"a single word cannot fit in {max_width:.1f}pt: {line!r}",
            )
    return lines or [""]


def _ensure_fits(
    text: str, font: str, size: float, max_width: float, region: str,
    char_space: float = 0.0,
) -> None:
    """Raise if ``text`` (optionally letter-spaced) is wider than ``max_width``."""
    width = pdfmetrics.stringWidth(text, font, size)
    if len(text) > 1:
        width += char_space * (len(text) - 1)
    if width > max_width:
        raise LayoutOverflowError(
            region, width, max_width,
            hint=f"'{text}' is {width - max_width:.1f}pt too wide for its column",
        )


def _reset_char_space(c: pdfcanvas.Canvas) -> None:
    """Reset the PDF Tc parameter so tracked text does not leak into normal text."""
    c._code.append("0 Tc")


def _draw_tracked(
    c: pdfcanvas.Canvas,
    x: float,
    y_top: float,
    text: str,
    font: str,
    size: float,
    color,
    char_space: float,
    align: str = "left",
) -> float:
    """Draw letter-spaced text at a top-down baseline; return its width."""
    if align == "right":
        width = pdfmetrics.stringWidth(text, font, size)
        if len(text) > 1:
            width += char_space * (len(text) - 1)
        x -= width
    t = c.beginText(x, _t(y_top))
    t.setFont(font, size)
    t.setFillColor(color)
    t.setCharSpace(char_space)
    t.textOut(text)
    c.drawText(t)
    _reset_char_space(c)
    return t.getX() - x


def _draw_text(
    c: pdfcanvas.Canvas,
    x: float,
    y_top: float,
    text: str,
    font: str,
    size: float,
    color,
    align: str = "left",
) -> float:
    _reset_char_space(c)
    if align == "right":
        x -= pdfmetrics.stringWidth(text, font, size)
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawString(x, _t(y_top), text)
    return pdfmetrics.stringWidth(text, font, size)


def _rule(c: pdfcanvas.Canvas, y_top: float, x0: float, x1: float) -> None:
    c.setStrokeColor(C_RULE)
    c.setLineWidth(RULE_WIDTH)
    y = _t(y_top)
    c.line(x0, y, x1, y)


# --------------------------------------------------------------------------
# Vector contact icons (subtle circular glyphs, grey stroke, no heavy fill).
# --------------------------------------------------------------------------
def _icon_circle(c: pdfcanvas.Canvas, cx: float, cy_top: float) -> None:
    c.setStrokeColor(C_GREY)
    c.setLineWidth(0.9)
    c.circle(cx, _t(cy_top), ICON_SIZE / 2, stroke=1, fill=0)


def _path_cake(c: pdfcanvas.Canvas, cx: float, cy_top: float):
    cy = _t(cy_top)
    p = c.beginPath()
    p.moveTo(cx - 5.0, cy - 3.8)
    p.lineTo(cx + 5.0, cy - 3.8)
    p.rect(cx - 4.2, cy - 3.2, 8.4, 4.6)
    for dx in (-2.6, 0.0, 2.6):
        p.moveTo(cx + dx, cy + 1.4)
        p.lineTo(cx + dx, cy + 3.6)
    return p


def _path_email(c: pdfcanvas.Canvas, cx: float, cy_top: float):
    cy = _t(cy_top)
    p = c.beginPath()
    p.rect(cx - 5.0, cy - 3.4, 10.0, 6.8)
    p.moveTo(cx - 5.0, cy + 3.4)
    p.lineTo(cx, cy + 0.2)
    p.lineTo(cx + 5.0, cy + 3.4)
    return p


def _path_phone(c: pdfcanvas.Canvas, cx: float, cy_top: float):
    cy = _t(cy_top)
    p = c.beginPath()
    p.circle(cx - 3.1, cy + 2.6, 1.7)
    p.circle(cx + 3.1, cy - 2.6, 1.7)
    p.moveTo(cx - 2.2, cy + 1.5)
    p.lineTo(cx + 2.2, cy - 1.5)
    p.moveTo(cx - 2.2, cy + 1.5)
    p.curveTo(cx - 4.6, cy + 1.2, cx - 4.6, cy - 1.2, cx - 2.2, cy - 1.5)
    p.moveTo(cx + 2.2, cy - 1.5)
    p.curveTo(cx + 4.6, cy - 1.2, cx + 4.6, cy + 1.2, cx + 2.2, cy + 1.5)
    return p


def _path_address(c: pdfcanvas.Canvas, cx: float, cy_top: float):
    cy = _t(cy_top)
    p = c.beginPath()
    p.moveTo(cx - 5.4, cy + 0.6)
    p.lineTo(cx, cy + 5.2)
    p.lineTo(cx + 5.4, cy + 0.6)
    p.rect(cx - 4.0, cy - 3.6, 8.0, 4.2)
    p.rect(cx - 1.2, cy - 3.6, 2.4, 2.0)
    return p


_ICON_PATHS = {
    ContactKind.birthday: _path_cake,
    ContactKind.email: _path_email,
    ContactKind.phone: _path_phone,
    ContactKind.address: _path_address,
}


def _draw_icon(c: pdfcanvas.Canvas, kind: ContactKind, cx: float, cy_top: float) -> None:
    _icon_circle(c, cx, cy_top)
    c.setStrokeColor(C_GREY)
    c.setFillColor(C_GREY)
    c.setLineWidth(0.9)
    c.drawPath(_ICON_PATHS[kind](c, cx, cy_top), stroke=1, fill=0)


# --------------------------------------------------------------------------
# Skill token wrapping and multi-font line drawing.
# --------------------------------------------------------------------------
def _skill_tokens(group) -> list[tuple[str, bool]]:
    """Split a skill group into (word, bold) runs, preserving separators."""
    tokens: list[tuple[str, bool]] = []
    for seg in group.segments:
        for part in re.findall(r"\S+\s*", seg.text):
            tokens.append((part, bool(seg.bold)))
    return tokens


def _wrap_skill_tokens(group, max_width: float) -> list[list[tuple[str, bool]]]:
    tokens = _skill_tokens(group)
    lines: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    current_width = 0.0
    for word, bold in tokens:
        word_width = pdfmetrics.stringWidth(
            word, FONT_BOLD if bold else FONT, BODY_SIZE
        )
        if current and current_width + word_width > max_width:
            lines.append(current)
            current = [(word, bold)]
            current_width = word_width
        else:
            current.append((word, bold))
            current_width += word_width
    if current:
        lines.append(current)
    return lines or [[("", False)]]


def _draw_skill_line(
    c: pdfcanvas.Canvas, x: float, y_top: float, line: list[tuple[str, bool]]
) -> None:
    """Draw one wrapped skill-value line as a single text object."""
    t = c.beginText(x, _t(y_top))
    t.setFillColor(C_DARK)
    for word, bold in line:
        t.setFont(FONT_BOLD if bold else FONT, BODY_SIZE)
        t.textOut(word)
    c.drawText(t)


def _draw_language_row(
    c: pdfcanvas.Canvas, x: float, y_top: float, name: str, level: str
) -> None:
    """One language row: bold name, size-10 grey dash, regular grey level."""
    t = c.beginText(x, _t(y_top))
    t.setFillColor(C_DARK)
    t.setFont(FONT_BOLD, BODY_SIZE)
    t.textOut(name)
    t.setFillColor(C_GREY)
    t.setFont(FONT, LANGUAGE_DASH_SIZE)
    t.textOut(" - ")
    t.setFillColor(C_BODY)
    t.setFont(FONT, BODY_SIZE)
    t.textOut(level)
    c.drawText(t)


def _language_row_width(name: str, level: str) -> float:
    return (
        pdfmetrics.stringWidth(name, FONT_BOLD, BODY_SIZE)
        + pdfmetrics.stringWidth(" - ", FONT, LANGUAGE_DASH_SIZE)
        + pdfmetrics.stringWidth(level, FONT, BODY_SIZE)
    )


# --------------------------------------------------------------------------
# Layout building: produce draw operations, then check overflow, then execute.
# Op forms:
#   ("text", x, y_top, text, font, size, color, align)
#   ("tracked", x, y_top, text, font, size, color, char_space)
#   ("rule", y_top, x0, x1)
#   ("icon", kind, cx, cy_top)
#   ("langrow", x, y_top, name, level)
#   ("skillline", x, y_top, line)
# --------------------------------------------------------------------------
def _build_sidebar_ops(cv: CVData) -> list:
    labels = cv.section_label
    ops: list = []

    # Vertical divider (full height).
    ops.append(("divider",))

    # Contact rows (flow).
    y = CONTACT_START_Y
    contact_end = CONTACT_START_Y
    for contact in cv.contacts:
        _ensure_fits(contact.lines[0], FONT, BODY_SIZE, CONTACT_TEXT_WIDTH, "contacts")
        ops.append(("icon", contact.kind, ICON_CENTER_X, y - ICON_GLYPH_OFFSET))
        ops.append(("text", CONTACT_TEXT_X, y, contact.lines[0], FONT, BODY_SIZE, C_GREY, "left"))
        contact_end = y
        if len(contact.lines) > 1:
            _ensure_fits(contact.lines[1], FONT, BODY_SIZE, CONTACT_TEXT_WIDTH, "contacts")
            y += CONTACT_LINE2_GAP
            ops.append(("text", CONTACT_TEXT_X, y, contact.lines[1], FONT, BODY_SIZE, C_GREY, "left"))
            contact_end = y
        y += CONTACT_GAP

    # Language skills section (suppressed when empty).
    lang_end = None
    if cv.languages:
        lang_header = contact_end + CONTACT_TO_SECTION_GAP
        _ensure_fits(
            labels("languages"), FONT, HEADER_SIZE, SIDEBAR_TEXT_WIDTH,
            "language skills", char_space=HEADER_TRACKING_EM * HEADER_SIZE,
        )
        ops.append(("tracked", SIDEBAR_TEXT_X, lang_header, labels("languages"),
                    FONT, HEADER_SIZE, C_DARK, HEADER_TRACKING_EM * HEADER_SIZE))
        ops.append(("rule", lang_header + LANG_HEADER_TO_RULE,
                    SIDEBAR_RULE_LEFT, SIDEBAR_RULE_RIGHT))
        y = lang_header + LANG_HEADER_TO_RULE + LANG_RULE_TO_CONTENT
        for lang in cv.languages:
            width = _language_row_width(lang.name, lang.level)
            if width > SIDEBAR_TEXT_WIDTH:
                raise LayoutOverflowError(
                    "language skills", width, SIDEBAR_TEXT_WIDTH,
                    hint=f"'{lang.name} - {lang.level}' is {width - SIDEBAR_TEXT_WIDTH:.1f}pt too wide",
                )
            ops.append(("langrow", SIDEBAR_TEXT_X, y, lang.name, lang.level))
            lang_end = y
            y += LANG_LEADING

    # Technical skills section (suppressed when empty, reflows up).
    skills_end = None
    if cv.skills:
        prev_end = lang_end if lang_end is not None else contact_end
        gap = LANG_TO_SECTION_GAP if lang_end is not None else CONTACT_TO_SECTION_GAP
        skills_header = prev_end + gap
        _ensure_fits(
            labels("skills"), FONT, HEADER_SIZE, SIDEBAR_TEXT_WIDTH,
            "technical skills", char_space=HEADER_TRACKING_EM * HEADER_SIZE,
        )
        ops.append(("tracked", SIDEBAR_TEXT_X, skills_header, labels("skills"),
                    FONT, HEADER_SIZE, C_DARK, HEADER_TRACKING_EM * HEADER_SIZE))
        ops.append(("rule", skills_header + SKILL_HEADER_TO_RULE,
                    SIDEBAR_RULE_LEFT, SIDEBAR_RULE_RIGHT))
        y = skills_header + SKILL_HEADER_TO_RULE + SKILL_RULE_TO_CONTENT
        for group in cv.skills:
            _ensure_fits(group.label, FONT_BOLD, BODY_SIZE, SIDEBAR_TEXT_WIDTH, "technical skills")
            ops.append(("text", SIDEBAR_TEXT_X, y, group.label, FONT_BOLD, BODY_SIZE, C_DARK, "left"))
            y += SKILL_VALUE_LEADING
            for line in _wrap_skill_tokens(group, SIDEBAR_TEXT_WIDTH):
                ops.append(("skillline", SIDEBAR_TEXT_X, y, line))
                skills_end = y
                y += SKILL_VALUE_LEADING
            y += SKILL_GROUP_GAP - SKILL_VALUE_LEADING

    # Sidebar overflow attribution.
    if contact_end > SIDEBAR_BOTTOM:
        raise LayoutOverflowError("contacts", contact_end, SIDEBAR_BOTTOM)
    if lang_end is not None and lang_end > SIDEBAR_BOTTOM:
        raise LayoutOverflowError("language skills", lang_end, SIDEBAR_BOTTOM)
    if skills_end is not None and skills_end > SIDEBAR_BOTTOM:
        raise LayoutOverflowError("technical skills", skills_end, SIDEBAR_BOTTOM)

    return ops


def _build_main_ops(cv: CVData) -> list:
    labels = cv.section_label
    ops: list = []

    # Name + headline (fixed).
    ops.append(("tracked", MAIN_LEFT, NAME1_Y, cv.first_name, FONT, NAME_SIZE, C_DARK,
                NAME_TRACKING_EM * NAME_SIZE))
    ops.append(("tracked", MAIN_LEFT, NAME2_Y, cv.last_name, FONT, NAME_SIZE, C_RULE,
                NAME_TRACKING_EM * NAME_SIZE))
    ops.append(("tracked", MAIN_LEFT, HEADLINE_Y, cv.headline, FONT, HEADER_SIZE, C_GREY,
                HEADLINE_TRACKING_EM * HEADER_SIZE))

    # Experience header + rule (suppressed when empty).
    exp_last = EXPERIENCE_START_Y
    if cv.experience:
        _ensure_fits(
            labels("experience"), FONT, HEADER_SIZE, MAIN_TEXT_WIDTH, "experience",
            char_space=HEADER_TRACKING_EM * HEADER_SIZE,
        )
        ops.append(("tracked", MAIN_LEFT, EXPERIENCE_HEADER_Y, labels("experience"),
                    FONT, HEADER_SIZE, C_DARK, HEADER_TRACKING_EM * HEADER_SIZE))
        ops.append(("rule", EXPERIENCE_RULE_Y, MAIN_LEFT, MAIN_RULE_RIGHT))

        # Experience entries (flow).
        y = EXPERIENCE_START_Y
        for entry in cv.experience:
            for line in _wrap(entry.role, FONT_BOLD, BODY_SIZE, MAIN_TEXT_WIDTH):
                ops.append(("text", MAIN_LEFT, y, line, FONT_BOLD, BODY_SIZE, C_DARK, "left"))
                exp_last = y
                y += BULLET_LEADING
            for line in _wrap(entry.meta, FONT, BODY_SIZE, MAIN_TEXT_WIDTH):
                ops.append(("text", MAIN_LEFT, y, line, FONT, BODY_SIZE, C_BODY, "left"))
                exp_last = y
                y += BULLET_LEADING
            y += META_TO_BULLET - BULLET_LEADING
            for bullet in entry.bullets:
                for i, line in enumerate(_wrap(bullet, FONT, BODY_SIZE, MAIN_BODY_WIDTH)):
                    if i == 0:
                        # Symbol font renders/extracts a real bullet (U+2022),
                        # matching the source PDF.  Only the first line of each
                        # bullet carries the marker.
                        ops.append(("text", BULLET_X, y + BULLET_GLYPH_DROP, "\u2022",
                                    FONT_SYMBOL, BODY_SIZE, C_BODY, "left"))
                    ops.append(("text", BULLET_TEXT_X, y, line, FONT, BODY_SIZE, C_BODY, "left"))
                    exp_last = y
                    y += BULLET_LEADING
            y += ENTRY_GAP - BULLET_LEADING

    # Education section (flow after experience, suppressed when empty).
    edu_last = None
    if cv.education:
        edu_rule = exp_last + EDU_RULE_GAP
        edu_header = edu_rule + EDU_RULE_TO_HEADER
        _ensure_fits(
            labels("education"), FONT, HEADER_SIZE, MAIN_TEXT_WIDTH, "education",
            char_space=HEADER_TRACKING_EM * HEADER_SIZE,
        )
        ops.append(("rule", edu_rule, MAIN_LEFT, MAIN_RULE_RIGHT))
        ops.append(("tracked", MAIN_LEFT, edu_header, labels("education"),
                    FONT, HEADER_SIZE, C_DARK, HEADER_TRACKING_EM * HEADER_SIZE))
        y = edu_header + MAIN_HEADER_TO_CONTENT
        for edu in cv.education:
            for line in _wrap(edu.title, FONT_BOLD, BODY_SIZE, MAIN_TEXT_WIDTH):
                ops.append(("text", MAIN_LEFT, y, line, FONT_BOLD, BODY_SIZE, C_DARK, "left"))
                edu_last = y
                y += EDUCATION_LEADING
            if edu.dates:
                ops.append(("text", MAIN_LEFT, y, edu.dates, FONT, BODY_SIZE, C_BODY, "left"))
                edu_last = y
                y += EDUCATION_LEADING

    # Certificates section (flow after education or experience).
    cert_last = None
    if cv.certificates:
        prev_last = edu_last if edu_last is not None else exp_last
        cert_rule = prev_last + CERT_RULE_GAP
        cert_header = cert_rule + CERT_RULE_TO_HEADER
        _ensure_fits(
            labels("certificates"), FONT, HEADER_SIZE, MAIN_TEXT_WIDTH, "certificates",
            char_space=HEADER_TRACKING_EM * HEADER_SIZE,
        )
        ops.append(("rule", cert_rule, MAIN_LEFT, MAIN_RULE_RIGHT))
        ops.append(("tracked", MAIN_LEFT, cert_header, labels("certificates"),
                    FONT, HEADER_SIZE, C_DARK, HEADER_TRACKING_EM * HEADER_SIZE))
        y = cert_header + MAIN_HEADER_TO_CONTENT
        for cert in cv.certificates:
            for line in _wrap(cert.title, FONT_BOLD, BODY_SIZE, MAIN_TEXT_WIDTH):
                ops.append(("text", MAIN_LEFT, y, line, FONT_BOLD, BODY_SIZE, C_DARK, "left"))
                cert_last = y
                y += EDUCATION_LEADING

    # Signature (pinned to the bottom when present).
    if cv.signature:
        ops.append(("text", SIGNATURE_RIGHT, SIGNATURE_DATE_Y, cv.signature.place_date,
                    FONT, BODY_SIZE, C_BODY, "right"))
        ops.append(("text", SIGNATURE_RIGHT, SIGNATURE_NAME_Y, cv.signature.name,
                    FONT, SIGNATURE_NAME_SIZE, C_DARK, "right"))

    # Main-column overflow attribution (first section that crosses the limit).
    bottom = (
        CONTENT_BOTTOM_WITH_SIGNATURE if cv.signature else CONTENT_BOTTOM_NO_SIGNATURE
    )
    if exp_last > bottom:
        raise LayoutOverflowError("experience", exp_last, bottom)
    if edu_last is not None and edu_last > bottom:
        raise LayoutOverflowError("education", edu_last, bottom)
    if cert_last is not None and cert_last > bottom:
        raise LayoutOverflowError("certificates", cert_last, bottom)

    return ops


def _execute(c: pdfcanvas.Canvas, ops: list) -> None:
    for op in ops:
        kind = op[0]
        if kind == "text":
            _, x, y, text, font, size, color, align = op
            _draw_text(c, x, y, text, font, size, color, align=align)
        elif kind == "tracked":
            _, x, y, text, font, size, color, char_space = op
            _draw_tracked(c, x, y, text, font, size, color, char_space)
        elif kind == "rule":
            _, y, x0, x1 = op
            _rule(c, y, x0, x1)
        elif kind == "icon":
            _, icon_kind, cx, cy = op
            _draw_icon(c, icon_kind, cx, cy)
        elif kind == "langrow":
            _, x, y, name, level = op
            _draw_language_row(c, x, y, name, level)
        elif kind == "skillline":
            _, x, y, line = op
            _draw_skill_line(c, x, y, line)
        elif kind == "divider":
            c.setStrokeColor(C_RULE)
            c.setLineWidth(RULE_WIDTH)
            c.line(DIVIDER_X, 0.0, DIVIDER_X, PAGE_H)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown draw op: {kind!r}")


def _check_bullet_limits(cv: CVData) -> None:
    """Fail early when the template's optional bullet limits are exceeded.

    ``0`` means "no limit" (the shipped ``classic`` value): the geometry then
    decides, and a too-dense CV is caught by the overflow check below instead.
    A template that sets a limit gets a clear error before anything is drawn.
    """
    if MAX_BULLETS_PER_ENTRY:
        for entry in cv.experience:
            if len(entry.bullets) > MAX_BULLETS_PER_ENTRY:
                raise LayoutOverflowError(
                    "experience", float(len(entry.bullets)), float(MAX_BULLETS_PER_ENTRY),
                    hint=(
                        f"template {NAME!r} allows at most {MAX_BULLETS_PER_ENTRY} "
                        f"bullet(s) per entry ({entry.role!r} has {len(entry.bullets)})"
                    ),
                )
    if MAX_BULLETS_TOTAL:
        total = sum(len(entry.bullets) for entry in cv.experience)
        if total > MAX_BULLETS_TOTAL:
            raise LayoutOverflowError(
                "experience", float(total), float(MAX_BULLETS_TOTAL),
                hint=(
                    f"template {NAME!r} allows at most {MAX_BULLETS_TOTAL} bullet(s) "
                    f"in total (this CV has {total})"
                ),
            )


def _render_classic(cv: CVData, options: LayoutOptions) -> bytes:
    """Implementation of the ``classic`` layout (see :class:`ClassicLayout`)."""
    _check_bullet_limits(cv)
    resolved, crop = options.photo_for(cv)
    photo_image = None
    if resolved is not None:
        photo_image = to_jpeg_image(load_photo(resolved, crop))

    sidebar_ops = _build_sidebar_ops(cv)
    main_ops = _build_main_ops(cv)

    buf = BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"CV {cv.first_name} {cv.last_name}")
    c.setAuthor("")
    if photo_image is not None:
        c.drawImage(
            ImageReader(photo_image),
            0,
            _t(PHOTO_SIZE),
            width=PHOTO_SIZE,
            height=PHOTO_SIZE,
        )
    _execute(c, sidebar_ops)
    _execute(c, main_ops)
    c.showPage()
    c.save()
    return buf.getvalue()


class ClassicLayout:
    """The reference layout: photo sidebar, rules, one A4 page.

    ``name`` and ``description`` come from ``template.toml``; the name must be
    unique in the registry (``classic`` is the shipped one).  See
    :mod:`cv_generator.layouts` for the layout contract.
    """

    name = NAME
    description = DESCRIPTION

    def render(self, cv: CVData, options: LayoutOptions) -> bytes:
        """Render ``cv``; raise :class:`LayoutOverflowError` when it does not fit."""
        return _render_classic(cv, options)


#: The layout the template scan picks up (see :mod:`cv_generator.layouts`).
LAYOUT = ClassicLayout()
#: Kept for callers that imported the classic layout by its old name.
CLASSIC_LAYOUT = LAYOUT
# ``origin`` names the template the layout came from.  The template scan
# recognises its own registration by identity, so this module-level call and the
# scan do not fight over the name.
register_layout(LAYOUT, origin=f"template {NAME}")

__all__ = [
    "CLASSIC_LAYOUT",
    "ClassicLayout",
    "LAYOUT",
    "CONTENT_BOTTOM_NO_SIGNATURE",
    "CONTENT_BOTTOM_WITH_SIGNATURE",
    "DESCRIPTION",
    "NAME",
    "PAGE_H",
    "PAGE_W",
]
