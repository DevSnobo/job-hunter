"""Parameterized SQLite access for applications, rounds, events, and artifacts.

No schema definition lives here: the database is created only by the numbered
SQL migrations. Every statement uses bound parameters.
"""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

from .db import all_rows, one, scalar, transaction
from .errors import UsageError

CLOSED_STATUSES = frozenset(
    {"rejected", "withdrawn", "offer_declined", "position_closed", "inactive"}
)
ACTIVE_STATUSES = frozenset({"in_progress", "offer_pending"})

APPLICATION_FIELDS = (
    "company",
    "role",
    "submitted_on",
    "discovery_source",
    "submission_channel",
    "recruiter_type",
    "platform_name",
    "source_detail",
    "job_url",
    "location",
    "notes",
    # Which intake folder below applications/ this application was registered
    # from (the <company>[_<posting-id>] identifier), or NULL when it was typed
    # in by hand. The folder itself stays on disk.
    "intake_folder",
)
ROUND_FIELDS = ("phase", "round_type", "format", "label", "notes")
EVENT_FIELDS = (
    "event_type",
    "occurred_on",
    "round_id",
    "scheduled_for",
    "scheduled_time",
    "timezone",
    "reason_code",
    "notes",
)

LOOKUP_TABLES = {
    "discovery_source": "discovery_sources",
    "submission_channel": "submission_channels",
    "recruiter_type": "recruiter_types",
    "round_type": "round_types",
    "format": "interview_formats",
    "participant_role": "participant_roles",
    "artifact_type": "artifact_types",
}


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------
def lookup_values(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    table = LOOKUP_TABLES[key]
    return all_rows(conn, f"SELECT code, label FROM {table} ORDER BY label")


def lookup_label(conn: sqlite3.Connection, key: str, code: str) -> str:
    table = LOOKUP_TABLES[key]
    value = scalar(conn, f"SELECT label FROM {table} WHERE code = ?", (code,))
    return code if value is None else str(value)


def require_lookup(conn: sqlite3.Connection, key: str, code: str) -> None:
    table = LOOKUP_TABLES[key]
    if scalar(conn, f"SELECT 1 FROM {table} WHERE code = ?", (code,)) is None:
        raise UsageError(f"unknown {key} '{code}'; see the values in {table}")


def event_types(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return all_rows(
        conn, "SELECT code, label, scope, application_state, round_state"
        " FROM event_types ORDER BY label"
    )


def event_type_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM event_types WHERE code = ?", (code,))
    if row is None:
        raise UsageError(f"unknown event type '{code}'")
    return row


# --------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------
def add_application(
    conn: sqlite3.Connection,
    *,
    company: str,
    role: str,
    submitted_on: str,
    discovery_source: str = "unknown",
    submission_channel: str = "unknown",
    recruiter_type: str = "unknown",
    platform_name: str | None = None,
    source_detail: str | None = None,
    job_url: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    intake_folder: str | None = None,
    app_id: str | None = None,
) -> str:
    company = (company or "").strip()
    role = (role or "").strip()
    if not company or not role:
        raise UsageError("company and role are required")
    for key, code in (
        ("discovery_source", discovery_source),
        ("submission_channel", submission_channel),
        ("recruiter_type", recruiter_type),
    ):
        require_lookup(conn, key, code)
    identifier = app_id or str(uuid.uuid4())
    with transaction(conn):
        conn.execute(
            "INSERT INTO applications (id, company, role, submitted_on, discovery_source,"
            " submission_channel, recruiter_type, platform_name, source_detail, job_url,"
            " location, notes, intake_folder) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                company,
                role,
                submitted_on,
                discovery_source,
                submission_channel,
                recruiter_type,
                _blank_to_none(platform_name),
                _blank_to_none(source_detail),
                _blank_to_none(job_url),
                _blank_to_none(location),
                _blank_to_none(notes),
                _blank_to_none(intake_folder),
            ),
        )
    return identifier


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def similar_applications(conn: sqlite3.Connection, company: str,
                         within_days: int = 14, submitted_on: str | None = None) -> list[sqlite3.Row]:
    """Applications for one employer (all of them unless a date window is given).

    Several postings per employer are normal, so the caller decides whether a
    row is a real duplicate; ``job_url`` is selected for exactly that decision.
    """
    sql = ("SELECT id, company, role, submitted_on, job_url FROM applications"
           " WHERE lower(trim(company)) = lower(trim(?))")
    params: list[object] = [company]
    if submitted_on:
        sql += " AND abs(julianday(submitted_on) - julianday(?)) <= ?"
        params.extend([submitted_on, within_days])
    sql += " ORDER BY submitted_on DESC"
    return all_rows(conn, sql, params)


def get_application(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM v_application_status WHERE id = ?", (app_id,))
    if row is None:
        raise UsageError(f"application not found: {app_id}")
    return row


def resolve_application(conn: sqlite3.Connection, ident: str) -> sqlite3.Row:
    """Accept a full UUID, a unique UUID prefix, or a unique company/role match."""
    ident = (ident or "").strip()
    if not ident:
        raise UsageError("no application given")
    row = one(conn, "SELECT * FROM v_application_status WHERE id = ?", (ident,))
    if row is not None:
        return row
    prefix_rows = all_rows(
        conn,
        "SELECT * FROM v_application_status WHERE id LIKE ? || '%' ORDER BY submitted_on DESC",
        (ident,),
    )
    if len(prefix_rows) == 1:
        return prefix_rows[0]
    if len(prefix_rows) > 1:
        raise UsageError(_ambiguous(ident, prefix_rows))
    exact = all_rows(
        conn,
        "SELECT * FROM v_application_status WHERE lower(company) = lower(?)"
        " OR lower(role) = lower(?) OR lower(company || ' ' || role) = lower(?)",
        (ident, ident, ident),
    )
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise UsageError(_ambiguous(ident, exact))
    like = all_rows(
        conn,
        "SELECT * FROM v_application_status WHERE lower(company) LIKE '%' || lower(?) || '%'"
        " OR lower(role) LIKE '%' || lower(?) || '%' ORDER BY submitted_on DESC",
        (ident, ident),
    )
    if len(like) == 1:
        return like[0]
    if not like:
        raise UsageError(f"no application matches '{ident}'")
    raise UsageError(_ambiguous(ident, like))


def _ambiguous(ident: str, rows: Sequence[sqlite3.Row]) -> str:
    lines = [f"'{ident}' matches several applications:"]
    for row in rows[:10]:
        lines.append(f"  {short_id(row['id'])}  {row['company']} - {row['role']}"
                     f" ({row['submitted_on']}, {row['current_status']})")
    if len(rows) > 10:
        lines.append(f"  ... and {len(rows) - 10} more")
    lines.append("Use a longer id prefix.")
    return "\n".join(lines)


def short_id(app_id: str, length: int = 8) -> str:
    return app_id[:length]


def application_id_exists(conn: sqlite3.Connection, app_id: str) -> bool:
    """True when this id belongs to a registered application.

    Application ids double as artifact-directory names (``applications/<uuid>``),
    so this also tells an intake folder apart from an application directory.
    """
    return scalar(conn, "SELECT 1 FROM applications WHERE id = ?", (app_id,)) is not None


def application_ids(conn: sqlite3.Connection) -> set[str]:
    """Every registered application id (the artifact directory names)."""
    return {str(row[0]) for row in all_rows(conn, "SELECT id FROM applications")}


def applications_for_linking(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows the intake-folder linker needs: identity, employer, url, current link."""
    return all_rows(
        conn,
        "SELECT id, company, role, job_url, intake_folder FROM applications"
        " ORDER BY submitted_on, created_at",
    )


def linked_intake_folders(conn: sqlite3.Connection) -> dict[str, str]:
    """Map intake folder name -> application id for every registered origin.

    A folder used for more than one (re)application maps to the newest
    application, which is the one that folder currently belongs to.
    """
    rows = all_rows(
        conn,
        "SELECT id, intake_folder FROM applications WHERE intake_folder IS NOT NULL"
        " ORDER BY submitted_on, created_at",
    )
    return {str(row["intake_folder"]): str(row["id"]) for row in rows}


def update_application(conn: sqlite3.Connection, app_id: str, **fields) -> list[str]:
    unknown = set(fields) - set(APPLICATION_FIELDS)
    if unknown:
        raise UsageError("cannot update application field(s): " + ", ".join(sorted(unknown)))
    if not fields:
        return []
    for key in ("discovery_source", "submission_channel", "recruiter_type"):
        if key in fields and fields[key] is not None:
            require_lookup(conn, key, fields[key])
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = [fields[name] for name in fields]
    with transaction(conn):
        conn.execute(f"UPDATE applications SET {assignments} WHERE id = ?", (*values, app_id))
    return sorted(fields)


def application_status(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row:
    return get_application(conn, app_id)


def state_as_of(conn: sqlite3.Connection, app_id: str,
                on_date: str | None = None) -> tuple[str, sqlite3.Row | None]:
    """Latest state-changing event at or before on_date (None = all events)."""
    sql = ("SELECT e.id, e.event_type, e.occurred_on, e.day_order, t.application_state"
           " FROM events e JOIN event_types t ON t.code = e.event_type"
           " WHERE e.application_id = ? AND t.application_state IS NOT NULL")
    params: list[object] = [app_id]
    if on_date:
        sql += " AND e.occurred_on <= ?"
        params.append(on_date)
    sql += " ORDER BY e.occurred_on DESC, e.day_order DESC LIMIT 1"
    row = one(conn, sql, params)
    if row is None:
        return "in_progress", None
    return str(row["application_state"]), row


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------
def rounds_for(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return all_rows(
        conn, "SELECT * FROM v_round_status WHERE application_id = ? ORDER BY sequence_no",
        (app_id,),
    )


def round_row(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM v_round_status WHERE id = ?", (round_id,))
    if row is None:
        raise UsageError(f"round not found: {round_id}")
    return row


def find_round(conn: sqlite3.Connection, app_id: str, ident: str | int) -> sqlite3.Row:
    """Look a round up by round id or by sequence number within the application."""
    text = str(ident).strip()
    if text.isdigit():
        number = int(text)
        row = one(conn, "SELECT * FROM v_round_status WHERE application_id = ? AND id = ?",
                  (app_id, number))
        if row is not None:
            return row
        row = one(conn,
                  "SELECT * FROM v_round_status WHERE application_id = ? AND sequence_no = ?",
                  (app_id, number))
        if row is not None:
            return row
    raise UsageError(f"no round '{ident}' in this application")


def find_round_by_id(conn: sqlite3.Connection, round_id: int, app_id: str) -> sqlite3.Row:
    """One round of a known application; refuses rounds of other applications."""
    row = one(conn, "SELECT * FROM v_round_status WHERE id = ?", (round_id,))
    if row is None:
        raise UsageError(f"round not found: {round_id}")
    if str(row["application_id"]) != app_id:
        raise UsageError(f"round {round_id} belongs to another application")
    return row


def round_is_disposable(conn: sqlite3.Connection, round_id: int, event_id: int) -> bool:
    """True when a round holds nothing except one event and no added details.

    Used by undo to remove a round that was created together with an event, and
    only then: a round with other events, participants, a label, or notes is
    kept, because the extra data may be intentional.
    """
    row = one(conn, "SELECT label, notes FROM rounds WHERE id = ?", (round_id,))
    if row is None:
        return False
    if (row["label"] or "").strip() or (row["notes"] or "").strip():
        return False
    other_events = int(scalar(
        conn, "SELECT count(*) FROM events WHERE round_id = ? AND id != ?", (round_id, event_id)
    ) or 0)
    participants = int(scalar(
        conn, "SELECT count(*) FROM round_participants WHERE round_id = ?", (round_id,)
    ) or 0)
    return other_events == 0 and participants == 0


def create_round(conn: sqlite3.Connection, app_id: str, *, phase: str, round_type: str,
                 fmt: str = "unknown", label: str | None = None, notes: str | None = None,
                 participants: Iterable[str] = ()) -> int:
    require_lookup(conn, "round_type", round_type)
    require_lookup(conn, "format", fmt)
    for role in participants:
        require_lookup(conn, "participant_role", role)
    if phase not in ("screening", "interview", "assessment"):
        raise UsageError(f"invalid phase '{phase}' (screening, interview, or assessment)")
    with transaction(conn):
        sequence = int(scalar(
            conn,
            "SELECT coalesce(max(sequence_no), 0) + 1 FROM rounds WHERE application_id = ?",
            (app_id,),
        ))
        cursor = conn.execute(
            "INSERT INTO rounds (application_id, sequence_no, phase, round_type, format,"
            " label, notes) VALUES (?,?,?,?,?,?,?)",
            (app_id, sequence, phase, round_type, fmt, _blank_to_none(label), _blank_to_none(notes)),
        )
        round_id = int(cursor.lastrowid)
        for role in participants:
            conn.execute(
                "INSERT OR IGNORE INTO round_participants (round_id, participant_role)"
                " VALUES (?,?)",
                (round_id, role),
            )
    return round_id


def update_round(conn: sqlite3.Connection, round_id: int, **fields) -> list[str]:
    unknown = set(fields) - set(ROUND_FIELDS)
    if unknown:
        raise UsageError("cannot update round field(s): " + ", ".join(sorted(unknown)))
    if not fields:
        return []
    if "round_type" in fields:
        require_lookup(conn, "round_type", fields["round_type"])
    if "format" in fields:
        require_lookup(conn, "format", fields["format"])
    if "phase" in fields and fields["phase"] not in ("screening", "interview", "assessment"):
        raise UsageError(f"invalid phase '{fields['phase']}'")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = [fields[name] for name in fields]
    with transaction(conn):
        conn.execute(f"UPDATE rounds SET {assignments} WHERE id = ?", (*values, round_id))
    return sorted(fields)


def participants_for(conn: sqlite3.Connection, round_id: int) -> list[str]:
    rows = all_rows(conn, "SELECT participant_role FROM round_participants"
                          " WHERE round_id = ? ORDER BY participant_role", (round_id,))
    return [str(row[0]) for row in rows]


def set_participants(conn: sqlite3.Connection, round_id: int, roles: Iterable[str]) -> None:
    roles = list(dict.fromkeys(roles))
    for role in roles:
        require_lookup(conn, "participant_role", role)
    with transaction(conn):
        conn.execute("DELETE FROM round_participants WHERE round_id = ?", (round_id,))
        for role in roles:
            conn.execute("INSERT INTO round_participants (round_id, participant_role) VALUES (?,?)",
                         (round_id, role))


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def add_event(
    conn: sqlite3.Connection,
    app_id: str,
    event_type: str,
    occurred_on: str,
    *,
    round_id: int | None = None,
    scheduled_for: str | None = None,
    scheduled_time: str | None = None,
    timezone: str | None = None,
    reason_code: str | None = None,
    notes: str | None = None,
    position: int | None = None,
    before_event_id: int | None = None,
    after_event_id: int | None = None,
) -> int:
    event_type_row(conn, event_type)
    if round_id is not None:
        row = one(conn, "SELECT application_id FROM rounds WHERE id = ?", (round_id,))
        if row is None:
            raise UsageError(f"round {round_id} does not exist")
        if row["application_id"] != app_id:
            raise UsageError(f"round {round_id} belongs to another application")
    with transaction(conn):
        if position is None:
            position = int(scalar(
                conn,
                "SELECT coalesce(max(day_order), 0) + 1 FROM events"
                " WHERE application_id = ? AND occurred_on = ?",
                (app_id, occurred_on),
            ))
        cursor = conn.execute(
            "INSERT INTO events (application_id, round_id, event_type, occurred_on, day_order,"
            " scheduled_for, scheduled_time, timezone, reason_code, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                app_id,
                round_id,
                event_type,
                occurred_on,
                position,
                scheduled_for,
                scheduled_time,
                timezone,
                _blank_to_none(reason_code),
                _blank_to_none(notes),
            ),
        )
        event_id = int(cursor.lastrowid)
        if before_event_id is not None or after_event_id is not None:
            _reorder(conn, event_id, before_event_id, after_event_id)
    return event_id


def _day_events(conn: sqlite3.Connection, app_id: str, occurred_on: str) -> list[sqlite3.Row]:
    return all_rows(
        conn,
        "SELECT id, day_order FROM events WHERE application_id = ? AND occurred_on = ?"
        " ORDER BY day_order",
        (app_id, occurred_on),
    )


def _reorder(conn: sqlite3.Connection, event_id: int, before_event_id: int | None,
             after_event_id: int | None) -> None:
    """Position one event within its day using temporary high positions."""
    row = one(conn, "SELECT application_id, occurred_on FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise UsageError(f"event {event_id} does not exist")
    app_id, occurred_on = row["application_id"], row["occurred_on"]
    day = [r["id"] for r in _day_events(conn, app_id, occurred_on)]
    if event_id not in day:
        raise UsageError("event is not on that day")
    day.remove(event_id)
    anchor = before_event_id if before_event_id is not None else after_event_id
    if anchor is None:
        raise UsageError("a reorder needs --before or --after")
    if anchor not in day:
        raise UsageError("the anchor event is not on the same day")
    index = day.index(anchor)
    if before_event_id is not None:
        day.insert(index, event_id)
    else:
        day.insert(index + 1, event_id)
    _write_order(conn, day)


def _write_order(conn: sqlite3.Connection, ordered_ids: Sequence[int]) -> None:
    if not ordered_ids:
        return
    start = int(scalar(conn, "SELECT coalesce(max(day_order), 0) FROM events WHERE application_id ="
                             " (SELECT application_id FROM events WHERE id = ?)",
                       (ordered_ids[0],)))
    for offset, event_id in enumerate(ordered_ids):
        conn.execute("UPDATE events SET day_order = ? WHERE id = ?",
                     (start + offset + 1, event_id))
    for offset, event_id in enumerate(ordered_ids):
        conn.execute("UPDATE events SET day_order = ? WHERE id = ?", (offset + 1, event_id))


def reorder_event(conn: sqlite3.Connection, event_id: int, *, before: int | None = None,
                  after: int | None = None) -> None:
    with transaction(conn):
        _reorder(conn, event_id, before, after)


def move_event_date(conn: sqlite3.Connection, event_id: int, occurred_on: str) -> None:
    with transaction(conn):
        _move_event_date(conn, event_id, occurred_on)


def _move_event_date(conn: sqlite3.Connection, event_id: int, occurred_on: str) -> None:
    row = one(conn, "SELECT application_id, occurred_on FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise UsageError(f"event {event_id} does not exist")
    app_id = row["application_id"]
    old_date = row["occurred_on"]
    if old_date == occurred_on:
        return
    top = int(scalar(conn, "SELECT coalesce(max(day_order), 0) FROM events"
                           " WHERE application_id = ?", (app_id,)))
    conn.execute("UPDATE events SET day_order = ? WHERE id = ?", (top + 1, event_id))
    position = int(scalar(
        conn,
        "SELECT coalesce(max(day_order), 0) + 1 FROM events"
        " WHERE application_id = ? AND occurred_on = ?",
        (app_id, occurred_on),
    ))
    conn.execute("UPDATE events SET occurred_on = ?, day_order = ? WHERE id = ?",
                 (occurred_on, position, event_id))
    remaining = [r["id"] for r in _day_events(conn, app_id, old_date)]
    if remaining:
        _write_order(conn, remaining)


def update_event(conn: sqlite3.Connection, event_id: int, **fields) -> list[str]:
    unknown = set(fields) - set(EVENT_FIELDS)
    if unknown:
        raise UsageError("cannot update event field(s): " + ", ".join(sorted(unknown)))
    if not fields:
        return []
    row = one(conn, "SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise UsageError(f"event {event_id} does not exist")
    if "event_type" in fields:
        event_type_row(conn, fields["event_type"])
    if "round_id" in fields and fields["round_id"] is not None:
        target = one(conn, "SELECT application_id FROM rounds WHERE id = ?", (fields["round_id"],))
        if target is None:
            raise UsageError(f"round {fields['round_id']} does not exist")
        if target["application_id"] != row["application_id"]:
            raise UsageError("round belongs to another application")
    date_change = fields.pop("occurred_on", None)
    with transaction(conn):
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = [fields[name] for name in fields]
            conn.execute(f"UPDATE events SET {assignments} WHERE id = ?", (*values, event_id))
        if date_change and date_change != row["occurred_on"]:
            _move_event_date(conn, event_id, date_change)
    return sorted(set(fields) | ({"occurred_on"} if date_change else set()))


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = one(
        conn,
        "SELECT e.*, t.label AS event_label, t.scope, t.application_state, t.round_state"
        " FROM events e JOIN event_types t ON t.code = e.event_type WHERE e.id = ?",
        (event_id,),
    )
    if row is None:
        raise UsageError(f"event {event_id} does not exist")
    return row


def timeline(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return all_rows(
        conn,
        "SELECT * FROM v_application_timeline WHERE application_id = ?"
        " ORDER BY occurred_on, day_order",
        (app_id,),
    )


def last_event(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row | None:
    return one(conn, "SELECT * FROM events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
               (app_id,))


# --------------------------------------------------------------------------
# delete previews and deletes
# --------------------------------------------------------------------------
def deletion_preview(conn: sqlite3.Connection, kind: str, ident: int | str) -> dict[str, int]:
    if kind == "event":
        event = get_event(conn, int(ident))
        return {
            "events": 1,
            "application": event["application_id"],
            "event": f"{event['event_label']} on {event['occurred_on']}",
        }
    if kind == "round":
        round_id = int(ident)
        row = round_row(conn, round_id)
        events = int(scalar(conn, "SELECT count(*) FROM events WHERE round_id = ?", (round_id,)))
        return {
            "events": events,
            "round": f"#{row['sequence_no']} {row['phase']} {row['round_type']}",
            "application": row["application_id"],
            "participants": int(scalar(conn, "SELECT count(*) FROM round_participants"
                                             " WHERE round_id = ?", (round_id,))),
        }
    if kind == "application":
        app = get_application(conn, str(ident))
        return {
            "events": int(scalar(conn, "SELECT count(*) FROM events WHERE application_id = ?",
                                 (app["id"],))),
            "rounds": int(scalar(conn, "SELECT count(*) FROM rounds WHERE application_id = ?",
                                 (app["id"],))),
            "artifacts": int(scalar(conn, "SELECT count(*) FROM artifacts WHERE application_id = ?",
                                    (app["id"],))),
            "application": f"{app['company']} - {app['role']}",
        }
    raise UsageError(f"unknown delete target '{kind}'")


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        if cursor.rowcount == 0:
            raise UsageError(f"event {event_id} does not exist")


def delete_round(conn: sqlite3.Connection, round_id: int) -> None:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
        if cursor.rowcount == 0:
            raise UsageError(f"round {round_id} does not exist")


def delete_application(conn: sqlite3.Connection, app_id: str) -> None:
    with transaction(conn):
        cursor = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        if cursor.rowcount == 0:
            raise UsageError(f"application {app_id} does not exist")


# --------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------
def add_artifact(conn: sqlite3.Connection, app_id: str, artifact_type: str,
                 relative_path: str, label: str | None = None) -> int:
    require_lookup(conn, "artifact_type", artifact_type)
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO artifacts (application_id, artifact_type, relative_path, label)"
            " VALUES (?,?,?,?)",
            (app_id, artifact_type, relative_path, _blank_to_none(label)),
        )
    return int(cursor.lastrowid)


def update_artifact(conn: sqlite3.Connection, artifact_id: int, *,
                    artifact_type: str | None = None, label: str | None = None) -> None:
    fields = {}
    if artifact_type is not None:
        require_lookup(conn, "artifact_type", artifact_type)
        fields["artifact_type"] = artifact_type
    if label is not None:
        fields["label"] = _blank_to_none(label)
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with transaction(conn):
        conn.execute(f"UPDATE artifacts SET {assignments} WHERE id = ?",
                     (*fields.values(), artifact_id))


def artifacts_for(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return all_rows(conn, "SELECT * FROM artifacts WHERE application_id = ? ORDER BY id", (app_id,))


def all_artifacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return all_rows(conn, "SELECT * FROM artifacts ORDER BY relative_path")


def delete_artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row:
    with transaction(conn):
        row = one(conn, "SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if row is None:
            raise UsageError(f"artifact {artifact_id} does not exist")
        conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    return row
