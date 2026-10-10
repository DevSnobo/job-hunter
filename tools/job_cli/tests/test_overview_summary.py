"""Sum counters under a printed table, the answered/waiting split, and the
rejected-last display order.

Two review items meet here: the counters and colours under the application
overview table, and the display order that keeps rejected applications at the
bottom. Both are display-only: the SQL order and the --json output stay as they
are, because a machine reader must keep seeing the query's order and its keys.
"""
from __future__ import annotations

import io
import json
import re
from uuid import uuid4

from helpers import rows

from job_cli import db as dbmod
from job_cli import reporting, ui
from job_cli.paths import build_layout

#: The keys `list --json` has always returned, in the query's order.
OVERVIEW_JSON_KEYS = [
    "id", "company", "role", "current_status", "submitted_on", "status_since",
    "last_activity_on", "completed_interviews", "completed_screens",
    "completed_assessments", "next_appointment",
]


def build_mixed(run, log_ok):
    """Three applications: one answered in progress, one waiting, one rejected.

    The rejected application has the newest activity, so the query puts it first
    and only the display order can move it to the bottom.
    """
    run("add", "--company", "Replied GmbH", "--role", "Backend", "--submitted-on", "2026-01-05")
    log_ok("Replied GmbH", "--event", "invited", "--date", "2026-01-08", "--round", "new",
           "--phase", "screening", "--round-type", "recruiter_screen", "--format", "phone")
    run("add", "--company", "Quiet AG", "--role", "Backend", "--submitted-on", "2026-01-06")
    log_ok("Quiet AG", "--event", "note", "--date", "2026-01-07")
    log_ok("Quiet AG", "--event", "follow_up_sent", "--date", "2026-01-09")
    run("add", "--company", "Closed KG", "--role", "Backend", "--submitted-on", "2026-01-07")
    log_ok("Closed KG", "--event", "rejected", "--date", "2026-01-12")


def overview_summary(run):
    layout = build_layout(str(run.db), str(run.root))
    conn = dbmod.connect(layout.db_path, must_exist=True)
    try:
        return reporting.overview_summary(conn, reporting.overview(conn, layout))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the counters
# --------------------------------------------------------------------------
def test_the_summary_counts_the_table_and_splits_in_progress(run, log_ok):
    build_mixed(run, log_ok)
    summary = overview_summary(run)
    assert summary["total"] == 3
    assert summary["status_counts"] == {"in_progress": 2, "rejected": 1}
    assert summary["in_progress"] == 2
    assert summary["rejected"] == 1
    # The invitation answered one of the two in-progress applications; a note and
    # a follow-up are applicant actions, so they do not answer anything.
    assert summary["answered"] == 1
    assert summary["waiting"] == 1
    assert summary["answered"] + summary["waiting"] == summary["in_progress"]


def test_an_invitation_to_a_screening_counts_as_an_employer_answer(run, log_ok):
    run("add", "--company", "Screened AG", "--role", "Dev", "--submitted-on", "2026-01-05")
    log_ok("Screened AG", "--event", "invited", "--date", "2026-01-06", "--round", "new",
           "--phase", "screening", "--round-type", "recruiter_screen", "--format", "phone")
    summary = overview_summary(run)
    assert summary["status_counts"] == {"in_progress": 1}
    assert summary["answered"] == 1
    assert summary["waiting"] == 0


def test_a_lone_application_without_employer_event_is_waiting(run, log_ok):
    run("add", "--company", "Silent AG", "--role", "Dev", "--submitted-on", "2026-01-05")
    log_ok("Silent AG", "--event", "follow_up_sent", "--date", "2026-01-09")
    summary = overview_summary(run)
    assert summary["answered"] == 0
    assert summary["waiting"] == 1


def test_overview_summary_describes_the_rows_it_is_given(run, log_ok):
    """The row-scoped helper counts exactly the rows it is handed."""
    build_mixed(run, log_ok)
    layout = build_layout(str(run.db), str(run.root))
    conn = dbmod.connect(layout.db_path, must_exist=True)
    try:
        only_rejected = reporting.overview(conn, layout, status="rejected")
        rows_summary = reporting.overview_summary(conn, only_rejected)
        tracked = reporting.tracker_summary(conn, layout)
    finally:
        conn.close()
    assert rows_summary["total"] == 1
    assert rows_summary["status_counts"] == {"rejected": 1}
    assert rows_summary["in_progress"] == 0
    assert rows_summary["waiting"] == 0
    # The tracker-wide helper keeps answering the whole-tracker question, so a
    # filtered table never loses the in_progress / rejected picture.
    assert tracked["total"] == 3
    assert tracked["status_counts"] == {"in_progress": 2, "rejected": 1}
    assert tracked["in_progress"] == 2
    assert tracked["rejected"] == 1
    assert tracked["answered"] == 1
    assert tracked["waiting"] == 1


def test_tracker_summary_counts_every_application(run, log_ok):
    build_mixed(run, log_ok)
    layout = build_layout(str(run.db), str(run.root))
    conn = dbmod.connect(layout.db_path, must_exist=True)
    try:
        tracked = reporting.tracker_summary(conn, layout)
    finally:
        conn.close()
    assert (tracked["total"], tracked["in_progress"], tracked["rejected"]) == (3, 2, 1)
    assert tracked["answered"] + tracked["waiting"] == tracked["in_progress"]


# --------------------------------------------------------------------------
# the printed table
# --------------------------------------------------------------------------
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """The captured console output without its colour codes."""
    return ANSI.sub("", text)


def _line_with(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if name in line)


def _render(monkeypatch, summary_rows, summary) -> str:
    buffer = io.StringIO()
    # width and height together: rich falls back to a dumb 80x25 terminal when
    # only one of them is set and TERM says dumb. color_system with no_color=False
    # keeps the colours visible when the environment sets TERM=dumb and NO_COLOR.
    console = ui.Console(file=buffer, force_terminal=True, width=200, height=60,
                         highlight=False, no_color=False, color_system="standard")
    monkeypatch.setattr(ui, "console", console)
    ui.render_applications(summary_rows, summary=summary)
    return buffer.getvalue()


def _row(company: str, status: str, *, app_id: str, activity: str = "2026-01-05") -> dict:
    return {
        "id": app_id, "company": company, "role": "Backend", "current_status": status,
        "submitted_on": "2026-01-05", "status_since": activity,
        "last_activity_on": activity, "completed_interviews": 0, "completed_screens": 0,
        "completed_assessments": 0, "next_appointment": None,
    }


def test_the_table_orders_rejected_last_and_colours_the_two_groups(monkeypatch):
    answered_id, waiting_id, rejected_id = str(uuid4()), str(uuid4()), str(uuid4())
    # Query order: newest activity first, which is the rejected row.
    table_rows = [
        _row("Closed KG", "rejected", app_id=rejected_id, activity="2026-01-12"),
        _row("Replied GmbH", "in_progress", app_id=answered_id, activity="2026-01-08"),
        _row("Quiet AG", "in_progress", app_id=waiting_id, activity="2026-01-06"),
    ]
    summary = {
        "total": 3, "status_counts": {"in_progress": 2, "rejected": 1},
        "in_progress": 2, "rejected": 1, "answered": 1, "waiting": 1,
        "answered_ids": {answered_id},
    }
    text = _render(monkeypatch, table_rows, summary)
    lines = text.splitlines()
    order = [lines.index(_line_with(text, name))
             for name in ("Replied GmbH", "Quiet AG", "Closed KG")]
    assert order == sorted(order), "the rejected application must be printed last"

    answered_line = _line_with(text, "Replied GmbH")
    waiting_line = _line_with(text, "Quiet AG")
    rejected_line = _line_with(text, "Closed KG")
    assert ui.ANSWERED_MARKER in answered_line and "\x1b[1;92m" in answered_line
    assert ui.WAITING_MARKER in waiting_line and "\x1b[33m" in waiting_line
    # A rejected row fades, but its status cell keeps the rejected colour.
    assert "\x1b[2m" in rejected_line and "\x1b[31m" in rejected_line
    assert "\x1b[2m" not in answered_line
    assert "rejected" in rejected_line


def test_the_counter_line_names_the_shown_rows_only_when_they_differ(monkeypatch):
    """Unfiltered prints one clean line; a filtered table says how many it shows."""
    answered_id = str(uuid4())
    table_rows = [_row("Replied GmbH", "in_progress", app_id=answered_id)]
    tracker = {
        "total": 4, "status_counts": {"in_progress": 2, "rejected": 2}, "in_progress": 2,
        "rejected": 2, "answered": 1, "waiting": 1, "answered_ids": {answered_id},
    }
    filtered = _plain(_render(monkeypatch, table_rows, tracker))
    line = next(row for row in filtered.splitlines() if "tracked" in row)
    assert "4 applications tracked" in line
    assert "in_progress 2 (1 answered, 1 waiting)" in line
    assert "rejected 2" in line
    assert line.endswith("- showing 1")

    whole = dict(tracker, total=1, status_counts={"in_progress": 1}, in_progress=1,
                 rejected=0, answered=1, waiting=0)
    unfiltered = _plain(_render(monkeypatch, table_rows, whole))
    line = next(row for row in unfiltered.splitlines() if "tracked" in row)
    assert "1 application tracked" in line
    assert "showing" not in line


def test_the_table_prints_the_legend_and_the_counters(monkeypatch):
    answered_id = str(uuid4())
    table_rows = [_row("Replied GmbH", "in_progress", app_id=answered_id)]
    summary = {
        "total": 1, "status_counts": {"in_progress": 1}, "in_progress": 1,
        "rejected": 0, "answered": 1, "waiting": 0, "answered_ids": {answered_id},
    }
    text = _render(monkeypatch, table_rows, summary)
    assert "legend:" in text
    assert "answered by the employer" in text and "still waiting" in text
    assert "1 application tracked" in text
    assert "1 answered" in text and "0 waiting" in text
    assert "showing" not in text


def test_a_table_without_in_progress_rows_prints_no_legend(monkeypatch):
    rejected_id = str(uuid4())
    table_rows = [_row("Closed KG", "rejected", app_id=rejected_id)]
    summary = {
        "total": 1, "status_counts": {"rejected": 1}, "in_progress": 0, "rejected": 1,
        "answered": 0, "waiting": 0, "answered_ids": set(),
    }
    text = _render(monkeypatch, table_rows, summary)
    assert "legend:" not in text
    assert "1 application tracked" in text


def test_render_applications_without_a_summary_still_prints_the_table(monkeypatch):
    """The counters are optional: a caller that passes none still gets the table."""
    table_rows = [_row("Plain AG", "offer_pending", app_id=str(uuid4()))]
    text = _render(monkeypatch, table_rows, None)
    assert "Plain AG" in text
    assert "legend:" not in text
    assert "1 application" not in text


# --------------------------------------------------------------------------
# the CLI surface
# --------------------------------------------------------------------------
def test_list_prints_the_counters_under_the_table(run, log_ok):
    build_mixed(run, log_ok)
    result = run("list")
    assert result.ok, result.output
    text = result.output
    assert "3 applications tracked" in text
    assert "in_progress 2 (1 answered, 1 waiting)" in text
    assert "rejected 1" in text
    assert "showing" not in text, "an unfiltered table keeps its single clean line"
    assert text.index("legend:") > text.index("Applications")
    # The counters come after the table, the usage hint comes last.
    assert text.index("3 applications tracked") > text.index("Closed KG")
    assert text.index("Use the short id") > text.index("3 applications tracked")


def test_a_filtered_table_keeps_the_tracker_counters_and_says_how_many_it_shows(run, log_ok):
    """The filtered table still answers the whole-tracker question."""
    build_mixed(run, log_ok)
    result = run("list", "--status", "rejected")
    assert result.ok, result.output
    line = next(line for line in result.output.splitlines() if "tracked" in line)
    assert line == ("3 applications tracked \u00b7 in_progress 2 (1 answered, 1 waiting)"
                    " \u00b7 rejected 1 - showing 1")
    assert "Replied GmbH" not in result.output


def test_a_limited_table_says_how_many_it_shows(run, log_ok):
    build_mixed(run, log_ok)
    result = run("list", "--limit", "1")
    assert result.ok, result.output
    assert "3 applications tracked" in result.output
    assert result.output.rstrip().splitlines()[-2].endswith("- showing 1")


def test_an_open_only_table_says_how_many_it_shows(run, log_ok):
    build_mixed(run, log_ok)
    result = run("list", "--open")
    assert result.ok, result.output
    assert "3 applications tracked" in result.output
    assert "- showing 2" in result.output


def test_list_prints_the_rejected_application_last(run, log_ok):
    build_mixed(run, log_ok)
    text = run("list").output
    order = [text.index(name) for name in ("Quiet AG", "Replied GmbH", "Closed KG")]
    assert order == sorted(order)


def test_list_json_keeps_the_query_order_and_the_old_keys(run, log_ok):
    """The counters are human-path only; a machine reader sees exactly what it saw."""
    build_mixed(run, log_ok)
    result = run("list", "--json")
    assert result.ok, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    # The query order is untouched: newest activity first, rejected included.
    assert [row["company"] for row in data] == ["Closed KG", "Quiet AG", "Replied GmbH"]
    for row in data:
        assert list(row) == OVERVIEW_JSON_KEYS
    assert "applications" not in result.output
    assert "legend" not in result.output


def test_waiting_prints_the_same_counter_line(run, log_ok):
    """`job waiting` uses the same helper, so its line is the tracker picture."""
    build_mixed(run, log_ok)
    result = run("waiting", "--as-of", "2026-02-01", "--stale-days", "20")
    assert result.ok, result.output
    assert "3 applications tracked" in result.output
    assert "in_progress 2 (1 answered, 1 waiting)" in result.output
    assert "rejected 1 - showing 2" in result.output
    assert "Review hint only" in result.output


def test_the_json_row_helper_matches_the_overview_query(run, log_ok):
    build_mixed(run, log_ok)
    assert [list(row) for row in rows(run, "list", "--json")] == [OVERVIEW_JSON_KEYS] * 3
