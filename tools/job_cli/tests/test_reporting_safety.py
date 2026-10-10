"""F1, F8, F9 regression tests for reporting.

F1: a replay metadata file is data, never executable SQL.
F8: employer-origin response events exclude applicant actions.
F9: every export is a new, self-contained pair; nothing is silently overwritten.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from helpers import find_id, payload, read_edges, rows
from job_cli import db as dbmod
from job_cli import reporting

MALICIOUS_REPLAY_SQL = [
    "DELETE FROM applications RETURNING completed_interviews, received_offer, status",
    "DELETE FROM events RETURNING event_type, occurred_on, day_order",
    "UPDATE applications SET company = 'stolen'"
    " RETURNING completed_interviews, received_offer, status",
    "INSERT INTO applications (id, company, role, submitted_on)"
    " VALUES ('evil', 'Evil', 'Evil', '2026-01-01')"
    " RETURNING completed_interviews, received_offer, status",
    "WITH doomed AS (SELECT id FROM applications)"
    " DELETE FROM applications WHERE id IN (SELECT id FROM doomed)"
    " RETURNING completed_interviews, received_offer, status",
    "DROP TABLE applications",
]


def _app_ids(run) -> list[str]:
    return sorted(row["id"] for row in rows(run, "list", "--json"))


def _metadata(out_dir: Path) -> dict:
    return json.loads((out_dir / "sankey-metadata.json").read_text(encoding="utf-8"))


def _write_edges(path: Path, edges) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "target", "value"])
        for (source, target), value in sorted(edges.items()):
            writer.writerow([source, target, value])


def _frozen_payload(run, directory: Path, query_sql: str) -> Path:
    """A metadata file that carries the malicious SQL and matching frozen groups."""
    directory.mkdir(parents=True, exist_ok=True)
    cohort = len(_app_ids(run))
    frozen_groups = [{"screens": 0, "assessments": 0, "interviews": 0, "received_offer": 0,
                      "pending": None, "endpoint": "in_progress", "count": cohort}]
    edges = reporting.build_sankey_from_groups(frozen_groups)
    edges_path = directory / "sankey-edges.csv"
    _write_edges(edges_path, edges)
    metadata = {
        "kind": "sankey_aggregate",
        "generated_at": "2026-01-01T00:00:00Z",
        "filters": {"as_of": "2026-06-01", "cohort_from": None, "cohort_to": None},
        "cohort_size": cohort,
        "query_sql": query_sql,
        "edges_file": "sankey-edges.csv",
        "frozen": {
            "format": reporting.FROZEN_FORMAT,
            "group_fields": list(reporting.GROUP_FIELDS),
            "group_count": len(frozen_groups),
            "application_count": cohort,
            "groups": frozen_groups,
            "edges_sha256": hashlib.sha256(edges_path.read_text().encode("utf-8")).hexdigest(),
        },
    }
    meta = directory / "sankey-metadata.json"
    meta.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return meta


# --------------------------------------------------------------------------
# F1 - replay metadata is data
# --------------------------------------------------------------------------
@pytest.mark.parametrize("query_sql", MALICIOUS_REPLAY_SQL)
def test_replay_never_executes_metadata_sql(run, new_app, log_ok, tmp_path, query_sql):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    before = _app_ids(run)
    assert before

    meta = _frozen_payload(run, tmp_path / "evil", query_sql)
    result = run("export", "replay", str(meta), "--json")
    assert result.ok, result.output
    data = json.loads(result.output)

    assert data["executed_metadata_sql"] is False
    assert _app_ids(run) == before  # nothing was deleted, updated, or inserted
    assert len(rows(run, "list", "--json")) == len(before)
    assert data["frozen_reproduced"] is True  # edges came from the frozen rows
    assert data["pair_consistent"] is True


@pytest.mark.parametrize("query_sql", MALICIOUS_REPLAY_SQL)
def test_replay_direct_call_cannot_write(run, new_app, tmp_path, query_sql):
    new_app()
    before = _app_ids(run)
    meta = _frozen_payload(run, tmp_path / "direct", query_sql)
    conn = dbmod.connect(run.db)
    try:
        reporting.replay_sankey(conn, meta, layout=None)
        remaining = [row[0] for row in conn.execute("SELECT id FROM applications")]
    finally:
        conn.close()
    assert sorted(remaining) == before


def test_replay_refuses_metadata_without_frozen_rows(run, new_app, tmp_path):
    new_app()
    before = _app_ids(run)
    directory = tmp_path / "nostfrozen"
    directory.mkdir()
    (directory / "sankey-edges.csv").write_text("source,target,value\n")
    meta = directory / "sankey-metadata.json"
    meta.write_text(json.dumps({
        "kind": "sankey_aggregate",
        "filters": {"as_of": "2026-06-01", "cohort_from": None, "cohort_to": None},
        "cohort_size": 1,
        "query_sql": "DELETE FROM applications"
        " RETURNING completed_interviews, received_offer, status",
    }) + "\n")
    result = run("export", "replay", str(meta))
    assert result.exit_code == 2
    assert "frozen" in result.output
    assert _app_ids(run) == before


def test_replay_rejects_edges_file_path_traversal(run, new_app, tmp_path):
    new_app()
    secret = tmp_path / "secret.csv"
    secret.write_text("source,target,value\nall_applications,end_rejected,1\n")
    directory = tmp_path / "traversal"
    meta = _frozen_payload(run, directory, "SELECT 1")
    payload_json = json.loads(meta.read_text())
    payload_json["edges_file"] = "../secret.csv"
    meta.write_text(json.dumps(payload_json) + "\n")
    result = run("export", "replay", str(meta))
    assert result.exit_code == 2
    assert "edges_file" in result.output


def test_replay_reports_unavailable_comparison_not_a_false_match(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "pair"
    assert run("export", "sankey", "--output", str(out)).ok
    empty = tmp_path / "empty.sqlite3"
    conn = dbmod.connect(empty)
    try:
        data = reporting.replay_sankey(conn, out / "sankey-metadata.json", layout=None)
    finally:
        conn.close()
    assert data["frozen_reproduced"] is True
    assert data["current_comparison"] == "unavailable"
    assert data["current_note"]
    assert data["reproduced"] is True  # frozen pair verifies; nothing else was claimed


# --------------------------------------------------------------------------
# F9 - versioned, coherent, self-contained exports
# --------------------------------------------------------------------------
def test_default_export_uses_unique_versioned_directories(run, new_app):
    new_app()
    first = payload(run, "--json", "export", "sankey")
    second = payload(run, "--json", "export", "sankey")
    first_dir = Path(first["edges"]).parent
    second_dir = Path(second["edges"]).parent
    assert first_dir != second_dir
    assert first_dir.parent == run.root / "exports"
    assert first_dir.name.startswith("sankey-")
    for directory in (first_dir, second_dir):
        assert (directory / "sankey-edges.csv").exists()
        assert (directory / "sankey-metadata.json").exists()
        assert not list(directory.glob("*.tmp-*"))
        assert not list(directory.glob("*.tmp"))


def test_explicit_output_refuses_to_overwrite_an_existing_pair(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "pair"
    assert run("export", "sankey", "--output", str(out)).ok
    edges_before = (out / "sankey-edges.csv").read_bytes()
    meta_before = (out / "sankey-metadata.json").read_bytes()
    again = run("export", "sankey", "--output", str(out))
    assert again.exit_code == 2
    assert "refusing" in again.output
    assert (out / "sankey-edges.csv").read_bytes() == edges_before
    assert (out / "sankey-metadata.json").read_bytes() == meta_before


def test_export_pair_is_self_contained_and_verifiable(run, new_app, log_ok, new_round, tmp_path):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-10", "--round", "1")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    out = tmp_path / "self"
    assert run("export", "sankey", "--output", str(out)).ok
    meta = _metadata(out)
    csv_text = (out / "sankey-edges.csv").read_text(encoding="utf-8")

    assert "query_sql" not in meta
    assert meta["frozen"]["format"] == reporting.FROZEN_FORMAT
    assert meta["frozen"]["edges_sha256"] == hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    assert meta["frozen"]["application_count"] == meta["cohort_size"] == 1

    groups = meta["frozen"]["groups"]
    assert groups == sorted(groups, key=lambda g: (g["interviews"], g["received_offer"],
                                                   g["endpoint"]))
    for group in groups:
        assert set(group) == set(reporting.GROUP_FIELDS)
        assert group["interviews"] in reporting.INTERVIEW_BUCKETS
        assert group["received_offer"] in (0, 1)
        assert group["endpoint"] in reporting.SANKEY_ENDPOINTS
        assert isinstance(group["count"], int) and not isinstance(group["count"], bool)
        assert group["count"] >= 1
    assert sum(group["count"] for group in groups) == meta["cohort_size"]

    replayed = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert replayed["pair_consistent"] is True
    assert replayed["frozen_reproduced"] is True
    assert replayed["current_comparison"] == "matches"
    assert replayed["reproduced"] is True
    assert reporting.build_sankey_from_groups(groups) == read_edges(
        out / "sankey-edges.csv")


def test_export_frozen_groups_carry_no_identifying_data(run, new_app, log_ok, tmp_path):
    app_id = new_app("Acme GmbH", "Secret Role", "2026-01-05")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20",
           "--notes", "private note text")
    out = tmp_path / "privacy"
    assert run("export", "sankey", "--output", str(out)).ok
    text = (out / "sankey-metadata.json").read_text(encoding="utf-8")
    for secret in ("Acme", "Secret Role", app_id, app_id[:8], "private note text",
                   "https://example.invalid/posting"):
        assert secret not in text
    frozen_text = json.dumps(_metadata(out)["frozen"])
    assert "2026-01-05" not in frozen_text  # no dates, not even the submitted date


def test_replay_detects_a_tampered_pair(run, new_app, log_ok, tmp_path):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    out = tmp_path / "tamper"
    assert run("export", "sankey", "--output", str(out)).ok
    edges = read_edges(out / "sankey-edges.csv")
    edges[("all_applications", "end_rejected")] = 99
    _write_edges(out / "sankey-edges.csv", edges)

    data = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert data["pair_consistent"] is False
    assert data["frozen_reproduced"] is False
    assert data["reproduced"] is False
    assert data["frozen_differences"]


def test_replay_still_reports_later_corrections(run, new_app, log_ok, tmp_path):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    out = tmp_path / "corrections"
    assert run("export", "sankey", "--output", str(out)).ok
    fresh = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert fresh["reproduced"] is True
    assert fresh["frozen_reproduced"] is True

    log_ok("Acme GmbH", "--event", "reopened", "--date", "2026-01-25")
    again = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert again["reproduced"] is False
    assert again["frozen_reproduced"] is True  # the frozen pair is still self-consistent
    assert again["current_comparison"] == "differs"
    assert again["differences"]


# --------------------------------------------------------------------------
# F8 - employer-origin events
# --------------------------------------------------------------------------
ALL_EVENT_TYPES = sorted(reporting.EMPLOYER_RESPONSE_EVENTS | reporting.APPLICANT_ACTION_EVENTS)

APPLICANT_ACTIONS = ("assessment_submitted", "offer_accepted", "offer_declined",
                     "job_started", "withdrawn", "follow_up_sent", "note",
                     "marked_inactive", "reopened")


def test_employer_and_applicant_event_sets_are_disjoint():
    assert reporting.EMPLOYER_RESPONSE_EVENTS.isdisjoint(reporting.APPLICANT_ACTION_EVENTS)
    for code in APPLICANT_ACTIONS:
        assert code in reporting.APPLICANT_ACTION_EVENTS
        assert code not in reporting.EMPLOYER_RESPONSE_EVENTS


@pytest.mark.parametrize("event_type", ALL_EVENT_TYPES)
def test_first_response_counts_only_employer_events(event_type):
    events = [
        {"event_type": "note", "occurred_on": "2026-01-02"},
        {"event_type": event_type, "occurred_on": "2026-01-11"},
    ]
    expected = 10 if event_type in reporting.EMPLOYER_RESPONSE_EVENTS else None
    assert reporting.first_employer_response_days("2026-01-01", events) == expected


def test_event_type_partition_matches_the_database(run):
    assert run("db", "status").ok
    conn = dbmod.connect(run.db)
    try:
        codes = {row[0] for row in conn.execute("SELECT code FROM event_types")}
    finally:
        conn.close()
    assert codes == set(reporting.EMPLOYER_RESPONSE_EVENTS) | set(reporting.APPLICANT_ACTION_EVENTS)
    stats = payload(run, "stats", "--json")
    responses = stats["responses"]
    assert set(responses["excluded_event_types"]) == codes - reporting.EMPLOYER_RESPONSE_EVENTS
    assert set(responses["employer_response_event_types"]) == reporting.EMPLOYER_RESPONSE_EVENTS
    assert set(responses["applicant_action_event_types"]) == reporting.APPLICANT_ACTION_EVENTS


def test_assessment_submitted_alone_is_not_an_employer_response(run, new_app, log_ok):
    """The review reproduction: one applicant action must not count as a response."""
    new_app("Acme GmbH", "Engineer", "2026-09-02")
    log_ok("Acme GmbH", "--event", "assessment_submitted", "--date", "2026-09-10",
           "--round", "new", "--phase", "assessment", "--round-type", "take_home")
    stats = payload(run, "stats", "--as-of", "2026-09-11",
                    "--from", "2026-09-02", "--to", "2026-09-02", "--json")
    assert stats["total_applications"] == 1
    assert stats["responses"]["applications_answered"] == 0
    assert stats["responses"]["applications_without_employer_event"] == 1
    assert stats["responses"]["first_response_median_days"] is None
    assert "assessment_submitted" in stats["responses"]["excluded_event_types"]


@pytest.mark.parametrize("event_type,extra,days", [
    ("rejected", (), 10),
    ("invited", ("--round", "new", "--phase", "interview", "--round-type", "general",
                 "--format", "video"), 10),
    ("assessment_received", ("--round", "new", "--phase", "assessment",
                            "--round-type", "take_home"), 10),
    ("offer_received", (), 10),
    ("position_closed", (), 10),
])
def test_employer_events_count_as_a_response(run, new_app, log_ok, event_type, extra, days):
    new_app("Acme GmbH", "Engineer", "2026-01-01")
    log_ok("Acme GmbH", "--event", event_type, "--date", "2026-01-11", *extra)
    stats = payload(run, "stats", "--as-of", "2026-02-01", "--json")
    assert stats["responses"]["applications_answered"] == 1
    assert stats["responses"]["applications_without_employer_event"] == 0
    assert stats["responses"]["first_response_median_days"] == float(days)


@pytest.mark.parametrize("event_type,extra", [
    ("offer_accepted", ("--date", "2026-01-11")),
    ("job_started", ("--date", "2026-01-11")),
])
def test_decision_events_do_not_replace_an_earlier_employer_response(
        run, new_app, log_ok, event_type, extra):
    """offer_accepted/job_started are applicant actions: they never create a response."""
    new_app("Acme GmbH", "Engineer", "2026-01-01")
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-06")
    if event_type == "job_started":
        log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-10")
    log_ok("Acme GmbH", "--event", event_type, *extra)
    stats = payload(run, "stats", "--as-of", "2026-02-01", "--json")
    assert stats["responses"]["applications_answered"] == 1
    # the response is the employer offer on 2026-01-06, never the applicant action
    assert stats["responses"]["first_response_median_days"] == 5.0


def test_replay_rejects_malformed_files_without_a_traceback(run, new_app, tmp_path):
    new_app()
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json")
    result = run("export", "replay", str(bad_json))
    assert result.exit_code == 2
    assert "valid JSON" in result.output

    missing = tmp_path / "missing.json"
    result = run("export", "replay", str(missing))
    assert result.exit_code == 2
    assert "cannot read" in result.output

    out = tmp_path / "pair"
    assert run("export", "sankey", "--output", str(out)).ok
    (out / "sankey-edges.csv").write_text(
        "source,target,value\nall_applications,end_rejected,not-an-integer\n")
    result = run("export", "replay", str(out / "sankey-metadata.json"))
    assert result.exit_code == 2
    assert "not a sankey edge CSV" in result.output

    (out / "sankey-edges.csv").write_text("only-a-header\n")
    data = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert data["edges_file_present"] is True
    assert data["frozen_reproduced"] is False  # a header-only CSV lost every stored edge
    assert data["pair_consistent"] is False
