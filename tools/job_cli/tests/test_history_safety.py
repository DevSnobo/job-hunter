"""History-correction safety: edits, positional inserts, state regressions,
undo of a round-creating event, deletion guards, and symlink refusals.

These are the regression tests for the functional-review findings F2-F6 and F10
that concern tools/job_cli/src/job_cli/{cli,repo,rules,artifacts}.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from helpers import find_id, payload, rows


# --------------------------------------------------------------------------
# F4: edit-event must validate the resulting history
# --------------------------------------------------------------------------
def test_edit_event_to_acceptance_without_offer_is_rejected(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    detail = payload(run, "show", app_id, "--json")
    rejected_id = detail["timeline"][-1]["event_id"]

    blocked = run("edit-event", str(rejected_id), "--set", "event_type=offer_accepted")
    assert blocked.exit_code == 2
    assert "offer" in blocked.output

    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "rejected"
    assert [event["event_type"] for event in after["timeline"]] == ["submitted", "rejected"]


def test_edit_event_to_job_started_without_acceptance_is_rejected(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    detail = payload(run, "show", app_id, "--json")
    offer_id = detail["timeline"][-1]["event_id"]

    blocked = run("edit-event", str(offer_id), "--set", "event_type=job_started")
    assert blocked.exit_code == 2
    assert "accepted" in blocked.output
    assert payload(run, "show", app_id, "--json")["application"]["current_status"] == "offer_pending"


def test_edit_event_rejects_an_unknown_timezone(run, new_app, new_round, log_ok):
    app_id = new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-07", "--round", "1",
           "--scheduled-for", "2026-01-12", "--scheduled-time", "10:00",
           "--timezone", "Europe/Berlin")
    detail = payload(run, "show", app_id, "--json")
    scheduled_id = detail["timeline"][-1]["event_id"]

    blocked = run("edit-event", str(scheduled_id), "--set", "timezone=Mars/Olympus")
    assert blocked.exit_code == 2
    assert "IANA" in blocked.output
    # an appointment time without a timezone is rejected as well
    unpaired = run("edit-event", str(scheduled_id), "--set", "timezone=")
    assert unpaired.exit_code == 2
    assert "together" in unpaired.output


def test_edit_event_rejects_a_completion_before_the_appointment(run, new_app, new_round, log_ok):
    app_id = new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "scheduled", "--date", "2026-01-07", "--round", "1",
           "--scheduled-for", "2026-01-20")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-20", "--round", "1")
    detail = payload(run, "show", app_id, "--json")
    completed_id = detail["timeline"][-1]["event_id"]

    blocked = run("edit-event", str(completed_id), "--set", "occurred_on=2026-01-10")
    assert blocked.exit_code == 2
    assert "scheduled for" in blocked.output
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["completed_interviews"] == 1
    assert after["timeline"][-1]["occurred_on"] == "2026-01-20"


def test_edit_event_rejects_a_completion_that_conflicts_with_a_cancellation(
        run, new_app, new_round, log_ok):
    app_id = new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "cancelled", "--date", "2026-01-07", "--round", "1")
    detail = payload(run, "show", app_id, "--json")
    invited_id = [event for event in detail["timeline"]
                  if event["event_type"] == "invited"][0]["event_id"]

    blocked = run("edit-event", str(invited_id), "--set", "event_type=completed")
    assert blocked.exit_code == 2
    assert "completed" in blocked.output
    after = payload(run, "show", app_id, "--json")
    assert after["rounds"][0]["current_status"] == "cancelled"
    assert after["application"]["completed_interviews"] == 0


def test_edit_event_reuses_the_round_phase_rules(run, new_app, new_round, log_ok):
    app_id = new_app()
    new_round("Acme GmbH", "2026-01-06", "interview", "general", "video")
    detail = payload(run, "show", app_id, "--json")
    invited_id = detail["timeline"][-1]["event_id"]

    blocked = run("edit-event", str(invited_id), "--set", "event_type=assessment_submitted")
    assert blocked.exit_code == 2
    assert "assessment" in blocked.output
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["completed_assessments"] == 0


def test_edit_event_still_applies_a_valid_correction(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-10")
    detail = payload(run, "show", app_id, "--json")
    rejected_id = detail["timeline"][-1]["event_id"]
    assert run("edit-event", str(rejected_id), "--set", "occurred_on=2026-01-12").ok
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "rejected"
    assert after["application"]["status_since"] == "2026-01-12"


# --------------------------------------------------------------------------
# F5: positional and backdated inserts validate the whole timeline
# --------------------------------------------------------------------------
def test_same_day_acceptance_before_offer_is_rejected(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-09-15")
    detail = payload(run, "show", app_id, "--json")
    offer_id = detail["timeline"][-1]["event_id"]

    blocked = run("log", "Acme GmbH", "--event", "offer_accepted", "--date", "2026-09-15",
                  "--before-event", str(offer_id))
    assert blocked.exit_code == 2
    assert "offer" in blocked.output

    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "offer_pending"
    assert [event["event_type"] for event in after["timeline"]] == ["submitted", "offer_received"]


def test_backdated_rejection_between_offer_and_acceptance_is_rejected(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-09-10")
    log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-09-20")

    blocked = run("log", "Acme GmbH", "--event", "rejected", "--date", "2026-09-15")
    assert blocked.exit_code == 2
    assert "reopened" in blocked.output

    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "accepted"
    assert [event["event_type"] for event in after["timeline"]] == [
        "submitted", "offer_received", "offer_accepted"]


def test_backdated_note_is_still_recorded(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    result = run("log", "Acme GmbH", "--event", "note", "--date", "2026-01-10",
                 "--notes", "old email")
    assert result.ok, result.output
    assert "backdated" in result.output
    assert payload(run, "show", app_id, "--json")["application"]["current_status"] == "rejected"


# --------------------------------------------------------------------------
# F6: no silent offer_pending/accepted/started regression
# --------------------------------------------------------------------------
def test_offer_pending_regression_needs_consent(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    blocked = run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-11",
                  "--round", "new", "--phase", "interview", "--round-type", "general",
                  "--format", "video")
    assert blocked.exit_code == 2
    assert "--allow-state-regression" in blocked.output or "reopened" in blocked.output
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "offer_pending"
    assert after["rounds"] == []  # the blocked attempt leaves no empty round behind

    confirmed = run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-11",
                    "--round", "new", "--phase", "interview", "--round-type", "general",
                    "--format", "video", "--allow-state-regression")
    assert confirmed.ok, confirmed.output
    assert payload(run, "show", app_id, "--json")["application"]["current_status"] == "in_progress"


def test_accepted_regression_needs_consent(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-01-12")
    blocked = run("log", "Acme GmbH", "--event", "invited", "--date", "2026-01-15",
                  "--round", "new", "--phase", "interview", "--round-type", "general")
    assert blocked.exit_code == 2
    assert payload(run, "show", app_id, "--json")["application"]["current_status"] == "accepted"


def test_explicit_reopened_event_is_the_documented_exception(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-01-10")
    reopened = run("log", "Acme GmbH", "--event", "reopened", "--date", "2026-01-11")
    assert reopened.ok, reopened.output
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "in_progress"
    assert "reopened" in [event["event_type"] for event in after["timeline"]]
    # once the explicit event exists, an ordinary round event no longer needs the flag
    assert run("log", "Acme GmbH", "--event", "completed", "--date", "2026-01-12",
               "--round", "new", "--phase", "interview", "--round-type", "general",
               "--format", "video").ok


# --------------------------------------------------------------------------
# F10: undo removes a round that was created with the undone event
# --------------------------------------------------------------------------
def test_undo_removes_the_round_created_with_the_event(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-02-01", "--round", "new",
           "--phase", "interview", "--round-type", "general", "--format", "video")
    assert len(payload(run, "show", app_id, "--json")["rounds"]) == 1

    undone = run("undo", "Acme GmbH", "--yes")
    assert undone.ok, undone.output
    after = payload(run, "show", app_id, "--json")
    assert after["rounds"] == []
    assert [event["event_type"] for event in after["timeline"]] == ["submitted"]


def test_undo_keeps_a_round_that_still_has_events(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "invited", "--date", "2026-01-06", "--round", "new",
           "--phase", "interview", "--round-type", "general", "--format", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")

    assert run("undo", "Acme GmbH", "--yes").ok
    after = payload(run, "show", app_id, "--json")
    assert [row["sequence_no"] for row in after["rounds"]] == [1]
    assert [event["event_type"] for event in after["timeline"]] == ["submitted", "invited"]


def test_undo_without_confirmation_is_refused_when_not_interactive(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "typo")
    refused = run("undo", "Acme GmbH")
    assert refused.exit_code == 2
    assert payload(run, "show", app_id, "--json")["timeline"][-1]["event_type"] == "note"


# --------------------------------------------------------------------------
# F2/F3: scripted confirmations and symlink safety
# --------------------------------------------------------------------------
def test_delete_refuses_to_break_later_history(run, new_app, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "offer_received", "--date", "2026-09-10")
    log_ok("Acme GmbH", "--event", "offer_accepted", "--date", "2026-09-20")
    detail = payload(run, "show", app_id, "--json")
    offer_id = [event for event in detail["timeline"]
                if event["event_type"] == "offer_received"][0]["event_id"]

    blocked = run("delete", "event", str(offer_id), "--yes")
    assert blocked.exit_code == 2
    assert "contradictory" in blocked.output
    after = payload(run, "show", app_id, "--json")
    assert after["application"]["current_status"] == "accepted"
    assert [event["event_type"] for event in after["timeline"]] == [
        "submitted", "offer_received", "offer_accepted"]


def test_deletes_and_restore_need_confirmation_without_a_terminal(run, new_app, project_root,
                                                                 tmp_path, log_ok):
    app_id = new_app()
    log_ok("Acme GmbH", "--event", "note", "--date", "2026-01-06", "--notes", "keep me")
    detail = payload(run, "show", app_id, "--json")
    event_id = detail["timeline"][-1]["event_id"]
    round_result = run("log", "Acme GmbH", "--event", "invited", "--date", "2026-01-07",
                       "--round", "new", "--phase", "interview", "--round-type", "general")
    assert round_result.ok
    round_id = payload(run, "show", app_id, "--json")["rounds"][0]["id"]

    assert run("delete", "event", str(event_id)).exit_code == 2
    assert run("delete", "round", str(round_id)).exit_code == 2
    assert run("delete", "application", app_id).exit_code == 2
    assert run("delete", "application", app_id, "--remove-files").exit_code == 2

    backup = tmp_path / "backup.sqlite3"
    assert run("db", "backup", "--label", "safety").ok
    assert run("db", "restore", str(backup)).exit_code == 2
    assert Path(run.db).exists()

    after = payload(run, "show", app_id, "--json")
    assert len(after["timeline"]) >= 2
    assert after["rounds"] != []


def _make_symlink(project_root: Path, app_id: str, target: Path) -> Path:
    applications = project_root / "applications"
    applications.mkdir(exist_ok=True)
    link = applications / app_id
    os.symlink(target, link)
    return link


def test_delete_never_follows_an_internal_application_symlink(run, new_app, tmp_path,
                                                              project_root):
    victim = new_app("Victim AG", "Backend", "2026-01-05")
    link_app = new_app("Link AG", "Backend", "2026-01-05", "--force")
    source = tmp_path / "cv.txt"
    source.write_text("keep me")
    assert run("attach", victim, str(source), "--type", "cv").ok
    victim_file = project_root / "applications" / victim / "cv.txt"

    _make_symlink(project_root, link_app, project_root / "applications" / victim)
    refused = run("delete", "application", link_app, "--yes", "--remove-files")
    assert refused.exit_code == 2
    assert "symlink" in refused.output
    assert victim_file.read_text() == "keep me"
    assert any(row["id"] == link_app for row in rows(run, "list", "--json"))

    os.unlink(project_root / "applications" / link_app)


def test_symlink_to_the_applications_base_is_refused(run, new_app, project_root):
    victim = new_app("Victim AG", "Backend", "2026-01-05")
    link_app = new_app("Link AG", "Backend", "2026-01-05", "--force")
    (project_root / "applications" / victim).mkdir(parents=True, exist_ok=True)
    (project_root / "applications" / victim / "posting.txt").write_text("keep me")

    _make_symlink(project_root, link_app, project_root / "applications")
    refused = run("delete", "application", link_app, "--yes", "--remove-files")
    assert refused.exit_code == 2
    assert (project_root / "applications" / victim / "posting.txt").read_text() == "keep me"
    os.unlink(project_root / "applications" / link_app)


def test_symlink_to_a_nested_directory_is_refused(run, new_app, project_root):
    link_app = new_app("Link AG", "Backend", "2026-01-05")
    nested = project_root / "applications" / "intake" / "nested"
    nested.mkdir(parents=True)
    (nested / "posting.txt").write_text("keep me")

    _make_symlink(project_root, link_app, nested)
    refused = run("delete", "application", link_app, "--yes", "--remove-files")
    assert refused.exit_code == 2
    assert (nested / "posting.txt").read_text() == "keep me"
    os.unlink(project_root / "applications" / link_app)


def test_attach_refuses_an_internal_application_symlink(run, new_app, tmp_path, project_root):
    victim = new_app("Victim AG", "Backend", "2026-01-05")
    link_app = new_app("Link AG", "Backend", "2026-01-05", "--force")
    (project_root / "applications" / victim).mkdir(parents=True, exist_ok=True)
    _make_symlink(project_root, link_app, project_root / "applications" / victim)
    source = tmp_path / "cv.txt"
    source.write_text("x")

    refused = run("attach", link_app, str(source), "--type", "cv")
    assert refused.exit_code == 2
    assert "symlink" in refused.output
    assert list((project_root / "applications" / victim).iterdir()) == []
    os.unlink(project_root / "applications" / link_app)
