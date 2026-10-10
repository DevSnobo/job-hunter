"""Cohort statistics, private snapshots, and the aggregate Sankey export."""
from __future__ import annotations

import json
from pathlib import Path

from helpers import find_id, payload, read_edges, rows


def build_story(run, log_ok, new_round):
    """Two applications: one rejected after two interviews, one accepted with 4."""
    first = find_id(run, "Acme GmbH")
    new_round("Acme GmbH", "2026-01-06", "screening", "recruiter_screen", "phone")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    new_round("Acme GmbH", "2026-01-08", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-10", "--round", "2")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")

    run("add", "--company", "Beta AG", "--role", "Platform", "--submitted-on", "2026-01-09")
    for index, day in enumerate(["2026-01-15", "2026-01-20", "2026-01-25", "2026-01-30"]):
        log_ok("Beta AG", "--event", "completed", "--date", day, "--round", "new",
               "--phase", "interview", "--round-type", "general", "--format", "video")
    log_ok("Beta AG", "--event", "offer_received", "--date", "2026-02-01")
    log_ok("Beta AG", "--event", "offer_received", "--date", "2026-02-02",
           "--notes", "revised numbers")
    log_ok("Beta AG", "--event", "offer_accepted", "--date", "2026-02-03")
    return first


def test_sankey_shape_endpoints_and_collapse(run, new_app, log_ok, new_round, tmp_path):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Beta AG", "Platform", "2026-01-09")
    build_story(run, log_ok, new_round)
    out = tmp_path / "export"
    result = run("export", "sankey", "--output", str(out))
    assert result.ok, result.output
    edges = read_edges(out / "sankey-edges.csv")
    metadata = json.loads((out / "sankey-metadata.json").read_text())

    assert metadata["cohort_size"] == 2
    assert sum(value for (source, _), value in edges.items() if source == "all_applications") == 2
    # Acme's first conversation is a screening, so it enters the flow through the
    # screening gate and only Beta reaches Interview 1 directly.
    assert edges[("all_applications", "screen_done")] == 1
    assert edges[("screen_done", "interview_1")] == 1
    assert edges[("all_applications", "interview_1")] == 1
    assert edges[("interview_2", "interview_3plus")] == 1
    assert edges[("interview_3plus", "offer_received")] == 1
    assert edges[("offer_received", "end_accepted")] == 1
    assert edges[("interview_1", "end_rejected")] == 1
    assert all(source != target for source, target in edges)
    assert not [source for source, _ in edges if source.startswith("end_")]
    terminal = {target for _, target in edges if target.startswith("end_")}
    assert terminal == {"end_accepted", "end_rejected"}
    assert sum(value for (_, target), value in edges.items()
               if target.startswith("end_")) == 2


def test_sankey_counts_offer_once_and_collapses_many_interviews(run, new_app, log_ok):
    new_app("Beta AG", "Platform", "2026-01-09")
    for index in range(5):
        log_ok("Beta AG", "--event", "completed", "--date", f"2026-01-1{index + 1}",
               "--round", "new", "--phase", "interview", "--round-type", "general")
    log_ok("Beta AG", "--event", "offer_received", "--date", "2026-02-01")
    log_ok("Beta AG", "--event", "offer_received", "--date", "2026-02-02")
    log_ok("Beta AG", "--event", "offer_declined", "--date", "2026-02-03")
    out = Path(run.tmp) / "export2"
    assert run("export", "sankey", "--output", str(out)).ok
    edges = read_edges(out / "sankey-edges.csv")
    assert edges[("all_applications", "interview_1")] == 1
    assert edges[("interview_3plus", "offer_received")] == 1
    assert edges[("offer_received", "end_offer_declined")] == 1
    assert len([key for key in edges if key[1] == "offer_received"]) == 1


def test_reopened_process_has_one_endpoint(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    log_ok("Acme GmbH", "--event", "reopened", "--date", "2026-01-15")
    out = Path(run.tmp) / "export3"
    assert run("export", "sankey", "--output", str(out)).ok
    edges = read_edges(out / "sankey-edges.csv")
    assert edges == {("all_applications", "end_in_progress"): 1}


def test_cutoff_excludes_later_facts_but_not_earlier_ones(run, new_app, log_ok, new_round):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-10", "--round", "1")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")

    before_submission = payload(run, "stats", "--as-of", "2026-01-04", "--json")
    assert before_submission["total_applications"] == 0

    before_invitation = payload(run, "stats", "--as-of", "2026-01-05", "--json")
    assert before_invitation["total_applications"] == 1
    assert before_invitation["completed_interviews"] == 0
    assert before_invitation["status_counts"] == {"in_progress": 1}

    before_completion = payload(run, "stats", "--as-of", "2026-01-08", "--json")
    assert before_completion["completed_interviews"] == 0
    assert before_completion["applications_with_interview"] == 0

    before_rejection = payload(run, "stats", "--as-of", "2026-01-15", "--json")
    assert before_rejection["completed_interviews"] == 1
    assert before_rejection["status_counts"] == {"in_progress": 1}

    after = payload(run, "stats", "--as-of", "2026-01-25", "--json")
    assert after["status_counts"] == {"rejected": 1}

    out = Path(run.tmp) / "wall"
    assert run("export", "sankey", "--as-of", "2026-01-15", "--output", str(out)).ok
    edges = read_edges(out / "sankey-edges.csv")
    assert edges == {("all_applications", "interview_1"): 1, ("interview_1", "end_in_progress"): 1}


def test_cohort_boundaries_are_inclusive(run, new_app):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Beta AG", "Platform", "2026-02-05")
    assert payload(run, "stats", "--from", "2026-01-05", "--to", "2026-01-05",
                   "--json")["total_applications"] == 1
    assert payload(run, "stats", "--from", "2026-02-05", "--to", "2026-03-01",
                   "--json")["total_applications"] == 1
    assert payload(run, "stats", "--from", "2026-01-05", "--to", "2026-02-05",
                   "--json")["total_applications"] == 2
    assert run("stats", "--from", "2026-03-01", "--to", "2026-01-01").exit_code == 2


def test_statistics_denominators_and_response_windows(run, new_app, log_ok):
    new_app("Acme GmbH", "Backend", "2026-01-01")
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-02")
    log_ok("Acme GmbH", "--event", "follow_up_sent", "--date", "2026-01-03")
    log_ok("Acme GmbH", "--event", "invited", "--date", "2026-01-11", "--round", "new",
           "--phase", "interview", "--round-type", "general")
    new_app("Quiet AG", "Dev", "2026-01-01", "--force")
    stats = payload(run, "stats", "--as-of", "2026-02-01", "--json")
    assert stats["total_applications"] == 2
    assert stats["still_open"] == 2
    assert stats["responses"]["applications_answered"] == 1
    assert stats["responses"]["applications_without_employer_event"] == 1
    assert stats["responses"]["first_response_median_days"] == 10.0  # notes do not count
    assert "follow_up_sent" in stats["responses"]["excluded_event_types"]
    assert stats["denominators"]["interview_rate_denominator"] == 2


def test_aggregate_export_has_no_identifying_data(run, new_app, log_ok, new_round):
    app_id = new_app()
    build_story(run, log_ok, new_round)
    out = Path(run.tmp) / "share"
    assert run("export", "sankey", "--output", str(out)).ok
    text = (out / "sankey-edges.csv").read_text()
    metadata = (out / "sankey-metadata.json").read_text()
    for secret in ("Acme", "Beta", app_id, app_id[:8], "Backend", "Platform"):
        assert secret not in text
        assert secret not in metadata.replace(app_id, "")  # ids never appear anywhere
    assert "no company names" in metadata or "Aggregate only" in metadata


def test_private_snapshot_requires_explicit_choice(run, new_app):
    new_app()
    refused = run("export", "snapshot")
    assert refused.exit_code == 2 and "--private" in refused.output
    target = Path(run.tmp) / "private.json"
    result = run("export", "snapshot", "--private", "--output", str(target))
    assert result.ok, result.output
    payload_json = json.loads(target.read_text())
    assert payload_json["kind"] == "private_snapshot"
    assert payload_json["rows"][0]["company"] == "Acme GmbH"
    assert "snapshot.sql" in payload_json["query_file"]
    assert "PRIVATE" in json.dumps(payload_json["warning"])


def test_replay_reproduces_then_reports_later_corrections(run, new_app, log_ok, new_round):
    new_app()
    build_story(run, log_ok, new_round)
    out = Path(run.tmp) / "replay"
    assert run("export", "sankey", "--output", str(out)).ok
    meta = out / "sankey-metadata.json"
    fresh = payload(run, "export", "replay", str(meta), "--json")
    assert fresh["reproduced"] is True

    log_ok("Acme GmbH", "--event", "reopened", "--date", "2026-01-25")
    again = payload(run, "export", "replay", str(meta), "--json")
    assert again["reproduced"] is False
    assert again["differences"]


def test_snapshot_and_sankey_agree_on_the_cohort(run, new_app, log_ok, new_round, tmp_path):
    new_app()
    build_story(run, log_ok, new_round)
    out = tmp_path / "agree"
    assert run("export", "sankey", "--output", str(out)).ok
    metadata = json.loads((out / "sankey-metadata.json").read_text())
    snapshot = tmp_path / "snap.json"
    assert run("export", "snapshot", "--private", "--output", str(snapshot)).ok
    assert json.loads(snapshot.read_text())["row_count"] == metadata["cohort_size"]
    assert metadata["total_flow"] == sum(read_edges(out / "sankey-edges.csv").values())
