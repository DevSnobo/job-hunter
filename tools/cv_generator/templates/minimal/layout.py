"""The ``minimal`` template: a plain single-column A4 CV, no sidebar, no photo.

A *template* is a directory with ``layout.py`` (this file) and
``template.toml`` (the pure values).  This one is deliberately small - single
column, no photo, no icons - so it is the easiest starting point for your own
template: copy the directory into one of your ``template_dirs``, change the
values in ``template.toml``, and keep the three contract items below.

What the layout contract asks for
---------------------------------
1. ``LAYOUT``: a layout instance - or a zero-argument factory returning one -
   with a unique ``name`` and a one-line ``description``.  The generator's
   registry collects it and ``cvgen layouts`` lists it.
2. ``render(cv, options) -> bytes``: the PDF bytes for a validated
   :class:`~cv_generator.schema.CVData`.
3. Raise :class:`~cv_generator.layouts.LayoutOverflowError` when the content
   does not fit one page.  The CLI reports that as a clean error and writes
   nothing, so a caller never receives a clipped or half-written PDF.

A layout that cannot draw photos simply ignores ``options``; this template does
exactly that (no photo, no ``cv.photo`` lookup), which is why it renders the
shipped fixtures without a profile picture.  A layout must never print to
stdout - the CLI owns stdout.

Every pure value below comes from ``template.toml``.  The relational values (the
gaps that make one section follow another) are template values too here, because
this layout has no reflow rules to preserve.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as pdfcanvas

from cv_generator.layouts import LayoutOptions, LayoutOverflowError, register_layout
from cv_generator.schema import CVData
from cv_generator.template import read_template_toml, value

_TEMPLATE_FILE = Path(__file__).resolve().with_name("template.toml")
_VALUES = read_template_toml(_TEMPLATE_FILE)


def _v(section: str, key: str):
    """One required value from ``template.toml`` (clear error when missing)."""
    return value(_VALUES, section, key, where=str(_TEMPLATE_FILE))


NAME = str(_VALUES.get("name", "minimal"))
DESCRIPTION = str(_VALUES.get("description", "Plain single-column A4 CV (no sidebar, no photo)."))

PAGE_W = float(_v("page", "width"))
PAGE_H = float(_v("page", "height"))

C_RULE = HexColor(_v("palette", "rule"))
C_HEADING = HexColor(_v("palette", "heading"))
C_BODY = HexColor(_v("palette", "body"))
C_MUTED = HexColor(_v("palette", "muted"))

FONT = str(_v("fonts", "regular"))
FONT_BOLD = str(_v("fonts", "bold"))

MARGIN_TOP = float(_v("geometry", "margin_top"))
MARGIN_BOTTOM = float(_v("geometry", "margin_bottom"))
MARGIN_LEFT = float(_v("geometry", "margin_left"))
MARGIN_RIGHT = float(_v("geometry", "margin_right"))
BULLET_INDENT = float(_v("geometry", "bullet_indent"))
BULLET_TEXT_INDENT = float(_v("geometry", "bullet_text_indent"))
RULE_GAP = float(_v("geometry", "rule_gap"))
HEADING_GAP = float(_v("geometry", "heading_gap"))
SECTION_GAP = float(_v("geometry", "section_gap"))
ENTRY_GAP = float(_v("geometry", "entry_gap"))
SIGNATURE_GAP = float(_v("geometry", "signature_gap"))

NAME_SIZE = float(_v("typography", "name_size"))
HEADLINE_SIZE = float(_v("typography", "headline_size"))
HEADING_SIZE = float(_v("typography", "heading_size"))
BODY_SIZE = float(_v("typography", "body_size"))
LEADING = float(_v("typography", "leading"))

MAX_BULLETS_PER_ENTRY = int(_v("limits", "max_bullets_per_entry"))
MAX_BULLETS_TOTAL = int(_v("limits", "max_bullets_total"))

RIGHT_EDGE = PAGE_W - MARGIN_RIGHT
TEXT_WIDTH = RIGHT_EDGE - MARGIN_LEFT


def _t(y_top: float) -> float:
    """Convert a top-down baseline to ReportLab's bottom-up coordinate."""
    return PAGE_H - y_top


def _lines(text: str, font: str, size: float, width: float) -> list[str]:
    """Wrap ``text``; an unbreakable over-long word is a layout error."""
    lines = simpleSplit(text, font, size, width) or [""]
    for line in lines:
        if pdfmetrics.stringWidth(line, font, size) > width + 0.5:
            raise LayoutOverflowError(
                "text", 0.0, 0.0,
                hint=f"a single word cannot fit in {width:.1f}pt: {line!r}",
            )
    return lines


def _check_bullet_limits(cv: CVData) -> None:
    """Optional hard limits from ``template.toml`` (0 means "no limit")."""
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


def _build_ops(cv: CVData) -> list:
    """Everything to draw, in order; overflow raises before anything is drawn.

    Op forms: ``("text", x, y_top, text, font, size, color, align)``,
    ``("rule", y_top, x0, x1)``.
    """
    _check_bullet_limits(cv)
    ops: list = []

    bottom = PAGE_H - MARGIN_BOTTOM - (SIGNATURE_GAP + 2 * LEADING if cv.signature else 0.0)

    y = MARGIN_TOP + NAME_SIZE
    ops.append(("text", MARGIN_LEFT, y, f"{cv.first_name} {cv.last_name}",
                FONT_BOLD, NAME_SIZE, C_HEADING, "left"))
    y += HEADLINE_SIZE + 6.0
    for line in _lines(cv.headline, FONT, HEADLINE_SIZE, TEXT_WIDTH):
        ops.append(("text", MARGIN_LEFT, y, line, FONT, HEADLINE_SIZE, C_MUTED, "left"))
        y += LEADING

    if cv.contacts:
        y += 4.0
        contacts = "  |  ".join(" - ".join(contact.lines) for contact in cv.contacts)
        for line in _lines(contacts, FONT, BODY_SIZE, TEXT_WIDTH):
            ops.append(("text", MARGIN_LEFT, y, line, FONT, BODY_SIZE, C_MUTED, "left"))
            y += LEADING
    y += SECTION_GAP

    def heading(region: str, label: str) -> None:
        """Draw one section heading + rule; overflow is attributed to ``region``."""
        nonlocal y
        if y > bottom:
            raise LayoutOverflowError(region, y, bottom)
        ops.append(("text", MARGIN_LEFT, y, label, FONT_BOLD, HEADING_SIZE, C_HEADING, "left"))
        ops.append(("rule", y + RULE_GAP, MARGIN_LEFT, RIGHT_EDGE))
        y += HEADING_GAP

    if cv.experience:
        heading("experience", cv.section_label("experience"))
        for index, entry in enumerate(cv.experience):
            if index:
                y += ENTRY_GAP
            for line in _lines(entry.role, FONT_BOLD, BODY_SIZE, TEXT_WIDTH):
                ops.append(("text", MARGIN_LEFT, y, line, FONT_BOLD, BODY_SIZE, C_HEADING, "left"))
                y += LEADING
            for line in _lines(entry.meta, FONT, BODY_SIZE, TEXT_WIDTH):
                ops.append(("text", MARGIN_LEFT, y, line, FONT, BODY_SIZE, C_BODY, "left"))
                y += LEADING
            for bullet in entry.bullets:
                for i, line in enumerate(_lines(bullet, FONT, BODY_SIZE, TEXT_WIDTH - BULLET_TEXT_INDENT)):
                    if i == 0:
                        ops.append(("text", MARGIN_LEFT + BULLET_INDENT, y, "\u2022",
                                    FONT, BODY_SIZE, C_BODY, "left"))
                    ops.append(("text", MARGIN_LEFT + BULLET_TEXT_INDENT, y, line,
                                FONT, BODY_SIZE, C_BODY, "left"))
                    y += LEADING
            if y > bottom:
                raise LayoutOverflowError("experience", y, bottom)
        y += SECTION_GAP

    if cv.education:
        heading("education", cv.section_label("education"))
        for index, edu in enumerate(cv.education):
            if index:
                y += ENTRY_GAP
            for line in _lines(edu.title, FONT_BOLD, BODY_SIZE, TEXT_WIDTH):
                ops.append(("text", MARGIN_LEFT, y, line, FONT_BOLD, BODY_SIZE, C_HEADING, "left"))
                y += LEADING
            if edu.dates:
                ops.append(("text", MARGIN_LEFT, y, edu.dates, FONT, BODY_SIZE, C_BODY, "left"))
                y += LEADING
            if y > bottom:
                raise LayoutOverflowError("education", y, bottom)
        y += SECTION_GAP

    if cv.certificates:
        heading("certificates", cv.section_label("certificates"))
        for cert in cv.certificates:
            for line in _lines(cert.title, FONT, BODY_SIZE, TEXT_WIDTH - BULLET_TEXT_INDENT):
                ops.append(("text", MARGIN_LEFT + BULLET_INDENT, y, "\u2022",
                            FONT, BODY_SIZE, C_BODY, "left"))
                ops.append(("text", MARGIN_LEFT + BULLET_TEXT_INDENT, y, line,
                            FONT, BODY_SIZE, C_BODY, "left"))
                y += LEADING
            if y > bottom:
                raise LayoutOverflowError("certificates", y, bottom)
        y += SECTION_GAP

    if cv.languages:
        heading("languages", cv.section_label("languages"))
        for lang in cv.languages:
            for line in _lines(f"{lang.name} - {lang.level}", FONT, BODY_SIZE, TEXT_WIDTH):
                ops.append(("text", MARGIN_LEFT, y, line, FONT, BODY_SIZE, C_BODY, "left"))
                y += LEADING
            if y > bottom:
                raise LayoutOverflowError("languages", y, bottom)
        y += SECTION_GAP

    if cv.skills:
        heading("skills", cv.section_label("skills"))
        for group in cv.skills:
            value_text = "".join(segment.text for segment in group.segments)
            for line in _lines(f"{group.label} {value_text}".strip(), FONT, BODY_SIZE, TEXT_WIDTH):
                ops.append(("text", MARGIN_LEFT, y, line, FONT, BODY_SIZE, C_BODY, "left"))
                y += LEADING
            if y > bottom:
                raise LayoutOverflowError("skills", y, bottom)

    if cv.signature:
        ops.append(("text", RIGHT_EDGE, PAGE_H - MARGIN_BOTTOM - LEADING,
                    cv.signature.place_date, FONT, BODY_SIZE, C_BODY, "right"))
        ops.append(("text", RIGHT_EDGE, PAGE_H - MARGIN_BOTTOM,
                    cv.signature.name, FONT_BOLD, BODY_SIZE, C_HEADING, "right"))

    return ops


def _render(cv: CVData, options: LayoutOptions) -> bytes:
    """Implementation of the ``minimal`` layout (``options`` is ignored: no photo)."""
    ops = _build_ops(cv)

    buf = BytesIO()
    canvas = pdfcanvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    canvas.setTitle(f"CV {cv.first_name} {cv.last_name}")
    canvas.setAuthor("")
    for op in ops:
        if op[0] == "text":
            _, x, y_top, text, font, size, color, align = op
            canvas.setFont(font, size)
            canvas.setFillColor(color)
            if align == "right":
                x -= pdfmetrics.stringWidth(text, font, size)
            canvas.drawString(x, _t(y_top), text)
        elif op[0] == "rule":
            _, y_top, x0, x1 = op
            canvas.setStrokeColor(C_RULE)
            canvas.setLineWidth(0.5)
            canvas.line(x0, _t(y_top), x1, _t(y_top))
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown draw op: {op[0]!r}")
    canvas.showPage()
    canvas.save()
    return buf.getvalue()


class MinimalLayout:
    """Plain single-column A4 layout: no sidebar, no photo, no icons.

    ``name`` and ``description`` come from ``template.toml``.
    """

    name = NAME
    description = DESCRIPTION

    def render(self, cv: CVData, options: LayoutOptions) -> bytes:
        """Render ``cv``; raise :class:`LayoutOverflowError` when it does not fit."""
        return _render(cv, options)


LAYOUT = MinimalLayout()
# ``origin`` names the template this layout came from; the template scan
# recognises its own registration by identity.
register_layout(LAYOUT, origin=f"template {NAME}")

__all__ = ["LAYOUT", "MinimalLayout"]
