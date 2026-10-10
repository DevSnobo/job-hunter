"""Aggregate Sankey chart: a generator-ready node/link file and a rendered image.

Everything here is drawn from the aggregate edges alone, so the artifacts carry no
company name, role, application id, url, path, or date. Rendering is done in this
module instead of a charting library: a Sankey is a small, fully specified drawing,
so every byte of the SVG is ours. No library banner, build stamp, source path, or
metadata block can end up in a file the tool calls shareable, and the same edges
always produce the same bytes.

The SVG is the canonical image. The PNG is an optional rasterization of that SVG
through a converter that is already on the machine (rsvg-convert, inkscape, or
ImageMagick).
"""
from __future__ import annotations

import contextlib
import errno
import math
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# drawing constants
# --------------------------------------------------------------------------
CANVAS_WIDTH = 1360
MARGIN_LEFT = 250
MARGIN_RIGHT = 280
MARGIN_TOP = 104
MARGIN_BOTTOM = 64
MIN_CANVAS_HEIGHT = 560
CONTENT_HEIGHT = 620          # the tallest column aims for this many pixels
NODE_WIDTH = 16.0
NODE_GAP = 18.0               # vertical gap between the nodes of one column
MIN_NODE_HEIGHT = 4.0
MIN_UNIT = 3.0                # pixels per application; the canvas grows below it
MAX_CANVAS_HEIGHT = 2600.0    # hard bound: the canvas grows to here, then bands thin out
MAX_UNIT = 30.0
SIDE_GAP = 12.0               # gap between a node and its side label
LABEL_SLACK = 22.0            # extra room after the widest side label
MIN_DRAWING_WIDTH = 300.0     # the columns themselves never get narrower than this
CURVE = 0.4                   # bezier control point as a fraction of the x span
MAX_STEP = 320.0              # widest useful column gap: a 2-column chart stays compact
MIN_STEP = 170.0              # narrowest column gap that still fits a middle label
RELAXATION_SWEEPS = 8

FONT_FAMILY = "DejaVu Sans,Segoe UI,Helvetica,Arial,sans-serif"
INK = "#0f172a"
MUTED = "#475569"
FAINT = "#94a3b8"

#: The column each node belongs to. A Sankey needs a left-to-right stage per node.
#: Completed gates come first, then the offer, then everything that is merely booked
#: (a booked round is the newest thing that happened, so it sits just before the
#: outcome), then the outcomes themselves.
STAGE_OF = {
    "all_applications": 0,
    "screen_done": 1,
    "assessment_done": 1,
    "interview_1": 2,
    "interview_2": 3,
    "interview_3plus": 4,
    "offer_received": 5,
    "booked_screening": 6,
    "booked_interview": 6,
    "booked_assessment": 6,
    "end_in_progress": 7,
    "end_awaiting_decision": 7,
    "end_accepted": 7,
    "end_rejected": 7,
    "end_withdrawn": 7,
    "end_offer_declined": 7,
    "end_position_closed": 7,
    "end_inactive": 7,
}

#: Nodes for work that is arranged but not done. They are drawn dashed and pale, so
#: the picture separates "this happened" from "this is booked".
PENDING_NODES = frozenset({"booked_screening", "booked_interview", "booked_assessment"})

#: Node colours, stable per node id, so one colour never means two things.
COLORS = {
    "all_applications": "#334155",
    "screen_done": "#0ea5e9",
    "assessment_done": "#7c3aed",
    "booked_screening": "#38bdf8",
    "booked_interview": "#6366f1",
    "booked_assessment": "#a78bfa",
    "interview_1": "#2563eb",
    "interview_2": "#3b82f6",
    "interview_3plus": "#60a5fa",
    "offer_received": "#0d9488",
    "end_in_progress": "#94a3b8",
    "end_awaiting_decision": "#f59e0b",
    "end_accepted": "#16a34a",
    "end_rejected": "#dc2626",
    "end_withdrawn": "#f97316",
    "end_offer_declined": "#a855f7",
    "end_position_closed": "#64748b",
    "end_inactive": "#db2777",
}
DEFAULT_COLOR = "#64748b"

#: Compact chart labels. The metadata keeps the long names.
SHORT_LABELS = {
    "screen_done": "Screening",
    "assessment_done": "Assessment",
    "interview_1": "Interview 1",
    "interview_2": "Interview 2",
    "interview_3plus": "Interview 3+",
    "offer_received": "Offer received",
}

#: Local converters, best first. Each template gets the svg path, png path, and width.
RASTERIZERS = (
    ("rsvg-convert", ("--width", "{width}", "--output", "{png}", "{svg}")),
    ("inkscape", ("--export-type=png", "--export-width={width}",
                  "--export-filename={png}", "{svg}")),
    ("magick", ("-background", "white", "-density", "144", "{svg}", "-resize",
                "{width}x", "{png}")),
    ("convert", ("-background", "white", "-density", "144", "{svg}", "-resize",
                 "{width}x", "{png}")),
)
PNG_SCALE = 2
RASTERIZE_TIMEOUT = 180       # seconds, per converter


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
@dataclass
class NodeBox:
    id: str
    label: str
    stage: int
    column: int
    total: int
    x: float
    y: float
    height: float
    color: str
    pending: bool = False


@dataclass
class LinkBox:
    source: str
    target: str
    value: int
    span: int
    x0: float
    y0: float
    x1: float
    y1: float
    thickness: float
    color_from: str
    color_to: str


@dataclass
class Chart:
    nodes: list[NodeBox] = field(default_factory=list)
    links: list[LinkBox] = field(default_factory=list)
    width: float = float(CANVAS_WIDTH)
    height: float = float(MIN_CANVAS_HEIGHT)
    unit: float = MIN_UNIT


def _node_height(value: int, unit: float) -> float:
    return max(value * unit, MIN_NODE_HEIGHT)


def _label_px(text: str, size: int = 13) -> float:
    """Advance width estimate for one label. Only used to reserve room for it.

    A rough estimate is enough: it decides the side margins, and every side keeps a
    slack of LABEL_SLACK pixels on top of it.
    """
    return len(text) * size * 0.58


def _clean(nodes: Sequence[tuple[str, str]], edges: Mapping[tuple[str, str], int]):
    """Drop the impossible: unknown node ids, self-edges, and non-positive flows."""
    labels = dict(nodes)
    kept = [
        (source, target, int(value))
        for (source, target), value in sorted(edges.items())
        if source in labels and target in labels and source != target and int(value) > 0
    ]
    return labels, kept


def _totals(labels: Mapping[str, str], kept: Sequence[tuple[str, str, int]]) -> dict[str, int]:
    in_total = {node_id: 0 for node_id in labels}
    out_total = {node_id: 0 for node_id in labels}
    for source, target, value in kept:
        out_total[source] += value
        in_total[target] += value
    return {node_id: max(in_total[node_id], out_total[node_id]) for node_id in labels}


def _columns(totals: Mapping[str, int]) -> list[list[str]]:
    """Group the nodes that carry flow into columns, skipping stages that are empty."""
    stages: dict[int, list[str]] = {}
    for node_id, total in totals.items():
        if total > 0:
            stages.setdefault(STAGE_OF.get(node_id, 0), []).append(node_id)
    return [stages[stage] for stage in sorted(stages)]


def _stack(order: Sequence[str], totals: Mapping[str, int], unit: float,
           avail: float) -> dict[str, float]:
    """Place one column: proportional heights, fixed gaps, vertically centered."""
    heights = [_node_height(totals[node_id], unit) for node_id in order]
    used = sum(heights) + NODE_GAP * max(0, len(order) - 1)
    cursor = MARGIN_TOP + max(0.0, (avail - used) / 2.0)
    placed: dict[str, float] = {}
    for node_id, height in zip(order, heights):
        placed[node_id] = cursor
        cursor += height + NODE_GAP
    return placed


def _relax(columns: list[list[str]], kept: Sequence[tuple[str, str, int]],
           totals: Mapping[str, int], unit: float) -> list[list[str]]:
    """Order every column by the barycenter of its neighbours to reduce crossings."""
    for sweep in range(RELAXATION_SWEEPS):
        positions: dict[str, float] = {}
        for column in columns:
            positions.update(_stack(column, totals, unit, CONTENT_HEIGHT))
        center = {node_id: positions[node_id] + _node_height(totals[node_id], unit) / 2.0
                  for node_id in positions}
        forward = sweep % 2 == 0
        for column_index, node_ids in enumerate(columns):
            if (forward and column_index == 0) or (not forward and column_index == len(columns) - 1):
                continue
            weight: dict[str, list[tuple[float, int]]] = {node_id: [] for node_id in node_ids}
            for source, target, value in kept:
                near, far = (source, target) if forward else (target, source)
                if far in weight and near in center:
                    weight[far].append((center[near], value))
            scored = []
            for index, node_id in enumerate(node_ids):
                pairs = weight[node_id]
                if pairs:
                    total = sum(value for _position, value in pairs)
                    score = sum(position * value for position, value in pairs) / total
                else:
                    score = center[node_id]
                scored.append((round(score, 6), index, node_id))
            columns[column_index] = [node_id for _score, _index, node_id in sorted(scored)]
    return columns


def build_chart(nodes: Sequence[tuple[str, str]],
                edges: Mapping[tuple[str, str], int]) -> Chart:
    """Lay out the aggregate. ``nodes`` is the declared (id, label) order."""
    labels, kept = _clean(nodes, edges)
    totals = _totals(labels, kept)
    columns = _columns(totals)
    if not columns:
        return Chart()

    widest = max(sum(totals[node_id] for node_id in column) for column in columns)
    gaps = max(NODE_GAP * (len(column) - 1) for column in columns)
    unit = max(MIN_UNIT, min(MAX_UNIT, CONTENT_HEIGHT / max(1, widest)))
    budget = MAX_CANVAS_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    if unit * widest + gaps > budget:
        # A taller canvas beats hairlines, but only up to MAX_CANVAS_HEIGHT: past that a
        # file nobody can open is worse than a thin band, so the cap wins.
        unit = min(unit, max(0.001, (budget - gaps) / max(1, widest)))
    columns = _relax(columns, kept, totals, unit)

    heights = {node_id: _node_height(totals[node_id], unit) for node_id in totals}
    used_by_column = [
        sum(heights[node_id] for node_id in column) + NODE_GAP * max(0, len(column) - 1)
        for column in columns
    ]
    height = float(math.ceil(max(float(MIN_CANVAS_HEIGHT),
                                 MARGIN_TOP + max(used_by_column) + MARGIN_BOTTOM)))
    avail = height - MARGIN_TOP - MARGIN_BOTTOM

    y: dict[str, float] = {}
    for column in columns:
        y.update(_stack(column, totals, unit, avail))

    # Side labels must never run off the canvas: the outer columns get margins that
    # fit their own text, and the canvas grows if those margins need the room.
    def widest(column: Sequence[str]) -> float:
        return max(_label_px(f"{labels[node_id]} ({totals[node_id]})") for node_id in column)

    left_margin = max(MARGIN_LEFT, widest(columns[0]) + SIDE_GAP + LABEL_SLACK)
    right_margin = max(MARGIN_RIGHT,
                       widest(columns[-1]) + NODE_WIDTH + SIDE_GAP + LABEL_SLACK)
    # The canvas also widens for the stages: a middle-column label needs its column to
    # be wide enough, or the labels of neighbouring stages run into each other.
    canvas_width = float(math.ceil(max(float(CANVAS_WIDTH),
                                      left_margin + right_margin + MIN_DRAWING_WIDTH,
                                      left_margin + right_margin + MIN_STEP * (len(columns) - 1))))
    span = canvas_width - left_margin - right_margin
    step = min(span / (len(columns) - 1), MAX_STEP) if len(columns) > 1 else 0.0
    # A narrow pipeline stays compact and centered instead of smearing two columns
    # across the whole canvas.
    left = left_margin + max(0.0, (span - step * (len(columns) - 1)) / 2.0)
    x = {node_id: left + index * step
         for index, column in enumerate(columns) for node_id in column}
    column_of = {node_id: index for index, column in enumerate(columns) for node_id in column}
    position_of = {node_id: index for column in columns for index, node_id in enumerate(column)}

    boxes = [
        NodeBox(id=node_id, label=labels[node_id], stage=STAGE_OF.get(node_id, 0),
                column=column_of[node_id], total=totals[node_id], x=x[node_id], y=y[node_id],
                height=heights[node_id], color=COLORS.get(node_id, DEFAULT_COLOR),
                pending=node_id in PENDING_NODES)
        for column in columns for node_id in column
    ]
    box_by_id = {box.id: box for box in boxes}

    outgoing: dict[str, list[tuple[str, str, int]]] = {node_id: [] for node_id in labels}
    incoming: dict[str, list[tuple[str, str, int]]] = {node_id: [] for node_id in labels}
    for source, target, value in kept:
        outgoing[source].append((source, target, value))
        incoming[target].append((source, target, value))
    # Port order inside a node follows the partner node's height on the canvas, which
    # is what keeps the bands flat: a link leaves near the height it arrives at.
    center = {node_id: y[node_id] + heights[node_id] / 2.0 for node_id in y}
    for node_id in outgoing:
        outgoing[node_id].sort(key=lambda item: (center[item[1]], item[1]))
    for node_id in incoming:
        incoming[node_id].sort(key=lambda item: (center[item[0]], item[0]))

    out_cursor: dict[str, float] = {}
    in_cursor: dict[str, float] = {}
    for box in boxes:
        out_sum = sum(value for _s, _t, value in outgoing[box.id]) * unit
        in_sum = sum(value for _s, _t, value in incoming[box.id]) * unit
        out_cursor[box.id] = box.y + max(0.0, (box.height - out_sum) / 2.0)
        in_cursor[box.id] = box.y + max(0.0, (box.height - in_sum) / 2.0)

    top_left: dict[tuple[str, str], float] = {}
    top_right: dict[tuple[str, str], float] = {}
    for box in boxes:
        for source, target, value in outgoing[box.id]:
            top_left[(source, target)] = out_cursor[box.id]
            out_cursor[box.id] += value * unit
        for source, target, value in incoming[box.id]:
            top_right[(source, target)] = in_cursor[box.id]
            in_cursor[box.id] += value * unit

    links: list[LinkBox] = []
    for source, target, value in kept:
        source_box = box_by_id[source]
        target_box = box_by_id[target]
        links.append(LinkBox(
            source=source, target=target, value=value,
            span=column_of[target] - column_of[source],
            x0=source_box.x + NODE_WIDTH, y0=top_left[(source, target)],
            x1=target_box.x, y1=top_right[(source, target)],
            thickness=value * unit,
            color_from=source_box.color, color_to=target_box.color,
        ))
    # long ribbons first: the short ones then stay on top of them
    links.sort(key=lambda link: (-link.span, column_of[link.source], position_of[link.source],
                                 column_of[link.target], position_of[link.target]))
    return Chart(nodes=boxes, links=links, width=canvas_width, height=height, unit=unit)


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------
def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-") else text


def _ribbon(link: LinkBox) -> str:
    """One flow band: forward along the top edge, back along the bottom edge."""
    control = max(8.0, (link.x1 - link.x0) * CURVE)
    bottom = link.thickness
    return (
        f"M {_num(link.x0)} {_num(link.y0)}"
        f" C {_num(link.x0 + control)} {_num(link.y0)}"
        f" {_num(link.x1 - control)} {_num(link.y1)}"
        f" {_num(link.x1)} {_num(link.y1)}"
        f" L {_num(link.x1)} {_num(link.y1 + bottom)}"
        f" C {_num(link.x1 - control)} {_num(link.y1 + bottom)}"
        f" {_num(link.x0 + control)} {_num(link.y0 + bottom)}"
        f" {_num(link.x0)} {_num(link.y0 + bottom)} Z"
    )


def _label_anchor(box: NodeBox, last_column: int) -> tuple[str, float, float, int, str]:
    """First column labels left, last column labels right, the rest above."""
    if box.column == 0:
        return "end", box.x - SIDE_GAP, box.y + box.height / 2.0 + 4.0, 13, "600"
    if box.column == last_column:
        return "start", box.x + NODE_WIDTH + SIDE_GAP, box.y + box.height / 2.0 + 4.0, 13, "600"
    return "middle", box.x + NODE_WIDTH / 2.0, box.y - 5.0, 12, "500"


def render_svg(chart: Chart, *, title: str, subtitle: str, note: str = "") -> str:
    """Render the chart as a self-contained SVG. No external reference of any kind."""
    height = chart.height
    last_column = max((box.column for box in chart.nodes), default=0)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(chart.width)}"'
        f' height="{_num(height)}" viewBox="0 0 {_num(chart.width)} {_num(height)}"'
        f' role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        f'<rect x="0" y="0" width="{_num(chart.width)}" height="{_num(height)}" fill="#ffffff"/>',
    ]
    if chart.links:
        gradients = "".join(
            f'<linearGradient id="flow-{index}" gradientUnits="userSpaceOnUse"'
            f' x1="{_num(link.x0)}" y1="0" x2="{_num(link.x1)}" y2="0">'
            f'<stop offset="0" stop-color="{link.color_from}"/>'
            f'<stop offset="1" stop-color="{link.color_to}"/></linearGradient>'
            for index, link in enumerate(chart.links)
        )
        parts.append(f"<defs>{gradients}</defs>")

    parts.append('<g fill-opacity="0.45">')
    for index, link in enumerate(chart.links):
        parts.append(
            f'<path d="{_ribbon(link)}" fill="url(#flow-{index})">'
            f"<title>{escape(f'{link.source} to {link.target}: {link.value}')}</title></path>"
        )
    parts.append("</g>")

    parts.append("<g>")
    for box in chart.nodes:
        pending = (' fill-opacity="0.25" stroke="' + box.color
                   + '" stroke-width="1.5" stroke-dasharray="4 3"') if box.pending else ""
        parts.append(
            f'<rect x="{_num(box.x)}" y="{_num(box.y)}" width="{_num(NODE_WIDTH)}"'
            f' height="{_num(box.height)}" rx="2" fill="{box.color}"{pending}>'
            f"<title>{escape(f'{box.label}: {box.total}')}</title></rect>"
        )
    parts.append("</g>")

    # Node labels carry a white halo: a middle-column label sits over the flow bands,
    # and the halo keeps it readable on any colour underneath.
    parts.append(f'<g font-family="{FONT_FAMILY}">')
    for halo in (True, False):
        for box in chart.nodes:
            anchor, x, y, size, weight = _label_anchor(box, last_column)
            text = escape(f"{box.label} ({box.total})")
            paint = ('fill="#ffffff" stroke="#ffffff" stroke-width="3.5"'
                     ' stroke-linejoin="round"' if halo else f'fill="{INK}"')
            parts.append(
                f'<text x="{_num(x)}" y="{_num(y)}" text-anchor="{anchor}" font-size="{size}"'
                f' font-weight="{weight}" {paint}>{text}</text>'
            )
    parts.append("</g>")

    parts.append(
        f'<g font-family="{FONT_FAMILY}">'
        f'<text x="48" y="48" font-size="21" font-weight="700" fill="{INK}">{escape(title)}</text>'
        f'<text x="48" y="72" font-size="13" fill="{MUTED}">{escape(subtitle)}</text></g>'
    )
    if note:
        parts.append(
            f'<g font-family="{FONT_FAMILY}"><text x="{_num(chart.width - 48)}"'
            f' y="{_num(height - 22)}" text-anchor="end" font-size="11" fill="{FAINT}">'
            f"{escape(note)}</text></g>"
        )
    if not chart.nodes:
        parts.append(
            f'<g font-family="{FONT_FAMILY}" fill="{MUTED}">'
            f'<text x="{_num(chart.width / 2)}" y="{_num(height / 2)}" text-anchor="middle"'
            f' font-size="15">No applications in this cohort</text></g>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def node_link_rows(chart: Chart) -> tuple[list[dict], list[dict]]:
    """The generator-ready view: integer indices plus the stable node ids."""
    index_of = {box.id: position for position, box in enumerate(chart.nodes)}
    nodes = [
        {"index": position, "id": box.id, "label": box.label, "stage": box.stage,
         "column": box.column, "total": box.total, "pending": box.pending}
        for position, box in enumerate(chart.nodes)
    ]
    links = [
        {"source": index_of[link.source], "target": index_of[link.target], "value": link.value,
         "source_id": link.source, "target_id": link.target}
        for link in chart.links
    ]
    return nodes, links


# --------------------------------------------------------------------------
# optional rasterization
# --------------------------------------------------------------------------
#: A path start: a Windows drive, or any slash in either direction.
_PATH_LIKE = re.compile(r"(?:[A-Za-z]:[\\/]|[/\\])")
def _safe_detail(text: str, svg_path: Path, png_path: Path) -> str:
    """A converter message with every local path cut out.

    A rasterizer commonly prints the file it failed on. That path names the private
    export directory, and the note is stored in the shareable metadata, so it must
    never survive. Two rules, in order:

    1. the two staged paths, their file names, and their directory become a plain
       phrase (a path may contain spaces, which defeats a pattern that stops at a
       space);
    2. whatever still looks like a path truncates the message, because keeping the
       prefix is useful ("cannot open", "Failed to load XML:") while the tail is not.
       A Windows drive and a backslash count as a path too; a converter that names
       another tool's private folder in passing would otherwise survive.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    for path in (svg_path, png_path):
        for needle in (str(path), path.name):
            if needle:
                line = line.replace(needle, "the chart file")
        for needle in (str(path.parent), path.parent.name):
            if needle:  # the export directory, which the user named
                line = line.replace(needle, " ")
    cut = _PATH_LIKE.search(line)
    if cut:
        line = line[:cut.start()]
    tokens = [token for token in line.split() if "\\" not in token and "/" not in token]
    return " ".join(tokens)[:160]


def _run_converter(command: list[str]) -> tuple[int | None, str, str, bool]:
    """Run one converter under a hard time bound.

    Returns ``(exit_code, stdout, stderr, timed_out)``. The child gets its own
    session, so a timeout kills the whole process group: a converter that leaves a
    helper behind would otherwise hold the pipes open and block far past the timeout.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=RASTERIZE_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
            process.communicate(timeout=5)
        return None, "", "", True
    return process.returncode, stdout or "", stderr or "", False


def _errno_name(exc: OSError) -> str:
    """The errno name, never the exception text: the text can name a local path."""
    if exc.errno is not None:
        return errno.errorcode.get(exc.errno, f"errno {exc.errno}")
    return type(exc).__name__


def find_rasterizer() -> str | None:
    for name, _template in RASTERIZERS:
        found = shutil.which(name)
        if found:
            return found
    return None


def rasterize(svg_path: Path, png_path: Path,
              *, width: int | None = None) -> tuple[str | None, str | None]:
    """Convert the SVG to a PNG with a local converter.

    Returns ``(tool, note)``. ``tool`` is None when no converter is installed or the
    converter failed, and the note says why. Nothing here is fatal: the SVG is
    written first, so the image is never lost.
    """
    notes: list[str] = []
    for name, template in RASTERIZERS:
        found = shutil.which(name)
        if not found:
            continue
        command = [found] + [
            argument.format(svg=str(svg_path), png=str(png_path), width=width or 0)
            for argument in template
        ]
        with contextlib.suppress(OSError):
            Path(png_path).unlink()  # an earlier converter may have left a partial file
        try:
            code, out, err, timed_out = _run_converter(command)
        except OSError as exc:  # cannot exec it at all
            notes.append(f"{name} could not run ({_errno_name(exc)})")
            continue
        except subprocess.SubprocessError as exc:
            notes.append(f"{name} could not run ({type(exc).__name__})")
            continue
        if timed_out:
            notes.append(f"{name} did not finish within {RASTERIZE_TIMEOUT} seconds")
            continue
        if code == 0 and Path(png_path).exists():
            return name, None
        detail = _safe_detail(err or out, svg_path, png_path)
        notes.append(f"{name} failed (exit {code})" + (f": {detail}" if detail else ""))
    if notes:
        summary = notes[0]
        if len(notes) > 1:
            summary += f"; {len(notes) - 1} other converter(s) failed too"
        return None, summary + "; the SVG is still written"
    return None, ("no SVG converter found (install librsvg, inkscape, or ImageMagick to also"
                  " get a PNG); the SVG is still written")
