"""Daily workflow: add, log, rounds, events, corrections, and rule enforcement."""
from __future__ import annotations

from helpers import find_id, payload, rows


def test_add_and_show_minimal_application(run, new_app):
    app_id = new_app()
    detail = payload(run, "show", app_id, "--json")
    assert detail["application"]["current_status"] == "in_progress"
    assert detail["application"]["company"] == "Acme GmbH"
    assert [event["event_type"] for event in detail["timeline"]] == ["submitted"]
    assert detail["rounds"] == []


def test_add_rejects_future_submission(run):
    result = run("add", "--company", "Future", "--role", "Dev", "--submitted-on", "2099-01-01")
    assert result.exit_code == 2
    assert "future" in result.output


def test_add_questions_the_same_posting_and_needs_force(run, new_app):
    new_app()
    duplicate = run("add", "--company", "Acme GmbH", "--role", "Backend Engineer",
                    "--submitted-on", "2026-01-09")
    assert duplicate.exit_code == 2
    assert "already has an application for this posting" in duplicate.output
    assert "--force" in duplicate.output
    forced = run("add", "--company", "Acme GmbH", "--role", "Backend Engineer",
                 "--submitted-on", "2026-01-09", "--force")
    assert forced.ok, forced.output
    assert len(rows(run, "list", "--json")) == 2


def test_a_second_posting_at_one_employer_needs_no_confirmation(run, new_app):
    new_app()
    second = run("add", "--company", "Acme GmbH", "--role", "Data Engineer",
                 "--submitted-on", "2026-01-09")
    assert second.ok, second.output
    assert "a different posting" in second.stderr
    assert len(rows(run, "list", "--json")) == 2


def test_the_job_url_decides_whether_an_entry_is_the_same_posting(run, new_app):
    new_app("Acme GmbH", "Backend Engineer", "2026-01-05", "--url", "https://example.com/a")
    other = run("add", "--company", "Acme GmbH", "--role", "Backend Engineer",
                "--submitted-on", "2026-01-06", "--url", "https://example.com/b")
    assert other.ok, other.output      # the same title, but a different posting
    same = run("add", "--company", "Acme GmbH", "--role", "Backend Engineer",
               "--submitted-on", "2026-01-07", "--url", "https://example.com/a/")
    assert same.exit_code == 2         # the same url; the trailing slash is not a difference
    assert len(rows(run, "list", "--json")) == 2


def test_resolve_by_prefix_company_and_role(run, new_app):
    app_id = new_app()
    for ident in (app_id[:8], "Acme GmbH", "Backend Engineer", app_id):
        assert run("show", ident).ok, ident


def test_resolve_reports_ambiguity(run, new_app):
    new_app("Acme GmbH", "One", "2026-01-05")
    new_app("Acme GmbH", "Two", "2026-01-06", "--force")
    result = run("show", "Acme")
    assert result.exit_code == 2
    assert "matches several" in result.output


def test_full_pipeline_status_and_counts(run, new_app, log_ok, new_round):
    app_id = new_app()
    new_round("Acme GmbH", "2026-01-06", "screening", "recruiter_screen", "phone")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    new_round("Acme GmbH", "2026-01-08", "interview", "general", "video",
              "--participants", "hiring_manager")
    log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-09", "--round", "2",
           "--scheduled-for", "2026-01-12", "--scheduled-time", "10:00",
           "--timezone", "Europe/Berlin")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-12", "--round", "2")
    new_round("Acme GmbH", "2026-01-13", "assessment", "take_home", "asynchronous")
    log_ok("Acme GmbH", "--event", "assessment_received", "--date", "2026-01-13",
           "--round", "3")
    log_ok("Acme GmbH", "--event", "assessment_submitted", "--date", "2026-01-15",
           "--round", "3")
    new_round("Acme GmbH", "2026-01-16", "interview", "technical", "on_site",
              "--participants", "hiring_manager,team")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-18", "--round", "4")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")

    detail = payload(run, "show", app_id, "--json")
    app = detail["application"]
    assert app["current_status"] == "rejected"
    assert app["completed_interviews"] == 2
    assert app["completed_screens"] == 1
    assert app["completed_assessments"] == 1
    rounds = {row["sequence_no"]: row for row in detail["rounds"]}
    assert rounds[4]["participants"] == ["hiring_manager", "team"]
    assert [event["event_type"] for event in detail["timeline"]] == [
        "submitted", "invited", "completed", "invited", "scheduled", "completed",
        "invited", "assessment_received", "assessment_submitted", "invited", "completed",
        "rejected",
    ]


def test_skipped_invitation_and_screening_still_counts(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-10", "--round", "new",
           "--phase", "interview", "--round-type", "technical", "--format", "video")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert detail["application"]["completed_interviews"] == 1


def test_five_interviews_are_all_tracked(run, new_app, log_ok):
    new_app()
    for index in range(5):
        day = f"2026-02-0{index + 1}"
        log_ok("Acme GmbH", "--event", "completed", "--date", day, "--round", "new",
               "--phase", "interview", "--round-type", "general", "--format", "video")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert detail["application"]["completed_interviews"] == 5
    assert [round_["sequence_no"] for round_ in detail["rounds"]] == [1, 2, 3, 4, 5]


def test_repeated_scheduling_keeps_one_round_and_latest_appointment(run, new_app, log_ok,
                                                                   new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-07", "--round", "1",
           "--scheduled-for", "2026-01-10", "--scheduled-time", "09:00",
           "--timezone", "Europe/Berlin")
    log_ok("Acme GmbH", "--event", "rescheduled", "--date", "2026-01-08", "--round", "1",
           "--scheduled-for", "2026-01-15", "--scheduled-time", "11:00",
           "--timezone", "Europe/Berlin")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert len(detail["rounds"]) == 1
    assert detail["rounds"][0]["scheduled_for"] == "2026-01-15"
    assert detail["rounds"][0]["scheduled_time"] == "11:00"
    assert detail["rounds"][0]["current_status"] == "scheduled"

    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-15", "--round", "1")
    upcoming = rows(run, "upcoming", "--as-of", "2026-01-16", "--json")
    assert upcoming == []


def test_completed_round_rejects_reschedule_and_second_completion(run, new_app, log_ok,
                                                                 new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    again = run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-08",
                "--round", "1")
    assert again.exit_code == 2 and "already has a completion" in again.output
    reschedule = run("log", "Acme GmbH", "--event", "rescheduled", "--date", "2026-01-08",
                     "--round", "1", "--scheduled-for", "2026-01-20")
    assert reschedule.exit_code == 2 and "completed" in reschedule.output


def test_assessment_phase_rules(run, new_app, log_ok):
    new_app()
    wrong = run("log", "Acme GmbH", "--event", "assessment_submitted", "--date",
                "2026-01-07", "--round", "new", "--phase", "interview",
                "--round-type", "technical")
    assert wrong.exit_code == 2 and "not an assessment" in wrong.output
    log_ok("Acme GmbH", "--event", "assessment_received", "--date", "2026-01-07",
           "--round", "new", "--phase", "assessment", "--round-type", "take_home")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert detail["application"]["completed_assessments"] == 0
    log_ok("Acme GmbH", "--event", "assessment_submitted", "--date", "2026-01-09",
           "--round", "1")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert detail["application"]["completed_assessments"] == 1
    assert detail["application"]["completed_interviews"] == 0


def test_same_day_ordering_and_reordering(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "first")
    log_ok("Acme GmbH", "--event", "follow_up_sent", "--date", "2026-01-06")
    detail = payload(run, "show", "Acme GmbH", "--json")
    orders = [event["day_order"] for event in detail["timeline"] if event["event_id"]]
    assert orders == [1, 2]

    event_id = detail["timeline"][-1]["event_id"]
    assert run("edit-event", str(event_id), "--set", "day_order=1").exit_code == 2


def test_event_can_be_inserted_before_another_same_day_event(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "A")
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "B")
    detail = payload(run, "show", "Acme GmbH", "--json")
    second_id = detail["timeline"][-1]["event_id"]
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "C",
           "--before-event", str(second_id))
    notes = [event["notes"] for event in payload(run, "show", "Acme GmbH", "--json")["timeline"]
             if event["event_type"] == "note"]
    assert notes == ["A", "C", "B"]


def test_backdated_event_warns_but_is_recorded(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-20", "--round", "new",
           "--phase", "interview", "--round-type", "general")
    result = run("log", "Acme GmbH", "--event", "note", "--date", "2026-01-10",
                 "--notes", "found the old email")
    assert result.ok
    assert "backdated" in result.output


def test_notes_and_follow_ups_after_rejection_keep_the_status(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-11", "--notes", "asked for feedback")
    log_ok("Acme GmbH", "--event", "follow_up_sent", "--date", "2026-01-12")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "rejected"


def test_closed_application_requires_explicit_reopening(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    blocked = run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-15",
                  "--round", "new", "--phase", "interview", "--round-type", "general")
    assert blocked.exit_code == 2 and "reopened" in blocked.output
    rounds_after = payload(run, "show", "Acme GmbH", "--json")["rounds"]
    assert rounds_after == []  # a blocked flow does not leave an empty round behind
    log_ok("Acme GmbH", "--event", "reopened", "--date", "2026-01-15")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "in_progress"


def test_no_response_marker_is_not_a_rejection(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "marked_inactive", "--date", "2026-01-20")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "inactive"
    waiting = rows(run, "waiting", "--as-of", "2026-02-01", "--json")
    assert [row["id"] for row in waiting] == []


def test_cancelled_round_does_not_close_the_application(run, new_app, log_ok, new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "cancelled", "--date", "2026-01-07", "--round", "1")
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert detail["application"]["current_status"] == "in_progress"
    assert detail["rounds"][0]["current_status"] == "cancelled"


def test_offer_flow_requires_an_offer_first(run, new_app, log_ok):
    new_app()
    blocked = run("log", "Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-15")
    assert blocked.exit_code == 2 and "offer_received" in blocked.output
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "offer_pending"
    log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-15")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "accepted"
    early = run("log", "Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-05")
    assert early.exit_code == 2


def test_job_started_requires_acceptance(run, new_app, log_ok):
    new_app()
    blocked = run("log", "Acme GmbH", "--event", "job_started", "--date", "2026-02-01")
    assert blocked.exit_code == 2 and "accepted" in blocked.output
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-12")
    log_ok("Acme GmbH", "--event", "job_started", "--date", "2026-02-01")
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "started"


def test_wrong_scope_and_cross_application_round(run, new_app, new_round, log_ok):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Other AG", "Data", "2026-01-05")
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    scoped = run("log", "Acme GmbH", "--event", "rejected", "--date", "2026-01-10", "--round", "1")
    assert scoped.exit_code == 2 and "application-level" in scoped.output
    cross = run("log", "Other AG", "--event", "completed", "--date", "2026-01-10", "--round", "1")
    assert cross.exit_code == 2
    assert "different application" in cross.output or "no round" in cross.output


def test_invalid_event_type_and_round_reference(run, new_app):
    new_app()
    assert run("log", "Acme GmbH", "--event", "nonsense", "--date", "2026-01-06").exit_code == 2
    assert run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-06",
               "--round", "7").exit_code == 2


def test_date_validation_and_dst_handling(run, new_app, log_ok, new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    bad = run("log", "Acme GmbH", "--event", "completed", "--date", "2026-02-30", "--round", "1")
    assert bad.exit_code == 2 and "valid calendar date" in bad.output
    future = run("log", "Acme GmbH", "--event", "completed", "--date", "2099-01-01", "--round", "1")
    assert future.exit_code == 2 and "future" in future.output

    date_only = log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-07", "--round", "1",
                       "--scheduled-for", "2026-03-05")
    assert date_only.ok
    bad_tz = run("log", "Acme GmbH", "--event", "rescheduled", "--date", "2026-01-08",
                 "--round", "1", "--scheduled-for", "2026-03-06", "--scheduled-time", "10:00",
                 "--timezone", "Mars/Olympus")
    assert bad_tz.exit_code == 2 and "IANA" in bad_tz.output

    nonexistent = run("log", "Acme GmbH", "--event", "rescheduled", "--date", "2026-01-09",
                      "--round", "1", "--scheduled-for", "2026-03-29", "--scheduled-time",
                      "02:30", "--timezone", "Europe/Berlin")
    assert nonexistent.exit_code == 2 and "does not exist" in nonexistent.output

    ambiguous = run("log", "Acme GmbH", "--event", "rescheduled", "--date", "2026-01-10",
                    "--round", "1", "--scheduled-for", "2026-10-25", "--scheduled-time",
                    "02:30", "--timezone", "Europe/Berlin")
    assert ambiguous.ok
    assert "happens twice" in ambiguous.output


def test_schedule_fields_are_rejected_on_other_events(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06")
    result = run("log", "Acme GmbH", "--event", "note", "--date", "2026-01-07",
                 "--scheduled-for", "2026-01-10")
    assert result.exit_code == 2 and "not a scheduling event" in result.output


def test_correcting_an_event_recalculates_state(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    detail = payload(run, "show", "Acme GmbH", "--json")
    event_id = detail["timeline"][-1]["event_id"]
    assert run("edit-event", str(event_id), "--set", "occurred_on=2026-01-12").ok
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "rejected"
    assert run("delete", "event", str(event_id), "--yes").ok
    assert payload(run, "show", "Acme GmbH", "--json")["application"]["current_status"] == "in_progress"


def test_undo_deletes_the_latest_event_after_review(run, new_app, log_ok):
    new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "typo")
    assert run("undo", "Acme GmbH").exit_code == 2  # no --yes
    assert run("undo", "Acme GmbH", "--yes").ok
    detail = payload(run, "show", "Acme GmbH", "--json")
    assert [event["event_type"] for event in detail["timeline"]] == ["submitted"]


def test_delete_application_keeps_files_unless_asked(run, new_app, project_root, tmp_path, log_ok):
    app_id = new_app()
    source = tmp_path / "posting.txt"
    source.write_text("text")
    assert run("attach", app_id, str(source), "--type", "job_posting").ok
    assert run("delete", "application", app_id, "--yes").ok
    assert (project_root / "applications" / app_id).is_dir()

    app_id_two = new_app("Other AG", "Data", "2026-02-01")
    assert run("attach", app_id_two, str(source), "--type", "job_posting").ok
    assert run("delete", "application", app_id_two, "--yes", "--remove-files").ok
    assert not (project_root / "applications" / app_id_two).exists()


def test_round_delete_preview_counts_dependents(run, new_app, new_round, log_ok):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    detail = payload(run, "show", "Acme GmbH", "--json")
    round_id = detail["rounds"][0]["id"]
    result = run("delete", "round", str(round_id), "--yes")
    assert result.ok
    assert "2 event(s)" in result.output
    assert payload(run, "show", "Acme GmbH", "--json")["rounds"] == []


def test_edit_application_attribute_and_lookup_validation(run, new_app):
    new_app()
    assert run("edit", "Acme GmbH", "--set", "location=Berlin").ok
    updated = payload(run, "show", "Acme GmbH", "--json")["application"]
    assert updated["location"] == "Berlin"
    bad = run("edit", "Acme GmbH", "--set", "discovery_source=carrier_pigeon")
    assert bad.exit_code == 2 and "unknown" in bad.output
    unknown = run("edit", "Acme GmbH", "--set", "salary=100")
    assert unknown.exit_code == 2


def test_participants_and_round_edits(run, new_app, new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    detail = payload(run, "show", "Acme GmbH", "--json")
    round_id = detail["rounds"][0]["id"]
    assert run("edit-round", "Acme GmbH", "--round", str(round_id), "--set",
               "format=on_site", "--participants", "hiring_manager,team").ok
    updated = payload(run, "show", "Acme GmbH", "--json")["rounds"][0]
    assert updated["format"] == "on_site"
    assert updated["participants"] == ["hiring_manager", "team"]


def test_upcoming_lists_past_due_as_needs_update(run, new_app, new_round, log_ok):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-06", "--round", "1",
           "--scheduled-for", "2026-01-10", "--scheduled-time", "09:00",
           "--timezone", "Europe/Berlin")
    upcoming = rows(run, "upcoming", "--as-of", "2026-01-20", "--json")
    assert upcoming[0]["past_due"] == 1
    assert upcoming[0]["round_status"] == "scheduled"


def test_waiting_uses_the_stale_threshold(run, new_app, log_ok):
    new_app("Quiet AG", "Dev", "2026-01-01")
    log_ok("Quiet AG", "--event", "rejected", "--date", "2026-01-02")
    new_app("Waiting AG", "Dev", "2026-01-01")
    assert rows(run, "waiting", "--as-of", "2026-02-15", "--stale-days", "60", "--json") == []
    waiting = rows(run, "waiting", "--as-of", "2026-03-15", "--json")
    assert [row["company"] for row in waiting] == ["Waiting AG"]
    assert waiting[0]["days_since_activity"] == 73


def test_list_filters(run, new_app):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Other AG", "Data", "2026-01-06")
    assert len(rows(run, "list", "--json")) == 2
    assert [row["company"] for row in rows(run, "list", "--search", "data", "--json")] == ["Other AG"]
    assert rows(run, "list", "--status", "rejected", "--json") == []
    assert len(rows(run, "list", "--status", "in_progress", "--json")) == 2
