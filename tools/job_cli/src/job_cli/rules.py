"""Workflow rules the database cannot express.

The schema enforces structure; these checks add the process rules the handoff
requires: explicit reopening, offer/acceptance ordering, round state sanity,
backdating warnings, and calendar/timezone validation.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import repo
from .db import all_rows, one, scalar
from .errors import UsageError

STATE_CHANGING_EXCLUDED = ()

ROUND_EVENTS = {"invited", "scheduled", "rescheduled", "completed", "cancelled",
                "assessment_received", "assessment_submitted"}
SCHEDULE_EVENTS = {"scheduled", "rescheduled"}
ASSESSMENT_EVENTS = {"assessment_received", "assessment_submitted"}
TERMINAL_APPLICATION_EVENTS = {"offer_accepted", "offer_declined", "rejected", "withdrawn",
                               "position_closed", "marked_inactive", "job_started"}


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" or "warn" or "info"
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


# --------------------------------------------------------------------------
# dates, times, timezones
# --------------------------------------------------------------------------
RELATIVE_DATES = {
    "today": 0,
    "yesterday": -1,
    "tomorrow": 1,
}


def today() -> str:
    return dt.date.today().isoformat()


def parse_date(value: str, *, field: str = "date") -> dt.date:
    text = (value or "").strip().lower()
    if text in RELATIVE_DATES:
        return dt.date.today() + dt.timedelta(days=RELATIVE_DATES[text])
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"{field} '{value}' is not a valid calendar date (use YYYY-MM-DD)") from exc
    return parsed


def validate_date(value: str, *, field: str = "date", allow_future: bool = False,
                  max_date: str | None = None) -> str:
    parsed = parse_date(value, field=field)
    if not allow_future and parsed > dt.date.today():
        raise UsageError(f"{field} {parsed.isoformat()} is in the future")
    if max_date:
        limit = parse_date(max_date, field="limit")
        if parsed > limit:
            raise UsageError(f"{field} {parsed.isoformat()} is after {limit.isoformat()}")
    return parsed.isoformat()


def validate_time(value: str) -> str:
    text = (value or "").strip()
    if len(text) != 5 or text[2] != ":":
        raise UsageError(f"time '{value}' must look like HH:MM")
    hours, minutes = text[:2], text[3:]
    if not (hours.isdigit() and minutes.isdigit()) or int(hours) > 23 or int(minutes) > 59:
        raise UsageError(f"time '{value}' is not a valid 24-hour time")
    return text


def local_timezone_name() -> str | None:
    env = os.environ.get("TZ")
    if env:
        try:
            ZoneInfo(env)
            return env
        except (ZoneInfoNotFoundError, ValueError):
            pass
    link = "/etc/localtime"
    try:
        target = os.path.realpath(link)
    except OSError:
        return None
    marker = "zoneinfo/"
    index = target.find(marker)
    if index != -1:
        name = target[index + len(marker):]
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return None


def check_timezone(name: str) -> list[Issue]:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return [Issue("error", "timezone", f"'{name}' is not a known IANA timezone name")]
    return []


def check_local_time(timezone: str, date_value: str, time_value: str) -> list[Issue]:
    """Detect DST ambiguity or a nonexistent local time."""
    parsed = parse_date(date_value, field="appointment date")
    hours, minutes = validate_time(time_value).split(":")
    naive = dt.datetime(parsed.year, parsed.month, parsed.day, int(hours), int(minutes))
    tz = ZoneInfo(timezone)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    issues: list[Issue] = []
    if first.utcoffset() != second.utcoffset():
        issues.append(Issue(
            "warn", "dst_ambiguous",
            f"{date_value} {time_value} in {timezone} happens twice (clocks go back);"
            f" the earlier offset ({first.utcoffset()}) is assumed",
        ))
    round_trip = first.astimezone(dt.UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        issues.append(Issue(
            "error", "dst_nonexistent",
            f"{date_value} {time_value} does not exist in {timezone} (clocks jump forward);"
            f" the nearest real time is {round_trip.strftime('%H:%M')}",
        ))
    return issues


# --------------------------------------------------------------------------
# event planning
# --------------------------------------------------------------------------
def state_before(conn: sqlite3.Connection, app_id: str, occurred_on: str,
                 after_same_day: bool = True) -> tuple[str, sqlite3.Row | None]:
    """Application state immediately before a new event on occurred_on."""
    sql = ("SELECT e.id, e.event_type, e.occurred_on, e.day_order, t.application_state"
           " FROM events e JOIN event_types t ON t.code = e.event_type"
           " WHERE e.application_id = ? AND t.application_state IS NOT NULL")
    params: list[object] = [app_id]
    if after_same_day:
        sql += " AND e.occurred_on <= ?"
        params.append(occurred_on)
    else:
        sql += " AND e.occurred_on < ?"
        params.append(occurred_on)
    sql += " ORDER BY e.occurred_on DESC, e.day_order DESC LIMIT 1"
    row = one(conn, sql, params)
    if row is None:
        return "in_progress", None
    return str(row["application_state"]), row


def plan_event(
    conn: sqlite3.Connection,
    app_id: str,
    event_type: str,
    occurred_on: str,
    *,
    round_id: int | None = None,
    scheduled_for: str | None = None,
    scheduled_time: str | None = None,
    timezone: str | None = None,
    after_same_day: bool = True,
) -> list[Issue]:
    """Return errors and warnings for a proposed event. Errors block the write."""
    issues: list[Issue] = []
    meta = repo.event_type_row(conn, event_type)
    scope = str(meta["scope"])
    changes_state = meta["application_state"] is not None

    if scope == "round" and round_id is None:
        issues.append(Issue("error", "round_required",
                            f"'{event_type}' is a round event; choose a round"))
    if scope == "application" and round_id is not None:
        issues.append(Issue("error", "round_not_allowed",
                            f"'{event_type}' is an application-level event and takes no round"))

    round_row = None
    if round_id is not None:
        round_row = repo.round_row(conn, round_id)
        if str(round_row["application_id"]) != app_id:
            issues.append(Issue("error", "wrong_application",
                                "the chosen round belongs to a different application"))
        issues.extend(_plan_round_event(conn, round_row, event_type, scheduled_for))

    state, previous = state_before(conn, app_id, occurred_on, after_same_day=after_same_day)
    if changes_state:
        issues.extend(_plan_state_change(conn, app_id, event_type, str(meta["application_state"]),
                                         state, occurred_on, previous))
    if event_type in ("offer_accepted", "offer_declined"):
        issues.extend(_plan_offer_decision(conn, app_id, event_type, occurred_on))
    if event_type == "job_started":
        issues.extend(_plan_job_started(conn, app_id, occurred_on))

    last = one(conn, "SELECT max(occurred_on) AS last_on FROM events WHERE application_id = ?",
               (app_id,))
    if last and last["last_on"] and occurred_on < str(last["last_on"]):
        later = all_rows(
            conn,
            "SELECT e.occurred_on, e.event_type, t.label FROM events e"
            " JOIN event_types t ON t.code = e.event_type"
            " WHERE e.application_id = ? AND e.occurred_on > ? AND t.application_state IS NOT NULL"
            " ORDER BY e.occurred_on, e.day_order",
            (app_id, occurred_on),
        )
        detail = "; ".join(f"{r['occurred_on']} {r['label']}" for r in later[:6])
        issues.append(Issue(
            "warn", "backdated",
            f"backdated entry: later events already exist"
            + (f" ({detail})" if detail else ""),
        ))
    return issues


def round_facts(conn: sqlite3.Connection, round_id: int) -> dict:
    """Completion and cancellation history of a round.

    The round's `current_status` is only the latest state event, so a round that
    was completed and then scheduled again would look merely `scheduled` again.
    Workflow rules need the completion fact itself.
    """
    row = repo.round_row(conn, round_id)
    completion = one(
        conn,
        "SELECT id, event_type, occurred_on FROM events WHERE round_id = ?"
        " AND event_type IN ('completed', 'assessment_submitted')"
        " ORDER BY occurred_on, day_order LIMIT 1",
        (round_id,),
    )
    return {"row": row, "completion": completion,
            "status": str(row["current_status"]), "phase": str(row["phase"])}


def _plan_round_event(conn: sqlite3.Connection, round_row: sqlite3.Row, event_type: str,
                      scheduled_for: str | None) -> list[Issue]:
    issues: list[Issue] = []
    facts = round_facts(conn, int(round_row["id"]))
    status = facts["status"]
    phase = facts["phase"]
    completion = facts["completion"]
    label = f"round #{round_row['sequence_no']} ({phase}, {round_row['round_type']})"
    if event_type in ("completed", "assessment_submitted"):
        if completion is not None:
            issues.append(Issue(
                "error", "already_completed",
                f"{label} already has a completion"
                f" ({completion['event_type']} on {completion['occurred_on']});"
                " correct that event instead of adding another one",
            ))
        if status == "cancelled":
            issues.append(Issue("error", "round_cancelled",
                                f"{label} was cancelled; create a new round instead of"
                                " completing it"))
        if event_type == "completed" and phase == "assessment":
            issues.append(Issue("error", "wrong_phase",
                                f"{label} is an assessment; use assessment_submitted"))
        if event_type == "assessment_submitted" and phase != "assessment":
            issues.append(Issue("error", "wrong_phase",
                                f"{label} is not an assessment round"))
    if event_type in SCHEDULE_EVENTS | {"cancelled"} and completion is not None:
        issues.append(Issue(
            "error", "round_after_completion",
            f"{label} is completed; correct the completion instead of logging"
            f" {event_type}, or create a new round",
        ))
    if event_type == "cancelled" and status == "cancelled":
        issues.append(Issue("warn", "already_cancelled", f"{label} is already cancelled"))
    if event_type == "invited":
        if completion is not None or status in ("completed", "cancelled"):
            issues.append(Issue("error", "round_closed",
                                f"{label} is {'completed' if completion else status};"
                                " create a new round for a genuinely new interview"))
        elif status in ("invited", "scheduled"):
            issues.append(Issue("warn", "round_reuse",
                                f"{label} is already {status}; reschedule it or create a new round"))
    if event_type in SCHEDULE_EVENTS and not scheduled_for:
        issues.append(Issue("error", "schedule_missing", "a scheduling event needs a date"))
    if event_type not in SCHEDULE_EVENTS and scheduled_for:
        issues.append(Issue("error", "schedule_unexpected",
                            f"'{event_type}' cannot carry an appointment date"))
    return issues


def _regression_issue(state: str) -> Issue:
    return Issue(
        "warn", "state_regression",
        f"this moves the application back from '{state}' to 'in_progress' while an offer or a"
        " start is already on record",
    )


def _plan_state_change(conn: sqlite3.Connection, app_id: str, event_type: str,
                       new_state: str, state: str, occurred_on: str,
                       previous: sqlite3.Row | None) -> list[Issue]:
    issues: list[Issue] = []
    where = "at the start" if previous is None else f"since {previous['occurred_on']}"
    if state in repo.CLOSED_STATUSES and event_type != "reopened":
        issues.append(Issue(
            "error", "needs_reopen",
            f"the application is '{state}' {where}; log 'reopened' first, then the new event",
        ))
    if event_type == "reopened" and state not in repo.CLOSED_STATUSES:
        issues.append(Issue("warn", "nothing_to_reopen",
                            f"the application is already '{state}'; a reopen event is unusual"))
    if (new_state == "in_progress" and state in ("offer_pending", "accepted", "started")
            and event_type != "reopened"):
        issues.append(_regression_issue(state))
    if event_type == "offer_received" and state in ("accepted", "started", "offer_declined"):
        issues.append(Issue("warn", "offer_after_decision",
                            f"an offer decision ({state}) is already recorded at that point"))
    if state == "offer_pending" and event_type == "offer_received":
        issues.append(Issue("info", "offer_revision",
                            "an offer is already recorded; extra offer events are counted once"
                            " in reports"))
    return issues


def _plan_offer_decision(conn: sqlite3.Connection, app_id: str, event_type: str,
                         occurred_on: str) -> list[Issue]:
    offer_id = scalar(
        conn,
        "SELECT e.id FROM events e WHERE e.application_id = ? AND e.event_type = 'offer_received'"
        " AND e.occurred_on <= ? ORDER BY e.occurred_on DESC, e.day_order DESC LIMIT 1",
        (app_id, occurred_on),
    )
    if offer_id is None:
        return [Issue(
            "error", "offer_missing",
            f"no offer_received event exists on or before {occurred_on};"
            f" record the offer first (backdating is allowed), then the offer decision",
        )]
    return []


def _plan_job_started(conn: sqlite3.Connection, app_id: str,
                      occurred_on: str) -> list[Issue]:
    state, previous = state_before(conn, app_id, occurred_on)
    if state not in ("accepted", "started"):
        return [Issue(
            "error", "start_requires_acceptance",
            "starting a job requires an accepted offer first (record offer_received and"
            " offer_accepted, backdating is allowed)",
        )]
    return []


def duplicate_completion(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row | None:
    return one(
        conn,
        "SELECT e.id, e.event_type, e.occurred_on FROM events e WHERE e.round_id = ?"
        " AND e.event_type IN ('completed', 'assessment_submitted')",
        (round_id,),
    )


# --------------------------------------------------------------------------
# whole-history validation
#
# The per-event checks above answer "is this event acceptable right now?".
# The functions below answer "is the complete resulting timeline consistent?"
# by replaying every event of one application in chronological order. That is
# what catches a positional or backdated insert, an edit, an undo, or a delete
# that leaves a later event without its prerequisite.
# --------------------------------------------------------------------------
def check_event_schedule(event_type: str, occurred_on: str, scheduled_for: str | None,
                         scheduled_time: str | None, timezone: str | None) -> list[Issue]:
    """Validate appointment fields for one (possibly edited) event."""
    issues: list[Issue] = []
    if event_type in SCHEDULE_EVENTS:
        if not scheduled_for:
            issues.append(Issue("error", "schedule_missing", "a scheduling event needs a date"))
        else:
            try:
                scheduled_for = validate_date(scheduled_for, field="appointment date",
                                              allow_future=True)
            except UsageError as exc:
                issues.append(Issue("error", "bad_appointment_date", str(exc)))
                scheduled_for = None
    elif scheduled_for or scheduled_time or timezone:
        issues.append(Issue(
            "error", "schedule_unexpected",
            f"'{event_type}' is not a scheduling event; it takes no appointment fields",
        ))
        return issues
    if scheduled_time or timezone:
        if not (scheduled_time and timezone):
            issues.append(Issue("error", "schedule_pair",
                                "an appointment time and its timezone must be given together"))
            return issues
        try:
            scheduled_time = validate_time(scheduled_time)
        except UsageError as exc:
            issues.append(Issue("error", "bad_time", str(exc)))
            return issues
        timezone_issues = check_timezone(timezone)
        issues.extend(timezone_issues)
        if timezone_issues:
            return issues
        try:
            issues.extend(check_local_time(timezone, scheduled_for or occurred_on, scheduled_time))
        except (UsageError, ZoneInfoNotFoundError, ValueError):  # pragma: no cover - defensive
            pass
    if scheduled_for and event_type in SCHEDULE_EVENTS:
        try:
            if parse_date(scheduled_for, field="appointment date") < parse_date(
                    occurred_on, field="event date"):
                issues.append(Issue(
                    "error", "appointment_before_event",
                    f"the appointment date {scheduled_for} is before the event date {occurred_on}",
                ))
        except UsageError:  # already reported above
            pass
    return issues


def event_key(event_id: int | None) -> str:
    return f"event:{event_id}"


def timeline_issues(conn: sqlite3.Connection, app_id: str) -> dict[str, Issue]:
    """Replay one application's history; keyed by the event that is at fault."""
    rows = all_rows(
        conn,
        "SELECT id, event_type, occurred_on, day_order, round_id, scheduled_for FROM events"
        " WHERE application_id = ? ORDER BY occurred_on, day_order, id",
        (app_id,),
    )
    rounds = {
        int(row["id"]): row
        for row in all_rows(
            conn, "SELECT id, sequence_no, phase FROM rounds WHERE application_id = ?", (app_id,)
        )
    }
    types = {
        str(row["code"]): row
        for row in all_rows(
            conn, "SELECT code, application_state, scope FROM event_types"
        )
    }
    return _simulate(rows, rounds, types)


def _round_label(row: sqlite3.Row | None, round_id: int | None) -> str:
    if row is None:
        return f"round {round_id}"
    return f"round #{row['sequence_no']} ({row['phase']})"


def _simulate(rows, rounds, types) -> dict[str, Issue]:
    issues: dict[str, Issue] = {}

    context = {"prefix": ""}

    def add(key: str, severity: str, code: str, message: str) -> None:
        previous = issues.get(key)
        if previous is not None and (previous.is_error or not severity == "error"):
            return
        issues[key] = Issue(severity, code, context["prefix"] + message)

    state = "in_progress"
    offer_seen = False
    facts: dict[int, dict] = {}
    for row in rows:
        key = event_key(int(row["id"]))
        etype = str(row["event_type"])
        occurred = str(row["occurred_on"])
        context["prefix"] = f"{etype} on {occurred}: "
        meta = types.get(etype, {})
        new_state = meta["application_state"] if meta else None
        round_id = row["round_id"]
        round_row = rounds.get(int(round_id)) if round_id is not None else None
        if new_state is not None and state in repo.CLOSED_STATUSES and etype != "reopened":
            add(key, "error", "needs_reopen",
                f"the application was '{state}' at this point; an explicit 'reopened' event is"
                " required before it")
        if etype in ("offer_accepted", "offer_declined") and not offer_seen:
            add(key, "error", "offer_missing",
                "no offer_received event exists earlier in the timeline; the decision has no offer")
        if etype == "job_started" and state not in ("accepted", "started"):
            add(key, "error", "start_requires_acceptance",
                f"the application was '{state}' at this point; job_started requires an accepted"
                " offer first")
        if new_state is not None:
            if etype == "reopened" and state not in repo.CLOSED_STATUSES:
                add(key, "warn", "nothing_to_reopen",
                    f"the application was already '{state}'; a reopen event is unusual")
            if (new_state == "in_progress" and state in ("offer_pending", "accepted", "started")
                    and etype != "reopened"):
                add(key, "warn", "state_regression", _regression_issue(state).message)
            if etype == "offer_received" and state in ("accepted", "started", "offer_declined"):
                add(key, "warn", "offer_after_decision",
                    f"an offer decision ('{state}') is already recorded at that point")
            state = str(new_state)
        if etype == "offer_received":
            offer_seen = True
        if round_id is None:
            continue
        if round_row is None:
            add(key, "error", "round_missing", f"{etype} refers to a round that no longer exists")
            continue
        label = _round_label(round_row, int(round_id))
        record = facts.setdefault(int(round_id), {
            "completion": None, "cancelled": False, "scheduled_for": None, "invited": False,
        })
        if etype in ("completed", "assessment_submitted"):
            if record["completion"] is not None:
                add(key, "error", "already_completed",
                    f"{label} already has a completion earlier in the timeline")
            if record["cancelled"]:
                add(key, "error", "round_cancelled",
                    f"{label} was cancelled earlier in the timeline")
            phase = str(round_row["phase"])
            if etype == "completed" and phase == "assessment":
                add(key, "error", "wrong_phase",
                    f"{label} is an assessment; use assessment_submitted")
            if etype == "assessment_submitted" and phase != "assessment":
                add(key, "error", "wrong_phase", f"{label} is not an assessment round")
            if record["scheduled_for"] and str(record["scheduled_for"]) > occurred:
                add(key, "error", "completion_before_appointment",
                    f"{label} was scheduled for {record['scheduled_for']}, after this completion"
                    f" on {occurred}")
            record["completion"] = key
        elif etype in SCHEDULE_EVENTS:
            if record["completion"] is not None:
                add(key, "error", "round_after_completion",
                    f"{label} was already completed; correct the completion instead of logging"
                    f" {etype}")
            if row["scheduled_for"]:
                record["scheduled_for"] = str(row["scheduled_for"])
        elif etype == "cancelled":
            if record["completion"] is not None:
                add(key, "error", "round_after_completion",
                    f"{label} was already completed; a cancellation is contradictory")
            record["cancelled"] = True
        elif etype == "invited":
            if record["completion"] is not None or record["cancelled"]:
                add(key, "error", "round_closed",
                    f"{label} is already closed; create a new round for a genuinely new interview")
    return issues


def new_timeline_issues(conn: sqlite3.Connection, app_id: str,
                        baseline: dict[str, Issue]) -> list[Issue]:
    """Issues that the change introduced, ignoring problems the history already had."""
    proposed = timeline_issues(conn, app_id)
    fresh: list[Issue] = []
    for key, issue in proposed.items():
        old = baseline.get(key)
        if old is not None and old.severity == issue.severity and old.code == issue.code:
            continue
        if old is not None and old.is_error and not issue.is_error:
            continue
        fresh.append(issue)
    return fresh
