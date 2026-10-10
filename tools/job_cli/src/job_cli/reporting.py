"""Cutoff-aware reporting: statistics, private snapshots, the aggregate export bundle.

All reporting runs through the SQL in sql/queries/ so the queries can be read,
reviewed, and replayed. Python only aggregates the resulting rows.

One Sankey export is one timestamped bundle under exports/: the edge CSV and the
metadata always, plus the generator-ready node/link JSON and the rendered chart
(SVG, and PNG when a local converter exists). sankey_chart.py does the drawing, so
the layout and every byte of the image stay ours.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import secrets
import sqlite3
import statistics as _stats
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import db as dbmod
from . import repo
from . import sankey_chart
from .db import all_rows, read_query
from .errors import JobError, UsageError
from .paths import QUERIES_DIR, Layout, find_job_root

# Employer-origin events. An applicant action is NOT an employer response, so the
# four applicant-side events assessment_submitted, offer_accepted,
# offer_declined, and job_started are excluded, together with follow_up_sent,
# note, withdrawn, marked_inactive, and reopened. Every event type in the schema
# is classified by these two sets; tests/test_reporting_safety.py pins that.
EMPLOYER_RESPONSE_EVENTS = frozenset(
    {
        "invited", "scheduled", "rescheduled", "completed", "cancelled",
        "assessment_received", "feedback_received",
        "offer_received", "rejected", "position_closed",
    }
)

APPLICANT_ACTION_EVENTS = frozenset(
    {
        "assessment_submitted", "offer_accepted", "offer_declined", "job_started",
        "withdrawn", "follow_up_sent", "note", "marked_inactive", "reopened",
    }
)

# Fixed shipped query used by replay. Replay never executes SQL text that the
# metadata file supplies.
SNAPSHOT_QUERY_NAME = "snapshot.sql"

# Frozen reconstruction data. One entry per observed chart shape with a count. The
# shapes are already collapsed to the published chart semantics (gates bucketed to
# 0/1, rounds 3+, status fold, at most one booked phase), so no per-application
# precision, order, or identifier can be recovered from them.
#
# v3 is what new exports emit: the shape also carries the completed screens and
# assessments and the phase that is merely booked. v2 (no screens, no assessments,
# no booked phase) and v1 (one tuple per application) are read-only: replay still
# reproduces them, but new exports never emit them.
FROZEN_FORMAT = "sankey-frozen-v3"
PREVIOUS_FROZEN_FORMAT = "sankey-frozen-v2"
LEGACY_FROZEN_FORMAT = "sankey-frozen-v1"
LEGACY_ROW_FIELDS = ("completed_interviews", "received_offer", "status")
GROUP_FIELDS = ("screens", "assessments", "interviews", "received_offer", "pending",
                "endpoint", "count")
PREVIOUS_GROUP_FIELDS = ("interviews", "received_offer", "endpoint", "count")
INTERVIEW_BUCKETS = (0, 1, 2, 3)  # 3 means "3 or more completed interviews"
GATE_BUCKETS = (0, 1)             # 1 means "at least one completed screening/assessment"

SANKEY_ENDPOINTS = OrderedDict([
    ("in_progress", ("end_in_progress", "Still in progress")),
    ("offer_pending", ("end_awaiting_decision", "Awaiting offer decision")),
    ("accepted", ("end_accepted", "Offer accepted")),
    ("started", ("end_accepted", "Offer accepted")),
    ("rejected", ("end_rejected", "Rejected")),
    ("withdrawn", ("end_withdrawn", "Withdrawn")),
    ("offer_declined", ("end_offer_declined", "Offer declined")),
    ("position_closed", ("end_position_closed", "Position closed")),
    ("inactive", ("end_inactive", "No response / presumed inactive")),
])

SANKEY_STATUS_FOLD = {"started": "accepted"}

SANKEY_NODES = OrderedDict([
    ("all_applications", "All applications"),
    ("screen_done", "Screening completed"),
    ("assessment_done", "Assessment completed"),
    ("interview_1", "Interview 1 completed"),
    ("interview_2", "Interview 2 completed"),
    ("interview_3plus", "Interview 3+ completed"),
    ("offer_received", "Offer received"),
    ("booked_screening", "Screening booked"),
    ("booked_interview", "Interview booked"),
    ("booked_assessment", "Assessment booked"),
    ("end_in_progress", "Still in progress"),
    ("end_awaiting_decision", "Awaiting offer decision"),
    ("end_accepted", "Offer accepted"),
    ("end_rejected", "Rejected"),
    ("end_withdrawn", "Withdrawn"),
    ("end_offer_declined", "Offer declined"),
    ("end_position_closed", "Position closed"),
    ("end_inactive", "No response / presumed inactive"),
])

#: Chart text. Fixed strings plus aggregate counts only, so a shareable artifact
#: never carries a company, role, link, path, or per-application date.
CHART_TITLE = "Job application pipeline"
CHART_NOTE = "Aggregate counts only: no company, role, link, or date"
NODE_LINK_FORMAT = "sankey-node-link-v1"

#: Generator hints shipped inside the node/link file, so the data can be plugged
#: into a chart tool without guessing what the fields mean.
GENERATOR_HINTS = {
    "plotly": ("nodes[i].label and links[j].source/target (integer indices into"
               " nodes) plus links[j].value map straight onto"
               " plotly.graph_objects.Sankey(node=..., link=...)."),
    "d3_sankey": "resolve links[j].source_id/target_id against nodes[i].id.",
    "echarts": "links[j].source_id/target_id become link source/target names.",
    "sankeymatic": ("use sankey-edges.csv as 'source [value] target' after replacing"
                    " the node ids with the nodes[i].label text."),
}

#: Every file one export writes. Used to refuse overwriting an earlier bundle.
SANKEY_ARTIFACTS = (
    "sankey-edges.csv",
    "sankey-metadata.json",
    "sankey-node-link.json",
    "sankey.svg",
    "sankey.png",
)

#: The schema's own round phases. A phase says what the conversation is for, not who
#: attends it: a screening is the filter call, an interview is where your work is
#: assessed, an assessment is a task instead of a conversation.
SANKEY_PHASES = ("screening", "interview", "assessment")

#: The node that shows one phase as arranged but not done.
BOOKED_NODES = {
    "screening": "booked_screening",
    "interview": "booked_interview",
    "assessment": "booked_assessment",
}

COUNTING_RULES = [
    "Cohort = applications with submitted_on between cohort_from and cohort_to (inclusive)"
    " and not after as_of.",
    "Only events with occurred_on <= as_of are counted.",
    "Screening = at least one completed screening-phase round; counted once per application.",
    "Assessment = at least one completed assessment-phase round or a submitted assessment,"
    " counted once per application.",
    "Interview n = the n-th completed interview round (phase = interview) ordered by the"
    " completion date.",
    "Round 3+ collapses to one node per application; no repeated self-edges are emitted.",
    "Booked <phase> = the newest round event at or before as_of is an invitation or a"
    " booking, and that round's phase is <phase>. At most one booked node per application,"
    " so the chart shows the next arranged step and never a list of appointments."
    " A booked node is drawn after every completed gate and after the offer, because it is"
    " the newest step in the flow.",
    "At most one offer_received node per application, however many offer events exist.",
    "Exactly one endpoint per application: the latest state-changing event at or before as_of.",
    "job_started is folded into Offer accepted for this first chart.",
]


def _params(as_of: str | None, cohort_from: str | None, cohort_to: str | None) -> dict:
    return {"as_of": as_of, "cohort_from": cohort_from, "cohort_to": cohort_to}


def load_snapshot_sql(layout: Layout) -> str:
    """Read the shipped snapshot query. Callers never supply SQL text."""
    return read_query(layout.query_file(SNAPSHOT_QUERY_NAME))


def run_snapshot(conn: sqlite3.Connection, layout: Layout, *, as_of: str | None = None,
                 cohort_from: str | None = None,
                 cohort_to: str | None = None) -> list[sqlite3.Row]:
    return all_rows(conn, load_snapshot_sql(layout), _params(as_of, cohort_from, cohort_to))


def run_cohort_events(conn: sqlite3.Connection, layout: Layout, *, as_of: str | None = None,
                      cohort_from: str | None = None,
                      cohort_to: str | None = None) -> list[sqlite3.Row]:
    return all_rows(conn, read_query(layout.query_file("cohort_events.sql")),
                    _params(as_of, cohort_from, cohort_to))


def upcoming(conn: sqlite3.Connection, layout: Layout, as_of: str) -> list[sqlite3.Row]:
    return all_rows(conn, read_query(layout.query_file("upcoming.sql")), {"as_of": as_of})


def stale(conn: sqlite3.Connection, layout: Layout, as_of: str,
          stale_days: int) -> list[sqlite3.Row]:
    return all_rows(conn, read_query(layout.query_file("stale.sql")),
                    {"as_of": as_of, "stale_days": stale_days})


def overview(conn: sqlite3.Connection, layout: Layout, *, search: str | None = None,
             status: str | None = None) -> list[sqlite3.Row]:
    pattern = f"%{search.lower()}%" if search else None
    return all_rows(conn, read_query(layout.query_file("application_overview.sql")),
                    {"status": status, "search": pattern})


# --------------------------------------------------------------------------
# counters below a printed overview table
# --------------------------------------------------------------------------
def _row_status(row) -> str:
    """The status of a status/overview row: views call it current_status."""
    keys = row.keys()
    return str(row["current_status"] if "current_status" in keys else row["status"])


def _row_id(row) -> str:
    """The application id of an overview row, whichever column carries it."""
    keys = row.keys()
    return str(row["id"] if "id" in keys else row["application_id"])


def employer_answered_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of the applications that have at least one employer-origin event.

    One row per answered application, so a printed table can be split into
    "the employer replied" and "still waiting" without a query per row. The
    event list is built from EMPLOYER_RESPONSE_EVENTS: that frozenset is the
    single source of truth for the classification, and a shipped .sql query
    would have to repeat the codes as SQL text where they could drift.
    """
    codes = sorted(EMPLOYER_RESPONSE_EVENTS)
    placeholders = ", ".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT DISTINCT application_id FROM events WHERE event_type IN ({placeholders})",
        codes,
    )
    return {str(row[0]) for row in rows}


def overview_summary(conn: sqlite3.Connection, rows) -> dict:
    """Counters for the application rows currently printed in a table.

    Total, a per-status count line, and the in_progress split: ``answered``
    applications are in progress and already have an employer-origin event (an
    interview or screening invitation counts), ``waiting`` ones have none yet.
    The counts follow the rows passed in, so they describe that table exactly.
    Rejected rows are counted like every other row. Pass every application
    (``tracker_summary``) when the counters must describe the whole tracker.
    """
    status_counts = Counter(_row_status(row) for row in rows)
    in_progress = status_counts.get("in_progress", 0)
    answered_ids = employer_answered_ids(conn)
    answered = sum(1 for row in rows
                   if _row_status(row) == "in_progress" and _row_id(row) in answered_ids)
    return {
        "total": len(rows),
        "status_counts": dict(sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))),
        "in_progress": in_progress,
        "rejected": status_counts.get("rejected", 0),
        "answered": answered,
        "waiting": in_progress - answered,
        # Every application with an employer-origin event: the renderer uses it to
        # colour the in_progress rows without asking the database per row.
        "answered_ids": answered_ids,
    }


def tracker_summary(conn: sqlite3.Connection, layout: Layout) -> dict:
    """The same counters for the whole tracker, whatever table is printed.

    Built from overview() + overview_summary() so the status counting and the
    employer-response rule stay in one place. A filtered printed table then still
    shows how many applications exist, how many are in progress (and how many of
    those the employer already answered), and how many were rejected, while the
    table itself shows only the rows that match.
    """
    return overview_summary(conn, overview(conn, layout))


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
def _days_between(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    return float(_stats.median(values))


def first_employer_response_days(submitted_on: str, events) -> int | None:
    """Days from submission to the first employer-origin event, or None.

    ``events`` only needs to support ``event["event_type"]`` and
    ``event["occurred_on"]`` (sqlite3.Row or a dict).
    """
    dates = [str(event["occurred_on"]) for event in events
             if event["event_type"] in EMPLOYER_RESPONSE_EVENTS]
    if not dates:
        return None
    return _days_between(submitted_on, min(dates))


def _excluded_event_types(conn: sqlite3.Connection) -> list[str]:
    """All event types in the schema that are not employer-origin responses."""
    try:
        codes = {str(row[0]) for row in conn.execute("SELECT code FROM event_types")}
    except sqlite3.Error:  # pragma: no cover - defensive
        codes = set()
    if not codes:
        return sorted(APPLICANT_ACTION_EVENTS)
    return sorted(codes - EMPLOYER_RESPONSE_EVENTS)


def statistics(conn: sqlite3.Connection, layout: Layout, *, as_of: str | None = None,
               cohort_from: str | None = None, cohort_to: str | None = None) -> dict:
    effective_as_of = as_of or dt.date.today().isoformat()
    rows = run_snapshot(conn, layout, as_of=effective_as_of, cohort_from=cohort_from,
                        cohort_to=cohort_to)
    events = run_cohort_events(conn, layout, as_of=effective_as_of, cohort_from=cohort_from,
                               cohort_to=cohort_to)

    by_app: dict[str, list[sqlite3.Row]] = {}
    submitted: dict[str, str] = {}
    for row in events:
        submitted[row["application_id"]] = row["submitted_on"]
        if row["event_type"] is None:
            continue
        by_app.setdefault(row["application_id"], []).append(row)

    status_counts = Counter(row["status"] for row in rows)
    total = len(rows)
    completed_interviews = sum(int(row["completed_interviews"]) for row in rows)
    completed_screens = sum(int(row["completed_screens"]) for row in rows)
    completed_assessments = sum(int(row["completed_assessments"]) for row in rows)
    offers = sum(1 for row in rows if int(row["received_offer"]))

    response_days: list[int] = []
    duration_days: list[int] = []
    terminal_states = set(repo.CLOSED_STATUSES) | {"accepted", "started"}
    for row in rows:
        app_id = str(row["id"])
        app_events = by_app.get(app_id, [])
        first_days = first_employer_response_days(str(row["submitted_on"]), app_events)
        if first_days is not None:
            response_days.append(first_days)
        state = str(row["status"])
        if state in terminal_states:
            state_events = [e for e in app_events if e["application_state"] is not None]
            if state_events:
                last = max(state_events, key=lambda e: (str(e["occurred_on"]), int(e["day_order"] or 0)))
                duration_days.append(_days_between(str(row["submitted_on"]), str(last["occurred_on"])))

    interviewed = sum(1 for row in rows if int(row["completed_interviews"]) > 0)
    still_open = status_counts.get("in_progress", 0) + status_counts.get("offer_pending", 0)
    return {
        "filters": {"as_of": effective_as_of, "cohort_from": cohort_from, "cohort_to": cohort_to},
        "total_applications": total,
        "status_counts": dict(sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))),
        "still_open": still_open,
        "completed_interviews": completed_interviews,
        "completed_screens": completed_screens,
        "completed_assessments": completed_assessments,
        "applications_with_interview": interviewed,
        "applications_with_offer": offers,
        "responses": {
            "applications_answered": len(response_days),
            "applications_without_employer_event": total - len(response_days),
            "first_response_median_days": _median(response_days),
            "first_response_mean_days": round(_stats.fmean(response_days), 2) if response_days else None,
            "excluded_event_types": _excluded_event_types(conn),
            "employer_response_event_types": sorted(EMPLOYER_RESPONSE_EVENTS),
            "applicant_action_event_types": sorted(APPLICANT_ACTION_EVENTS),
        },
        "duration": {
            "applications_with_terminal_state": len(duration_days),
            "median_days_to_terminal_state": _median(duration_days),
            "mean_days_to_terminal_state": round(_stats.fmean(duration_days), 2)
            if duration_days else None,
        },
        "denominators": {
            "interview_rate_denominator": total,
            "still_in_progress_note": f"{still_open} of {total} applications are still open",
        },
    }


# --------------------------------------------------------------------------
# private snapshot
# --------------------------------------------------------------------------
def write_snapshot(conn: sqlite3.Connection, layout: Layout, *, as_of: str | None = None,
                   cohort_from: str | None = None, cohort_to: str | None = None,
                   output: str | Path | None = None) -> Path:
    effective_as_of = as_of or dt.date.today().isoformat()
    sql = load_snapshot_sql(layout)
    rows = run_snapshot(conn, layout, as_of=effective_as_of, cohort_from=cohort_from,
                        cohort_to=cohort_to)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    target = Path(output).expanduser() if output else (layout.exports_dir / f"snapshot-{stamp}.json")
    if target.is_dir():
        target = target / f"snapshot-{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "private_snapshot",
        "warning": "PRIVATE: contains company names, roles, and recorded paths.",
        "generated_at": _now(),
        "db_path": str(layout.db_path),
        "filters": {"as_of": effective_as_of, "cohort_from": cohort_from, "cohort_to": cohort_to},
        "query_file": "sql/queries/snapshot.sql",
        "query_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "row_count": len(rows),
        "rows": [dict(row) for row in rows],
    }
    _atomic_write(target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return target


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


# --------------------------------------------------------------------------
# Sankey
# --------------------------------------------------------------------------
def _fold_status(status: str) -> str:
    """Fold a status to the single endpoint the published chart shows for it."""
    if status not in SANKEY_ENDPOINTS:
        raise UsageError(f"unknown application status '{status}' in the sankey mapping")
    return SANKEY_STATUS_FOLD.get(status, status)


def _pending_phase(value) -> str | None:
    """The phase that is arranged but not done, or None."""
    if value is None or value == "":
        return None
    if value not in SANKEY_PHASES:
        raise UsageError(f"unknown round phase '{value}' for a booked round")
    return str(value)


def shape_from_row(row) -> tuple:
    """One collapsed chart shape: the gates, the offer, the booked phase, the outcome."""
    return (
        min(int(row["completed_screens"]), GATE_BUCKETS[-1]),
        min(int(row["completed_assessments"]), GATE_BUCKETS[-1]),
        min(int(row["completed_interviews"]), INTERVIEW_BUCKETS[-1]),
        int(row["received_offer"]),
        _pending_phase(row["pending_phase"]),
        _fold_status(str(row["status"])),
    )


def path_from_row(row) -> list[str]:
    """Node ids for one application: gates, offer, the booked step, one endpoint."""
    return _path_from_shape(*shape_from_row(row))


def canonical_path(row) -> list[str]:
    """Node ids for one application. Kept as the public name for the path builder."""
    return path_from_row(row)


def _path_from_shape(screens: int, assessments: int, interviews: int, received_offer: int,
                     pending: str | None, endpoint: str) -> list[str]:
    """Node ids for one collapsed chart shape."""
    if endpoint not in SANKEY_ENDPOINTS:
        raise UsageError(f"unknown sankey endpoint '{endpoint}'")
    if pending is not None and pending not in BOOKED_NODES:
        raise UsageError(f"unknown booked phase '{pending}'")
    path = ["all_applications"]
    if screens:
        path.append("screen_done")
    if assessments:
        path.append("assessment_done")
    if interviews >= 1:
        path.append("interview_1")
    if interviews >= 2:
        path.append("interview_2")
    if interviews >= 3:
        path.append("interview_3plus")
    if received_offer:
        path.append("offer_received")
    if pending:
        path.append(BOOKED_NODES[pending])
    path.append(SANKEY_ENDPOINTS[endpoint][0])
    return path


def _add_path(edges: Counter, path: list[str], count: int = 1) -> None:
    for source, target in zip(path, path[1:]):
        if source == target:  # defensive: never emit self-edges
            continue
        edges[(source, target)] += count


def build_sankey(rows: list[sqlite3.Row]) -> Counter:
    edges: Counter = Counter()
    for row in rows:
        _add_path(edges, path_from_row(row))
    return edges


def _group_sort_key(group: dict) -> tuple:
    return (group["screens"], group["assessments"], group["interviews"],
            group["received_offer"], group["pending"] or "", group["endpoint"])


def aggregate_groups(rows: list[sqlite3.Row]) -> list[dict]:
    """Collapse cohort rows into counted chart shapes (the frozen replay data).

    Every group is one shape the published chart already shows: a screening flag, an
    assessment flag, an interview bucket (0, 1, 2, or 3 for "3+"), an offer flag, the
    booked phase (None, or one phase), and a folded endpoint. A group carries only a
    count, and the output is sorted by shape, so no per-application tuple, order, id,
    name, date, url, note, or path survives.
    """
    counts: Counter = Counter()
    for row in rows:
        counts[shape_from_row(row)] += 1
    groups = [
        {"screens": screens, "assessments": assessments, "interviews": interviews,
         "received_offer": received, "pending": pending, "endpoint": endpoint,
         "count": count}
        for (screens, assessments, interviews, received, pending, endpoint), count
        in counts.items()
    ]
    return sorted(groups, key=_group_sort_key)


def build_sankey_from_groups(groups: list[dict]) -> Counter:
    """Rebuild the aggregate from frozen group counts only. Never touches a database."""
    edges: Counter = Counter()
    for group in groups:
        _add_path(
            edges,
            _path_from_shape(int(group["screens"]), int(group["assessments"]),
                             int(group["interviews"]), int(group["received_offer"]),
                             group["pending"], str(group["endpoint"])),
            int(group["count"]),
        )
    return edges


def _export_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")


def _opaque_export_id() -> str:
    """A random export id. Never derived from paths, names, filters, or data."""
    return secrets.token_hex(16)


def _prepare_export_dir(layout: Layout, output: str | Path | None) -> Path:
    """Pick the directory for one export bundle; never silently reuse an old one."""
    if output is None:
        base = layout.exports_dir
        base.mkdir(parents=True, exist_ok=True)
        stamp = _export_stamp()
        for counter in range(1, 1000):
            name = f"sankey-{stamp}" if counter == 1 else f"sankey-{stamp}-{counter}"
            candidate = base / name
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            return candidate
        raise UsageError(f"cannot find a free export directory under {base}")
    target = Path(output).expanduser()
    if target.exists() and not target.is_dir():
        raise UsageError(f"--output {target} exists and is not a directory")
    target.mkdir(parents=True, exist_ok=True)
    _refuse_existing_export(target)
    return target


def _refuse_existing_export(directory: Path) -> None:
    existing = [name for name in SANKEY_ARTIFACTS if (directory / name).exists()]
    if existing:
        raise UsageError(
            "refusing to overwrite an existing Sankey export in " + str(directory)
            + " (" + ", ".join(existing) + "); every export must be a new coherent bundle."
            " Pass a new --output directory, or omit --output to get a unique"
            " timestamped directory under exports/."
        )


def _render_edges_csv(edges: Counter) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["source", "target", "value"])
    for (source, target), value in sorted(edges.items()):
        writer.writerow([source, target, value])
    return buffer.getvalue()


def _publish_bundle(files: dict[Path, str], moves: list[tuple[Path, Path]]) -> None:
    """Write every file to a staged temp name first, then rename them all into place.

    A bundle is published as a whole: a reader never sees a half-written SVG next to
    a complete metadata file. ``moves`` holds binaries that are already staged on disk
    (the rasterized PNG), so they join the same rename batch.
    """
    staged: list[tuple[Path, Path]] = []
    try:
        for target, text in files.items():
            temp = target.with_name(target.name + f".tmp-{os.getpid()}")
            temp.write_text(text, encoding="utf-8")
            staged.append((temp, target))
        for temp, target in staged:
            os.replace(temp, target)
        staged.clear()
        for temp, target in moves:
            os.replace(temp, target)
    except BaseException:
        for temp, _target in staged:
            with contextlib.suppress(OSError):
                temp.unlink()
        raise
    finally:
        for temp, _target in moves:
            with contextlib.suppress(OSError):
                temp.unlink()  # a leftover from a failed rename, or nothing at all


@dataclass(frozen=True)
class SankeyExport:
    """One timestamped bundle: the aggregate pair plus the optional chart files."""

    directory: Path
    edges: Path
    metadata: Path
    node_link: Path | None = None
    svg: Path | None = None
    png: Path | None = None
    png_tool: str | None = None
    png_note: str | None = None

    @property
    def files(self) -> list[Path]:
        """Every file of the bundle, in reading order."""
        return [path for path in (self.edges, self.metadata, self.node_link, self.svg,
                                 self.png) if path is not None]


def _chart_labels() -> list[tuple[str, str]]:
    """Chart labels: the compact text where the long metadata name is too wide."""
    return [(node_id, sankey_chart.SHORT_LABELS.get(node_id, label))
            for node_id, label in SANKEY_NODES.items()]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _tidy_number(value: float):
    """A JSON number without a float artefact: 860, not 860.0000000000001."""
    return int(value) if float(value).is_integer() else round(value, 2)


def _chart_subtitle(effective_as_of: str, cohort_from: str | None, cohort_to: str | None,
                    cohort_size: int, generated_at: str) -> str:
    parts = [f"as of {effective_as_of}"]
    if cohort_from or cohort_to:
        parts.append(f"cohort {cohort_from or 'start'} to {cohort_to or 'today'}")
    parts.append(_plural(cohort_size, "application"))
    parts.append(f"generated {generated_at}")
    return "  \u00b7  ".join(parts)


def _node_link_payload(model: sankey_chart.Chart, *, generated_at: str, export_id: str,
                       filters: dict, cohort_size: int) -> dict:
    """The generator-ready view of the same aggregate the chart draws."""
    nodes, links = sankey_chart.node_link_rows(model)
    return {
        "kind": "sankey_node_link",
        "format": NODE_LINK_FORMAT,
        "title": CHART_TITLE,
        "generated_at": generated_at,
        "export_id": export_id,
        "filters": filters,
        "cohort_size": cohort_size,
        "drawn_nodes": len(nodes),
        "drawn_links": len(links),
        "total_flow": sum(node["total"] for node in nodes if node["column"] == 0),
        "nodes": nodes,
        "links": links,
        "node_fields": {
            "index": "position in this nodes array",
            "id": "stable node id, the same id sankey-edges.csv uses",
            "label": "display text",
            "stage": "pipeline stage: 0 applications, 1-3 interviews, 4 offer, 5 outcome",
            "column": "drawn column, left to right (stages without flow are dropped)",
            "total": "applications through this node",
        },
        "link_fields": {
            "source": "index into nodes",
            "target": "index into nodes",
            "value": "applications on this flow",
            "source_id": "nodes[source].id",
            "target_id": "nodes[target].id",
        },
        "generator_hints": GENERATOR_HINTS,
        "node_labels": dict(SANKEY_NODES),
        "status_fold": SANKEY_STATUS_FOLD,
        "counting_rules": COUNTING_RULES,
        "edges_file": "sankey-edges.csv",
        "svg_file": "sankey.svg",
        "metadata_file": "sankey-metadata.json",
        "privacy": (
            "Aggregate only: no company names, roles, uuids, urls, paths, notes, or"
            " dates. Every node is a chart node and every value is a count of"
            " applications, so this file is shareable as it stands."
        ),
    }


def _stage_chart(out_dir: Path, edges: Counter, *, subtitle: str, export_id: str,
                 filters: dict, cohort_size: int, generated_at: str,
                 png: bool) -> tuple[dict[Path, str], list[tuple[Path, Path]], dict]:
    """Render the chart into staged text files, plus an optional staged PNG."""
    model = sankey_chart.build_chart(_chart_labels(), edges)
    svg_text = sankey_chart.render_svg(model, title=CHART_TITLE, subtitle=subtitle,
                                       note=CHART_NOTE)
    node_link_path = out_dir / "sankey-node-link.json"
    svg_path = out_dir / "sankey.svg"
    png_path = out_dir / "sankey.png"

    moves: list[tuple[Path, Path]] = []
    png_tool: str | None = None
    png_note: str | None = None
    if png:
        # The staged names keep the real extensions: a converter decides the output format
        # from the file name, and Inkscape ignores (and then drops) an unknown extension.
        svg_temp = out_dir / f"{svg_path.stem}-stage-{os.getpid()}.svg"
        png_temp = out_dir / f"{png_path.stem}-stage-{os.getpid()}.png"
        try:
            svg_temp.write_text(svg_text, encoding="utf-8")
            png_tool, png_note = sankey_chart.rasterize(
                svg_temp, png_temp, width=int(model.width * sankey_chart.PNG_SCALE))
        finally:
            with contextlib.suppress(OSError):
                svg_temp.unlink()
            if not png_tool:
                with contextlib.suppress(OSError):
                    png_temp.unlink()
        if png_tool:
            moves.append((png_temp, png_path))
    else:
        png_note = "PNG output is switched off (--no-png)"

    files = {
        node_link_path: json.dumps(
            _node_link_payload(model, generated_at=generated_at, export_id=export_id,
                               filters=filters, cohort_size=cohort_size),
            indent=2, ensure_ascii=False) + "\n",
        svg_path: svg_text,
    }
    chart = {
        "renderer": "job-cli sankey-chart (deterministic SVG, no charting library)",
        "layout": "pipeline columns, barycenter ordering, one flow band per edge",
        "node_link_file": node_link_path.name,
        "node_link_format": NODE_LINK_FORMAT,
        "svg_file": svg_path.name,
        "svg_sha256": hashlib.sha256(svg_text.encode("utf-8")).hexdigest(),
        "png_file": png_path.name if png_tool else None,
        "png_tool": png_tool,
        "png_note": png_note,
        "canvas": {"width": _tidy_number(model.width), "height": _tidy_number(model.height)},
        "drawn_nodes": len(model.nodes),
        "drawn_links": len(model.links),
    }
    return files, moves, chart


def export_sankey(conn: sqlite3.Connection, layout: Layout, *, as_of: str | None = None,
                  cohort_from: str | None = None, cohort_to: str | None = None,
                  output: str | Path | None = None, chart: bool = True,
                  png: bool = True) -> SankeyExport:
    """Write one timestamped aggregate bundle.

    Always: ``sankey-edges.csv`` and ``sankey-metadata.json``. With ``chart``: also
    ``sankey-node-link.json`` (generator-ready data) and ``sankey.svg`` (the rendered
    chart), plus ``sankey.png`` when ``png`` is set and a local converter is installed.
    """
    effective_as_of = as_of or dt.date.today().isoformat()
    rows = run_snapshot(conn, layout, as_of=effective_as_of, cohort_from=cohort_from,
                        cohort_to=cohort_to)
    edges = build_sankey(rows)
    groups = aggregate_groups(rows)
    out_dir = _prepare_export_dir(layout, output)
    edges_path = out_dir / "sankey-edges.csv"
    meta_path = out_dir / "sankey-metadata.json"

    generated_at = _now()
    export_id = _opaque_export_id()
    filters = {"as_of": effective_as_of, "cohort_from": cohort_from, "cohort_to": cohort_to}
    csv_text = _render_edges_csv(edges)

    files: dict[Path, str] = {edges_path: csv_text}
    moves: list[tuple[Path, Path]] = []
    chart_meta: dict | None = None
    if chart:
        staged, moves, chart_meta = _stage_chart(
            out_dir, edges,
            subtitle=_chart_subtitle(effective_as_of, cohort_from, cohort_to, len(rows),
                                     generated_at),
            export_id=export_id, filters=filters, cohort_size=len(rows),
            generated_at=generated_at, png=png)
        files.update(staged)

    artifacts = ["sankey-edges.csv", "sankey-metadata.json"]
    if chart_meta is not None:
        artifacts += ["sankey-node-link.json", "sankey.svg"]
        if chart_meta["png_file"]:
            artifacts.append("sankey.png")

    metadata = {
        "kind": "sankey_aggregate",
        "generated_at": generated_at,
        "export_id": export_id,
        "filters": filters,
        "cohort_size": len(rows),
        "edge_count": len(edges),
        "total_flow": sum(edges.values()),
        "nodes": dict(SANKEY_NODES),
        "status_fold": SANKEY_STATUS_FOLD,
        "counting_rules": COUNTING_RULES,
        "endpoints": {state: node[0] for state, node in SANKEY_ENDPOINTS.items()},
        "phases": list(SANKEY_PHASES),
        "booked_nodes": dict(BOOKED_NODES),
        "query_file": "sql/queries/snapshot.sql",
        "artifacts": artifacts,
        "chart": chart_meta,
        "frozen": {
            "format": FROZEN_FORMAT,
            "group_fields": list(GROUP_FIELDS),
            "group_count": len(groups),
            "application_count": len(rows),
            "groups": groups,
            "edges_sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
        },
        "environment": {
            "tool_version": _tool_version(),
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
        },
        "privacy": (
            "Aggregate only: no company names, roles, uuids, urls, paths, notes, or"
            " dates. Frozen replay data is a counted set of collapsed chart shapes"
            " (screening flag, assessment flag, interview bucket 0-3 where 3 means 3+,"
            " offer flag, the single booked phase or null, folded endpoint, count); it"
            " carries no per-application tuple, order, or identifier. The chart data"
            " and image are drawn from the same counted aggregate."
            " export_id is random and independent of the output directory name."
        ),
        "edges_file": edges_path.name,
    }
    files[meta_path] = json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"

    try:
        _publish_bundle(files, moves)
    except BaseException:
        for temp, _target in moves:
            with contextlib.suppress(OSError):
                temp.unlink()
        raise
    return SankeyExport(
        directory=out_dir, edges=edges_path, metadata=meta_path,
        node_link=None if chart_meta is None else out_dir / "sankey-node-link.json",
        svg=None if chart_meta is None else out_dir / "sankey.svg",
        png=None if not chart_meta or not chart_meta["png_file"] else out_dir / "sankey.png",
        png_tool=None if chart_meta is None else chart_meta["png_tool"],
        png_note=None if chart_meta is None else chart_meta["png_note"],
    )


def _stored_edges_path(meta_path: Path, name) -> Path:
    candidate = name if isinstance(name, str) and name else "sankey-edges.csv"
    relative = Path(candidate)
    parts = relative.parts
    if relative.is_absolute() or len(parts) != 1 or parts[0] in (".", ".."):
        raise UsageError(f"invalid edges_file reference in {meta_path}: {name!r}")
    return meta_path.parent / relative


def _parse_edges_csv(text: str, edges_path: Path) -> Counter:
    stored: Counter = Counter()
    try:
        for record in csv.DictReader(io.StringIO(text)):
            stored[(record["source"], record["target"])] = int(record["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageError(f"{edges_path} is not a sankey edge CSV: {exc}") from exc
    return stored


def _differences(replayed: Counter, stored: Counter) -> list[dict]:
    found = []
    for key in sorted(set(replayed) | set(stored)):
        if replayed.get(key, 0) != stored.get(key, 0):
            found.append({
                "edge": list(key),
                "replayed": replayed.get(key, 0),
                "stored": stored.get(key, 0),
            })
    return found


def _group_error(meta_path: Path, item) -> UsageError:
    return UsageError(f"{meta_path} has a malformed frozen group: {item!r}")


def _bucket(item, name: str, allowed: tuple, meta_path: Path) -> int:
    value = item[name]
    if isinstance(value, bool) or value not in allowed:
        raise _group_error(meta_path, item)
    return int(value)


def _endpoint_of(item, meta_path: Path) -> str:
    endpoint = item["endpoint"]
    # a stored group must already carry the folded endpoint, never a raw status
    if (not isinstance(endpoint, str) or endpoint not in SANKEY_ENDPOINTS
            or SANKEY_STATUS_FOLD.get(endpoint, endpoint) != endpoint):
        raise _group_error(meta_path, item)
    return endpoint


def _count_of(item, meta_path: Path) -> int:
    count = item["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise _group_error(meta_path, item)
    return count


def _group_from_item(item, meta_path: Path) -> dict:
    """Validate one v3 aggregate group. Only the seven declared fields are allowed."""
    if not isinstance(item, dict) or set(item) != set(GROUP_FIELDS):
        raise _group_error(meta_path, item)
    pending = item["pending"]
    if pending is not None and pending not in SANKEY_PHASES:
        raise _group_error(meta_path, item)
    return {
        "screens": _bucket(item, "screens", GATE_BUCKETS, meta_path),
        "assessments": _bucket(item, "assessments", GATE_BUCKETS, meta_path),
        "interviews": _bucket(item, "interviews", INTERVIEW_BUCKETS, meta_path),
        "received_offer": _bucket(item, "received_offer", (0, 1), meta_path),
        "pending": pending,
        "endpoint": _endpoint_of(item, meta_path),
        "count": _count_of(item, meta_path),
    }


def _previous_group_from_item(item, meta_path: Path) -> dict:
    """Validate one v2 aggregate group and lift it to the v3 shape. Read-only."""
    if not isinstance(item, dict) or set(item) != set(PREVIOUS_GROUP_FIELDS):
        raise _group_error(meta_path, item)
    return {
        "screens": 0,
        "assessments": 0,
        "interviews": _bucket(item, "interviews", INTERVIEW_BUCKETS, meta_path),
        "received_offer": _bucket(item, "received_offer", (0, 1), meta_path),
        "pending": None,
        "endpoint": _endpoint_of(item, meta_path),
        "count": _count_of(item, meta_path),
    }


def _legacy_frozen_groups(frozen: dict, meta_path: Path) -> tuple[list[dict], int]:
    """Read v1 per-application rows and collapse them. They are never re-emitted."""
    raw = frozen.get("rows")
    if not isinstance(raw, list) or not raw:
        raise UsageError(f"{meta_path} has an empty frozen row list; nothing to replay")
    shapes: Counter = Counter()
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != len(LEGACY_ROW_FIELDS):
            raise UsageError(f"{meta_path} has a malformed frozen row: {item!r}")
        completed, received, status = item
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise UsageError(f"{meta_path} has a malformed interview count: {item!r}")
        if isinstance(received, bool) or received not in (0, 1):
            raise UsageError(f"{meta_path} has a malformed offer flag: {item!r}")
        if not isinstance(status, str) or status not in SANKEY_ENDPOINTS:
            raise UsageError(f"{meta_path} has a malformed status: {item!r}")
        shapes[(min(completed, INTERVIEW_BUCKETS[-1]), received, _fold_status(status))] += 1
    declared = frozen.get("row_count")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared != len(raw):
        raise UsageError(
            f"{meta_path} declares frozen row_count={declared} but carries {len(raw)} rows"
        )
    groups = sorted(
        ({"screens": 0, "assessments": 0, "interviews": interviews,
          "received_offer": received, "pending": None, "endpoint": endpoint,
          "count": count}
         for (interviews, received, endpoint), count in shapes.items()),
        key=_group_sort_key)
    return groups, len(raw)


def _previous_frozen_groups(frozen: dict, meta_path: Path) -> tuple[list[dict], int]:
    """Read v2 groups (no gates, no booked phase) and lift them to v3. Read-only."""
    raw = frozen.get("groups")
    if not isinstance(raw, list) or not raw:
        raise UsageError(f"{meta_path} has an empty frozen group list; nothing to replay")
    groups = sorted((_previous_group_from_item(item, meta_path) for item in raw),
                    key=_group_sort_key)
    total = sum(group["count"] for group in groups)
    declared = frozen.get("application_count")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared != total:
        raise UsageError(
            f"{meta_path} declares {declared} applications but its frozen groups sum to {total}")
    declared_groups = frozen.get("group_count")
    if (isinstance(declared_groups, int) and not isinstance(declared_groups, bool)
            and declared_groups != len(groups)):
        raise UsageError(
            f"{meta_path} declares frozen group_count={declared_groups} but carries"
            f" {len(groups)} groups")
    return groups, total


def _no_frozen_message(meta_path: Path) -> str:
    return (
        f"{meta_path} has no valid frozen aggregate data ({FROZEN_FORMAT};"
        f" {PREVIOUS_FROZEN_FORMAT} and {LEGACY_FROZEN_FORMAT} are read-only supported);"
        " refusing to replay it."
        " Replay never executes SQL stored in a metadata file, so only a self-contained"
        " export can be replayed. Re-export with current tooling."
    )


def _frozen_groups(frozen, meta_path: Path,
                   declared_cohort: int | None = None) -> tuple[list[dict], int, str]:
    """Validate frozen aggregate groups. The metadata file is data, never SQL."""
    if not isinstance(frozen, dict):
        raise UsageError(_no_frozen_message(meta_path))
    fmt = frozen.get("format")
    if fmt == LEGACY_FROZEN_FORMAT:
        groups, total = _legacy_frozen_groups(frozen, meta_path)
    elif fmt == PREVIOUS_FROZEN_FORMAT:
        groups, total = _previous_frozen_groups(frozen, meta_path)
    elif fmt == FROZEN_FORMAT:
        raw = frozen.get("groups")
        declared_apps = frozen.get("application_count")
        empty_is_coherent = (isinstance(declared_apps, int)
                             and not isinstance(declared_apps, bool) and declared_apps == 0)
        if not isinstance(raw, list) or (not raw and not empty_is_coherent):
            raise UsageError(f"{meta_path} has an empty frozen group list; nothing to replay")
        groups = sorted((_group_from_item(item, meta_path) for item in raw),
                        key=_group_sort_key)
        total = sum(group["count"] for group in groups)
        declared = frozen.get("application_count")
        if (isinstance(declared, int) and not isinstance(declared, bool)
                and declared != total):
            raise UsageError(
                f"{meta_path} declares {declared} applications but its frozen groups sum"
                f" to {total}"
            )
        declared_groups = frozen.get("group_count")
        if (isinstance(declared_groups, int) and not isinstance(declared_groups, bool)
                and declared_groups != len(groups)):
            raise UsageError(
                f"{meta_path} declares frozen group_count={declared_groups} but carries"
                f" {len(groups)} groups"
            )
    else:
        raise UsageError(_no_frozen_message(meta_path))
    if declared_cohort is not None and declared_cohort != total:
        raise UsageError(
            f"{meta_path} declares cohort_size={declared_cohort} but its frozen groups sum"
            f" to {total}; the pair is inconsistent"
        )
    return groups, total, fmt


def _filter_value(meta_path: Path, name: str, value) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise UsageError(f"{meta_path} has a non-string {name} filter: {value!r}")


def _shipped_snapshot_sql(layout: Layout | None) -> str:
    if layout is not None:
        return read_query(layout.query_file(SNAPSHOT_QUERY_NAME))
    return read_query(find_job_root().joinpath(*QUERIES_DIR, SNAPSHOT_QUERY_NAME))


def _current_edges(conn: sqlite3.Connection, layout: Layout | None,
                   params: dict) -> tuple[Counter | None, int | None, str | None]:
    """Run the shipped snapshot query on a read-only connection, or explain why not."""
    try:
        sql = _shipped_snapshot_sql(layout)
    except (JobError, OSError) as exc:
        return None, None, f"current data comparison unavailable: {exc}"
    try:
        if layout is not None and Path(layout.db_path).exists():
            read_only = dbmod.connect(layout.db_path, read_only=True, must_exist=True)
            try:
                rows = all_rows(read_only, sql, params)
            finally:
                read_only.close()
        else:
            rows = _rows_query_only(conn, sql, params)
    except (sqlite3.Error, JobError) as exc:
        return None, None, f"current data comparison unavailable: {exc}"
    return build_sankey(rows), len(rows), None


def _rows_query_only(conn: sqlite3.Connection, sql: str, params: dict):
    """Best-effort read-only execution on a connection we do not own."""
    previous = None
    with contextlib.suppress(sqlite3.Error):
        previous = conn.execute("PRAGMA query_only").fetchone()[0]
        conn.execute("PRAGMA query_only = ON")
    try:
        return all_rows(conn, sql, params)
    finally:
        if previous is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA query_only = {int(previous)}")


def replay_sankey(conn: sqlite3.Connection, metadata_path: str | Path, *,
                  layout: Layout | None = None) -> dict:
    """Verify a saved aggregate without executing SQL from the metadata file.

    The metadata is data only. The frozen aggregate groups inside it rebuild the
    aggregate with no database access at all; the optional current-data comparison
    uses the fixed shipped sql/queries/snapshot.sql on a read-only connection.
    """
    meta_path = Path(metadata_path).expanduser()
    try:
        raw_metadata = meta_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # a file that is not UTF-8 text is a usage error, never a traceback
        raise UsageError(f"cannot read the sankey metadata file {meta_path}: {exc}") from exc
    try:
        metadata = json.loads(raw_metadata)
    except ValueError as exc:
        raise UsageError(f"{meta_path} is not valid JSON: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("kind") != "sankey_aggregate":
        raise UsageError(f"{meta_path} is not a sankey metadata file")
    edges_path = _stored_edges_path(meta_path, metadata.get("edges_file"))
    try:
        csv_text = edges_path.read_text(encoding="utf-8") if edges_path.exists() else None
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read the stored edges file {edges_path}: {exc}") from exc
    stored = _parse_edges_csv(csv_text, edges_path) if csv_text is not None else Counter()

    frozen = metadata.get("frozen")
    declared_cohort = metadata.get("cohort_size")
    if isinstance(declared_cohort, bool) or not isinstance(declared_cohort, int):
        declared_cohort = None
    groups, frozen_size, frozen_format = _frozen_groups(frozen, meta_path, declared_cohort)
    frozen_edges = build_sankey_from_groups(groups)
    frozen_differences = _differences(frozen_edges, stored)

    expected_hash = frozen.get("edges_sha256") if isinstance(frozen, dict) else None
    pair_consistent: bool | None = None
    if isinstance(expected_hash, str) and csv_text is not None:
        pair_consistent = (
            hashlib.sha256(csv_text.encode("utf-8")).hexdigest() == expected_hash
        )

    filters = metadata.get("filters") or {}
    if not isinstance(filters, dict):
        raise UsageError(f"{meta_path} has a malformed filters object")
    params = _params(_filter_value(meta_path, "as_of", filters.get("as_of")),
                     _filter_value(meta_path, "cohort_from", filters.get("cohort_from")),
                     _filter_value(meta_path, "cohort_to", filters.get("cohort_to")))
    current_edges, current_size, current_note = _current_edges(conn, layout, params)
    current_differences = ([] if current_edges is None
                           else _differences(current_edges, stored))

    pair_ok = pair_consistent is not False
    if current_edges is None:
        comparison = "unavailable"
        reproduced = not frozen_differences and pair_ok
        differences = frozen_differences
        replayed_size = frozen_size
    else:
        comparison = "matches" if not current_differences else "differs"
        reproduced = not frozen_differences and pair_ok and not current_differences
        differences = current_differences or frozen_differences
        replayed_size = current_size

    return {
        "metadata": str(meta_path),
        "edges_file": str(edges_path),
        "edges_file_present": csv_text is not None,
        "executed_metadata_sql": False,
        "frozen_format": frozen_format,
        "frozen_group_count": len(groups),
        "frozen_cohort_size": frozen_size,
        "frozen_reproduced": not frozen_differences,
        "frozen_differences": frozen_differences,
        "pair_consistent": pair_consistent,
        "current_comparison": comparison,
        "current_note": current_note,
        "current_cohort_size": current_size,
        "current_differences": current_differences,
        "stored_cohort_size": metadata.get("cohort_size"),
        # keys the CLI prints today
        "reproduced": reproduced,
        "replayed_cohort_size": replayed_size,
        "differences": differences,
    }


def _tool_version() -> str:
    from . import __version__
    return __version__
