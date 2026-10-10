"""Command-line interface for the private job tracker.

Interactive prompts when stdin is a terminal; the same operations are also
available through explicit flags for scripts and tests.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.panel import Panel

from . import (
    __version__,
    artifacts,
    db as dbmod,
    intake,
    migrations,
    repo,
    reporting,
    rules,
    ui,
)
from .errors import Cancelled, DatabaseError, JobError, MigrationError, UsageError
from .paths import Layout, build_layout

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Private tracker for submitted job applications. Run without arguments for the menu.",
)
db_app = typer.Typer(help="Database status, migrations, backups, and health checks.")
export_app = typer.Typer(help="Private snapshots and aggregate exports.")
delete_app = typer.Typer(help="Delete events, rounds, or applications (with a preview and backup).")
app.add_typer(db_app, name="db")
app.add_typer(export_app, name="export")
app.add_typer(delete_app, name="delete")

EVENT_ORDER = [
    "invited", "scheduled", "rescheduled", "completed", "cancelled",
    "assessment_received", "assessment_submitted", "feedback_received", "follow_up_sent",
    "note", "offer_received", "offer_accepted", "offer_declined", "rejected", "withdrawn",
    "position_closed", "marked_inactive", "reopened", "job_started",
]


@dataclass
class Ctx:
    layout: Layout
    conn: sqlite3.Connection
    json_output: bool = False
    yes: bool = False

    @property
    def interactive(self) -> bool:
        return ui.interactive_available()


# --------------------------------------------------------------------------
# startup and migrations
# --------------------------------------------------------------------------
def _apply_migrations(layout: Layout, state: migrations.DbState) -> tuple[list, Path | None, list[str]]:
    conn = dbmod.connect(layout.db_path)
    try:
        backup_path = None
        if not state.is_empty:
            backup_path = migrations.backup(conn, layout.backups_dir)
        applied = migrations.apply_pending(conn, state)
        problems = migrations.verify(conn)
    finally:
        conn.close()
    return applied, backup_path, problems


def prepare_database(layout: Layout, *, prompt: bool = True,
                     admin_command: bool = False) -> list[str]:
    """Bootstrap a fresh database; require approval before changing an existing one.

    Database maintenance commands (`job db ...`) may run with migrations still
    pending so that `db status`, `db migrate --dry-run`, and `db backup` stay usable.
    """
    state = migrations.inspect(layout.db_path, layout.migrations_dir)
    if not state.pending:
        return []
    messages: list[str] = []
    if admin_command and not state.is_empty:
        listing = ", ".join(m.display for m in state.pending)
        ui.note(f"[yellow]{len(state.pending)} pending migration(s): {listing}[/yellow]")
        return []
    if not state.is_empty:
        listing = ", ".join(m.display for m in state.pending)
        message = f"{len(state.pending)} pending migration(s): {listing}"
        if not (prompt and ui.interactive_available()):
            raise MigrationError(
                message + "; run `job db migrate` to apply them (a backup is taken first)"
            )
        ui.note(f"[yellow]{message}[/yellow]")
        if not ui.confirm("Apply them now? A backup is taken first.", default=True):
            raise MigrationError("pending migrations were not applied")
    applied, backup_path, problems = _apply_migrations(layout, state)
    if applied:
        messages.append("applied " + ", ".join(m.display for m in applied))
    if backup_path:
        messages.append(f"backup: {backup_path}")
    if state.is_empty:
        messages.append(f"initialized {layout.db_path}")
    if problems:
        raise MigrationError("post-migration checks failed: " + "; ".join(problems))
    if messages:
        ui.note("[green]" + "; ".join(messages) + "[/green]")
    return messages


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    db: str = typer.Option(None, "--db", help="Path to the SQLite database (default data/job.sqlite3)."),
    root: str = typer.Option(None, "--root", help="Job project root (default: JOB_ROOT or detected)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output where supported."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes for confirmations."),
    no_migrate: bool = typer.Option(False, "--no-migrate", help="Do not apply pending migrations on startup."),
    no_link: bool = typer.Option(False, "--no-link",
                                 help="Do not link applications to their applications/ folders on startup."),
) -> None:
    layout = build_layout(db, root)
    if not no_migrate:
        prepare_database(layout, admin_command=ctx.invoked_subcommand == "db")
    if not layout.db_path.exists():
        raise DatabaseError(f"database not found: {layout.db_path} (use --db or `job db migrate`)")
    conn = dbmod.connect(layout.db_path, must_exist=True)
    ctx.call_on_close(conn.close)
    ctx.obj = Ctx(layout, conn, json_output, yes)
    if not no_link and ctx.invoked_subcommand != "db":
        _reconcile_intake_links(ctx.obj)
    if ctx.invoked_subcommand is None:
        if ui.interactive_available():
            _menu(ctx.obj)
        else:
            typer.echo(ctx.get_help())
            raise typer.Exit(code=2)


def main() -> None:
    try:
        app(prog_name="job")
    except Cancelled as exc:
        ui.console.print(f"[yellow]Cancelled: {exc}[/yellow]")
        raise SystemExit(130) from None
    except JobError as exc:
        ui.err_console.print(f"[red]error:[/red] {exc}")
        raise SystemExit(2) from None


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _ctx(ctx: typer.Context) -> Ctx:
    if ctx.obj is None:  # pragma: no cover - defensive
        raise UsageError("internal error: no context")
    return ctx.obj


def _dump(data) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _lookup_choices(c: Ctx, key: str, *, unknown_default: bool = True) -> list[tuple[str, str]]:
    return ui.labelled_lookups(repo.lookup_values(c.conn, key))


def _event_choices(c: Ctx) -> list[tuple[str, str]]:
    rows = {str(row["code"]): row for row in repo.event_types(c.conn)}
    choices = [(code, f"{rows[code]['label']}  [{code}]") for code in EVENT_ORDER if code in rows]
    for code, row in rows.items():
        if code not in EVENT_ORDER:
            choices.append((code, f"{row['label']}  [{code}]"))
    return choices


def _prompt_application(c: Ctx, message: str = "Which application?") -> sqlite3.Row:
    query = ui.text(message + " (search company or role; blank lists recent)")
    rows = reporting.overview(c.conn, c.layout, search=query or None)
    if not rows:
        raise UsageError("no applications match that search")
    options = [
        (
            str(row["id"]),
            f"{repo.short_id(str(row['id']))}  {row['company']} - {row['role']}  "
            f"| {row['current_status']} | submitted {row['submitted_on']} "
            f"| {row['completed_interviews']} interviews",
        )
        for row in rows[:40]
    ]
    return repo.get_application(c.conn, ui.choice("Select an application", options))


def _resolve(c: Ctx, ident: str | None, message: str = "Which application?") -> sqlite3.Row:
    if ident:
        return repo.resolve_application(c.conn, ident)
    ui.require_interactive()
    return _prompt_application(c, message)


def _print_issues(issues: list[rules.Issue]) -> None:
    for issue in issues:
        colour = {"error": "red", "warn": "yellow", "info": "cyan"}[issue.severity]
        ui.console.print(f"[{colour}]{issue.severity}: {issue.message}[/{colour}]")


def _show_status(c: Ctx, app_id: str) -> None:
    row = repo.get_application(c.conn, app_id)
    ui.console.print(
        f"[bold]{row['company']} - {row['role']}[/bold] -> {ui.status_text(str(row['current_status']))}"
        f" (submitted {row['submitted_on']})"
    )
    ui.render_timeline(repo.timeline(c.conn, app_id)[-6:], title="Recent timeline")


def _backup_before_change(c: Ctx, why: str) -> Path | None:
    if c.yes or True:
        path = migrations.backup(c.conn, c.layout.backups_dir, label="job")
        ui.console.print(f"[dim]backup before {why}: {path}[/dim]")
        return path


def _confirm(c: Ctx, message: str, *, default: bool = False) -> bool:
    if c.yes:
        return True
    if not c.interactive:
        raise UsageError(message + " (non-interactive run: pass --yes to confirm)")
    return ui.confirm(message, default=default)


class HistoryConflict(JobError):
    """A write would leave the application's history contradictory.

    Raised inside the write transaction so the change is rolled back and the
    recorded history stays exactly as it was.
    """

    def __init__(self, issues: list[rules.Issue]):
        self.issues = list(issues)
        super().__init__("; ".join(issue.message for issue in self.issues))


def _write_validated(c: Ctx, app_id: str, write, *, allow_regression: bool = False):
    """Run one write, then revalidate the whole history in the same transaction.

    Returns (write result, remaining warnings). Contradictions and unconsented
    state regressions roll the transaction back.
    """
    try:
        with dbmod.transaction(c.conn):
            baseline = rules.timeline_issues(c.conn, app_id)
            result = write()
            fresh = rules.new_timeline_issues(c.conn, app_id, baseline)
            blocking = [issue for issue in fresh if issue.is_error]
            regressions = [issue for issue in fresh if issue.code == "state_regression"]
            if blocking or (regressions and not allow_regression):
                raise HistoryConflict(blocking + regressions)
    except sqlite3.IntegrityError as exc:
        raise UsageError(f"the database rejected that change: {exc}") from exc
    warnings = [issue for issue in fresh
                if not issue.is_error and issue.code != "state_regression"]
    return result, warnings


def _conflict_message(conflict: HistoryConflict) -> str:
    text = "; ".join(issue.message for issue in conflict.issues)
    if any(issue.code == "state_regression" for issue in conflict.issues):
        text += ("; pass --allow-state-regression to confirm, or record an explicit 'reopened'"
                 " event first")
    return text


def _validated_delete(c: Ctx, app_id: str, write) -> None:
    """Delete rows only when the remaining history still makes sense."""
    try:
        _write_validated(c, app_id, write)
    except HistoryConflict as conflict:
        raise UsageError(
            "refusing to delete: the remaining history would be contradictory ("
            + "; ".join(issue.message for issue in conflict.issues)
            + "). Correct or delete the dependent events first."
        ) from None


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------
def _link_rows(c: Ctx) -> list[dict]:
    """The application rows the folder linker needs, as plain mappings."""
    return [dict(row) for row in repo.applications_for_linking(c.conn)]


def _reconcile_intake_links(c: Ctx) -> None:
    """Link applications to their applications/ folder when that is unambiguous.

    Runs on startup for every command except `job db ...`: an application that
    was entered before its folder was recognized gets its link as soon as the
    folder is there. Only a certain match is linked (see intake.plan_links);
    anything ambiguous is left alone and shown by `job intake`.
    """
    found = intake.scan(c.layout.applications_dir, exclude=repo.application_ids(c.conn))
    if not found.folders:
        return
    proposals, _ambiguous = intake.plan_links(found.folders, _link_rows(c))
    if not proposals:
        return
    for proposal in proposals:
        repo.update_application(c.conn, proposal.application_id, intake_folder=proposal.folder)
    for proposal in proposals:
        row = repo.get_application(c.conn, proposal.application_id)
        ui.note(f"linked {repo.short_id(proposal.application_id)} {row['company']} - {row['role']}"
                f" to applications/{proposal.folder} ({proposal.reason})")


def _same_posting(existing: sqlite3.Row, *, role: str, job_url: str | None) -> bool:
    """True when an existing application looks like the very same posting.

    Two different job URLs are two different postings, even at the same
    employer, and several postings per employer are normal. Only an identical
    URL (or, when one side has no URL at all, an identical role title) is worth
    questioning: that is the shape of a double entry rather than a second
    application.
    """
    existing_url = intake.normalize_url(existing["job_url"])
    if existing_url and job_url:
        return existing_url == intake.normalize_url(job_url)
    return str(existing["role"]).strip().casefold() == role.strip().casefold()


def _application_listing(rows: Sequence[sqlite3.Row], limit: int = 4) -> str:
    shown = "; ".join(f"{repo.short_id(str(row['id']))} {row['role']} ({row['submitted_on']})"
                      for row in rows[:limit])
    return shown + (f"; and {len(rows) - limit} more" if len(rows) > limit else "")


def _intake_label(folder: intake.IntakeFolder) -> str:
    source = intake.source_host(folder.source_url) if folder.source_url else "no source.txt"
    return f"{folder.name}  ->  {folder.company}  |  {source}  |  {len(folder.files)} file(s)"


def _report_intake(c: Ctx, folder: intake.IntakeFolder) -> None:
    """Say what came from the folder. Goes to stderr, so --json stdout stays clean."""
    ui.note(f"intake folder [bold]applications/{folder.name}[/bold] -> company"
            f" {folder.company!r}"
            + (f", posting id {folder.posting_id}" if folder.posting_id else ", no posting id"))
    if folder.source_url:
        ui.note(f"job url from source.txt: {folder.source_url}")
    else:
        ui.note(f"[yellow]no job url[/yellow] ({folder.source_problem})")
    if folder.files:
        ui.note("[dim]files: " + ", ".join(folder.files) + "[/dim]")
    linked = repo.linked_intake_folders(c.conn).get(folder.name)
    if linked:
        ui.note(f"[yellow]note:[/yellow] this folder already produced application"
                f" {repo.short_id(linked)}; a reapplication is a new application")


def _prompt_intake_folder(c: Ctx) -> intake.IntakeFolder | None:
    """Offer the applications/ folders that no application was registered from yet."""
    result = intake.scan(c.layout.applications_dir, exclude=repo.application_ids(c.conn))
    pending = intake.pending(result.folders, repo.linked_intake_folders(c.conn))
    if not pending:
        return None
    if not ui.confirm(f"Start from an intake folder under applications/? ({len(pending)} folder(s)"
                      " with no application yet)", default=True):
        return None
    newest = intake.newest(pending)
    options = [(folder.name, _intake_label(folder)) for folder in pending]
    options.append(("", "Do not use a folder"))
    # The folder that was most likely just prepared is the recommendation, so it
    # moves to the front: the cursor then starts on the first entry of the list.
    options = ui.first_default(options, newest.name if newest else None)
    picked = ui.choice("Which folder?", options, default=options[0][0])
    if not picked:
        return None
    return intake.find(c.layout.applications_dir, picked)


def _intake_source(c: Ctx, *, from_folder: str | None,
                   company: str | None) -> intake.IntakeFolder | None:
    """The intake folder this application is registered from, if any.

    An explicit --from-folder always wins, also in scripts. Otherwise an
    interactive run that did not preset --company offers the pending folders.
    """
    if from_folder:
        folder = intake.find(c.layout.applications_dir, from_folder)
        if repo.application_id_exists(c.conn, folder.name):
            raise UsageError(
                f"applications/{folder.name} belongs to an existing application (it is that"
                " application's own file folder); pass a folder that the posting capture"
                " tool created"
            )
        return folder
    if not c.interactive or company:
        return None
    return _prompt_intake_folder(c)


def _add_application(c: Ctx, *, company: str | None, role: str | None, submitted_on: str | None,
                     discovery_source: str | None = None, submission_channel: str | None = None,
                     recruiter_type: str | None = None, platform_name: str | None = None,
                     source_detail: str | None = None, job_url: str | None = None,
                     location: str | None = None, notes: str | None = None,
                     from_folder: str | None = None, force: bool = False) -> str:
    interactive = c.interactive
    company_given = bool(company)
    folder = _intake_source(c, from_folder=from_folder, company=company)
    if folder is not None:
        if job_url is None:
            job_url = folder.source_url
        if not company:
            company = folder.company
        _report_intake(c, folder)
    if not company:
        ui.require_interactive()
        company = ui.text("Company")
    elif folder is not None and not company_given and interactive:
        # The identifier supplied the name: Enter keeps it, typing replaces it.
        company = ui.text("Company (from the folder identifier)", default=company)
    if not role:
        ui.require_interactive()
        role = ui.text("Role")
    if not submitted_on:
        ui.require_interactive()
        submitted_on = ui.date("Submission date (YYYY-MM-DD)", default=rules.today())
    submitted_on = rules.validate_date(submitted_on, field="submission date")
    company = company.strip()
    role = role.strip()
    if not company or not role:
        raise UsageError("company and role are required")

    if interactive and discovery_source is None:
        if ui.confirm("Add details now (source, channel, recruiter, url, location)?", default=False):
            # "unknown" is the honest answer, so it stays the recommended one and
            # is listed first; the cursor starts there instead of on a later line.
            discovery_source = ui.choice(
                "Discovery source",
                ui.first_default(_lookup_choices(c, "discovery_source"), "unknown"),
                default="unknown")
            submission_channel = ui.choice(
                "Submission channel",
                ui.first_default(_lookup_choices(c, "submission_channel"), "unknown"),
                default="unknown")
            recruiter_type = ui.choice(
                "Recruiter involvement",
                ui.first_default(_lookup_choices(c, "recruiter_type"), "unknown"),
                default="unknown")
            platform_name = ui.optional_text("Platform name (Workday, GreenHouse, ...)")
            # A job url taken from the folder's source.txt is the default, so Enter keeps it.
            job_url = ui.optional_text("Job url", default=job_url)
            location = ui.optional_text("Location")
            notes = ui.optional_text("Notes")
        else:
            discovery_source, submission_channel, recruiter_type = "unknown", "unknown", "unknown"

    existing = repo.similar_applications(c.conn, company)
    if existing:
        likely_duplicates = [row for row in existing
                             if _same_posting(row, role=role, job_url=job_url)]
        if likely_duplicates:
            message = (f"{company} already has an application for this posting:"
                       f" {_application_listing(likely_duplicates)}")
            if force:
                ui.note(f"[yellow]note:[/yellow] {message}")
            elif c.interactive and not c.yes:
                ui.note(f"[yellow]note:[/yellow] {message}")
                if not ui.confirm("Record a second application for it anyway?", default=True):
                    raise Cancelled("duplicate application declined")
            else:
                raise UsageError(message + " (pass --force to record it anyway)")
        else:
            # Several postings per employer are normal: this is a new application,
            # not a reapplication, so the other entries are only worth mentioning.
            ui.note(f"[dim]{company} also has {len(existing)} application(s):"
                    f" {_application_listing(existing)} - a different posting is a separate"
                    " application here[/dim]")

    if c.interactive and not c.yes:
        _review_new_application(
            c, company=company, role=role, submitted_on=submitted_on,
            discovery_source=discovery_source or "unknown",
            submission_channel=submission_channel or "unknown",
            recruiter_type=recruiter_type or "unknown", platform_name=platform_name,
            job_url=job_url, location=location, notes=notes,
            intake_folder=folder.name if folder else None,
        )
    app_id = repo.add_application(
        c.conn,
        company=company,
        role=role,
        submitted_on=submitted_on,
        discovery_source=discovery_source or "unknown",
        submission_channel=submission_channel or "unknown",
        recruiter_type=recruiter_type or "unknown",
        platform_name=platform_name,
        source_detail=source_detail,
        job_url=job_url,
        location=location,
        notes=notes,
        intake_folder=folder.name if folder else None,
    )
    return app_id


def _review_new_application(c: Ctx, *, company: str, role: str, submitted_on: str,
                            discovery_source: str, submission_channel: str, recruiter_type: str,
                            platform_name: str | None, job_url: str | None,
                            location: str | None, notes: str | None,
                            intake_folder: str | None = None) -> None:
    """Short review screen before the guided add writes anything."""
    details = [
        ("company", company),
        ("role", role),
        ("submitted on", submitted_on),
        ("intake folder", intake_folder or "-"),
        ("discovery source", discovery_source),
        ("submission channel", submission_channel),
        ("recruiter", recruiter_type),
        ("platform", platform_name or "-"),
        ("url", job_url or "-"),
        ("location", location or "-"),
        ("notes", notes or "-"),
    ]
    ui.render_table("Review the new application",
                    ("field", "value"), details, empty="nothing entered")
    if not ui.confirm("Save this application?", default=True):
        raise Cancelled("application not saved")


@app.command()
def add(
    ctx: typer.Context,
    company: str = typer.Option(None, "--company", help="Company name (required)."),
    role: str = typer.Option(None, "--role", help="Role title (required)."),
    submitted_on: str = typer.Option(None, "--submitted-on", help="Submission date YYYY-MM-DD."),
    discovery_source: str = typer.Option(None, "--discovery-source"),
    submission_channel: str = typer.Option(None, "--submission-channel"),
    recruiter_type: str = typer.Option(None, "--recruiter-type"),
    platform_name: str = typer.Option(None, "--platform"),
    source_detail: str = typer.Option(None, "--source-detail"),
    job_url: str = typer.Option(None, "--url"),
    location: str = typer.Option(None, "--location"),
    notes: str = typer.Option(None, "--notes"),
    from_folder: str = typer.Option(
        None, "--from-folder",
        help="Intake folder under applications/ (<company>[_<posting-id>]): the company name"
             " comes from the folder identifier, the job url from its source.txt."),
    force: bool = typer.Option(False, "--force", help="Create even when a similar application exists."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Register a submitted application (the only thing that creates a new application).

    With --from-folder (or by answering the folder question on a terminal) the
    application starts from a folder that the posting capture tool wrote under
    applications/, so the company name and the job url do not have to be typed
    again. Every folder stays on disk unchanged; see also `job intake`.
    """
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    app_id = _add_application(
        c, company=company, role=role, submitted_on=submitted_on,
        discovery_source=discovery_source, submission_channel=submission_channel,
        recruiter_type=recruiter_type, platform_name=platform_name, source_detail=source_detail,
        job_url=job_url, location=location, notes=notes, from_folder=from_folder, force=force,
    )
    _post_add(c, app_id)


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------
DEFAULT_PHASE_BY_TYPE = {
    "recruiter_screen": "screening",
    "take_home": "assessment",
    "online_test": "assessment",
}


def _create_round(c: Ctx, app_id: str, event_code: str | None,
                  round_type: str | None = None, phase: str | None = None,
                  fmt: str | None = None, participants: list[str] | None = None) -> int:
    """Create a round, prompting only for what the caller did not supply."""
    if not round_type:
        ui.require_interactive("creating a round without --round-type")
        round_type = ui.choice("Round type",
                               ui.first_default(_lookup_choices(c, "round_type"), "general"),
                               default="general")
    default_phase = DEFAULT_PHASE_BY_TYPE.get(round_type, "interview")
    if event_code in ("assessment_received", "assessment_submitted"):
        default_phase = "assessment"
    if not phase:
        if ui.interactive_available():
            # The phase implied by the round type (or the event) is the
            # recommendation, so it is listed first and the cursor starts there.
            phase = ui.choice(
                "Phase",
                ui.first_default(
                    [("screening", "Screening"), ("interview", "Interview"),
                     ("assessment", "Assessment")],
                    default_phase),
                default=default_phase,
            )
        else:
            phase = default_phase
    if not fmt:
        if ui.interactive_available():
            fmt = ui.choice("Format",
                            ui.first_default(_lookup_choices(c, "format"), "unknown"),
                            default="unknown")
        else:
            fmt = "unknown"
    if participants is None and phase == "interview" and ui.interactive_available():
        participants = ui.checkbox("Participants (space toggles, enter confirms)",
                                   _lookup_choices(c, "participant_role"))
        if participants and len(participants) > 1:
            ui.console.print(
                "[dim]One round covers one independently tracked step; several participants in"
                " one meeting stay one round.[/dim]"
            )
    return repo.create_round(c.conn, app_id, phase=phase, round_type=round_type, fmt=fmt,
                             participants=participants or [])


def _new_round(c: Ctx, app_id: str, event_code: str | None, round_type: str | None = None,
               phase: str | None = None, fmt: str | None = None,
               participants: list[str] | None = None, created: list[int] | None = None) -> int:
    round_id = _create_round(c, app_id, event_code, round_type, phase, fmt, participants)
    if created is not None:
        created.append(round_id)
    return round_id


def _choose_round(c: Ctx, app: sqlite3.Row, event_code: str, round_flag: str | None,
                  round_type: str | None = None, phase: str | None = None, fmt: str | None = None,
                  participants: list[str] | None = None,
                  created: list[int] | None = None) -> int | None:
    """Resolve the round for an event; new round ids are recorded in `created`
    so a failed event write can remove a round that was just invented."""
    app_id = str(app["id"])
    scope = str(repo.event_type_row(c.conn, event_code)["scope"])
    if scope == "application":
        if round_flag:
            raise UsageError(f"'{event_code}' is an application-level event and takes no round")
        return None
    rounds = repo.rounds_for(c.conn, app_id)
    if round_flag:
        if round_flag.lower() in ("new", "create"):
            return _new_round(c, app_id, event_code, round_type, phase, fmt, participants, created)
        if round_flag.lower() in ("none", "-"):
            if scope == "round":
                raise UsageError(f"'{event_code}' needs a round")
            return None
        return int(repo.find_round(c.conn, app_id, round_flag)["id"])
    if scope == "either":
        if not c.interactive:
            return None
        options = [("-", "No round (application-level)")]
        options += [

            (str(row["id"]),
             f"#{row['sequence_no']} {row['phase']} {row['round_type']} ({row['current_status']})")
            for row in rounds
        ]
        options.append(("new", "New round"))
        pick = ui.choice("Attach to which round?", options, default="-")
        if pick == "new":
            return _new_round(c, app_id, event_code, round_type, phase, fmt, participants, created)
        return None if pick == "-" else int(pick)
    if not c.interactive:
        raise UsageError(
            f"'{event_code}' is a round event: pass --round <id|sequence|new>"
            " (with --phase/--round-type for a new round)"
        )
    open_rounds = [row for row in rounds if row["current_status"] in ("planned", "invited", "scheduled")]
    ordered = open_rounds + [row for row in rounds if row not in open_rounds]
    options = [
        (str(row["id"]),
         f"#{row['sequence_no']} {row['phase']} {row['round_type']} ({row['current_status']})"
         + (f" appointment {row['scheduled_for']}" if row["scheduled_for"] else ""))
        for row in ordered
    ]
    options.append(("new", "New round"))
    # No default: the list already puts the rounds that still need attention
    # first, and pre-pointing at one of them would start the cursor there.
    pick = ui.choice("Which round?", options)
    if pick == "new":
        return _new_round(c, app_id, event_code, round_type, phase, fmt, participants, created)
    return int(pick)


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------
def _event_extra_fields(c: Ctx, event: str, on_date: str, scheduled_for: str | None,
                        scheduled_time: str | None, timezone: str | None):
    if event in rules.SCHEDULE_EVENTS:
        if not scheduled_for:
            ui.require_interactive()
            scheduled_for = ui.date("Appointment date (YYYY-MM-DD, may be in the future)",
                                    default=on_date)
        scheduled_for = rules.validate_date(scheduled_for, field="appointment date",
                                            allow_future=True)
        if scheduled_time is None and ui.interactive_available():
            answer = ui.optional_text("Appointment time HH:MM (blank for a date-only appointment)")
            scheduled_time = answer
        if scheduled_time:
            scheduled_time = rules.validate_time(scheduled_time)
            if timezone is None:
                timezone = rules.local_timezone_name()
            if timezone is None:
                ui.require_interactive()
                timezone = ui.text("Timezone (IANA name, e.g. Europe/Berlin)")
            for issue in rules.check_timezone(timezone):
                raise UsageError(issue.message)
            for issue in rules.check_local_time(timezone, scheduled_for, scheduled_time):
                _print_issues([issue])
                if issue.is_error:
                    raise UsageError(issue.message)
        else:
            scheduled_time = None
            timezone = None
    else:
        if scheduled_for or scheduled_time or timezone:
            raise UsageError(f"'{event}' is not a scheduling event; it takes no appointment fields")
    return scheduled_for, scheduled_time, timezone


def _regression_consent(c: Ctx, issues: list[rules.Issue], consent: bool) -> bool:
    """Silent state regression is not allowed: it needs a flag or a clear answer."""
    regressions = [issue for issue in issues if issue.code == "state_regression"]
    if not regressions or consent:
        return consent
    if c.interactive:
        if ui.confirm("Continue and move the application back to 'in_progress'?", default=False):
            return True
        raise Cancelled("state regression declined")
    raise UsageError(
        regressions[0].message
        + "; pass --allow-state-regression to confirm, or record an explicit 'reopened' event"
        " first"
    )


def _discard_rounds(c: Ctx, created: list[int]) -> None:
    """Compensate a failed flow: drop rounds that were created but stayed empty."""
    for round_id in created:
        referenced = dbmod.scalar(c.conn, "SELECT count(*) FROM events WHERE round_id = ?",
                                  (round_id,))
        if referenced:
            continue
        with contextlib.suppress(JobError):
            repo.delete_round(c.conn, round_id)


def _remediate(c: Ctx, app_id: str, event: str, on_date: str, round_id: int | None,
               issues: list[rules.Issue], created: list[int] | None = None) -> int | None:
    """Interactive fix-up for blocking rule errors. Returns a (new) round id."""
    codes = {issue.code for issue in issues if issue.is_error}
    ui.require_interactive()
    if "needs_reopen" in codes:
        if ui.confirm("Reopen the process first (adds a 'reopened' event on that date)?",
                      default=True):
            repo.add_event(c.conn, app_id, "reopened", on_date)
            ui.console.print("[green]added[/green] reopened event")
            return round_id
        raise Cancelled("reopening declined")
    if "offer_missing" in codes:
        if ui.confirm("Record the offer first (backdating is allowed)?", default=True):
            offer_date = ui.date("Offer received on", default=on_date)
            repo.add_event(c.conn, app_id, "offer_received",
                           rules.validate_date(offer_date, field="offer date"))
            ui.console.print("[green]added[/green] offer_received event")
            return round_id
        raise Cancelled("offer backfill declined")
    if "start_requires_acceptance" in codes:
        if ui.confirm("Backfill offer_received and offer_accepted first?", default=True):
            offer_date = rules.validate_date(ui.date("Offer received on", default=on_date),
                                             field="offer date")
            accept_date = rules.validate_date(ui.date("Offer accepted on", default=on_date),
                                              field="acceptance date")
            if accept_date < offer_date:
                raise UsageError("the acceptance date is before the offer date")
            repo.add_event(c.conn, app_id, "offer_received", offer_date)
            repo.add_event(c.conn, app_id, "offer_accepted", accept_date)
            ui.console.print("[green]added[/green] offer_received and offer_accepted events")
            return round_id
        raise Cancelled("acceptance backfill declined")
    if codes & {"already_completed", "round_closed", "round_cancelled", "round_after_completion"}:
        rounds = repo.rounds_for(c.conn, app_id)
        usable = [row for row in rounds if row["current_status"] != "completed"]
        options = [
            (str(row["id"]),
             f"#{row['sequence_no']} {row['phase']} {row['round_type']} ({row['current_status']})")
            for row in usable
        ]
        options.append(("new", "New round"))
        if ui.confirm("Choose another round (or create a new one)?", default=True):
            # No default: "New round" sits last, and the cursor starts on the
            # first usable round of a fresh list.
            pick = ui.choice("Which round?", options)
            if pick == "new":
                return _new_round(c, app_id, event, created=created)
            return int(pick)
        raise Cancelled("round choice declined")
    raise UsageError("; ".join(issue.message for issue in issues if issue.is_error))


def _log_flow(c: Ctx, app: sqlite3.Row, *, event: str | None = None, on_date: str | None = None,
              round_flag: str | None = None, round_type: str | None = None,
              phase: str | None = None, fmt: str | None = None,
              participants: list[str] | None = None, scheduled_for: str | None = None,
              scheduled_time: str | None = None, timezone: str | None = None,
              reason: str | None = None, notes: str | None = None,
              before_event: int | None = None, after_event: int | None = None,
              strict: bool = False, allow_state_regression: bool = False) -> int:
    app_id = str(app["id"])
    if not event:
        ui.require_interactive()
        event = ui.choice("What happened?", _event_choices(c))
    meta = repo.event_type_row(c.conn, event)
    if not on_date:
        ui.require_interactive()
        on_date = ui.date(f"When did this happen? ({meta['label']})", default=rules.today())
    on_date = rules.validate_date(on_date, field="event date")

    scheduled_for, scheduled_time, timezone = _event_extra_fields(
        c, event, on_date, scheduled_for, scheduled_time, timezone
    )
    created: list[int] = []
    consent = allow_state_regression
    try:
        round_id = _choose_round(c, app, event, round_flag, round_type, phase, fmt, participants,
                                 created)

        def _add():
            return repo.add_event(
                c.conn, app_id, event, on_date, round_id=round_id, scheduled_for=scheduled_for,
                scheduled_time=scheduled_time, timezone=timezone, reason_code=reason, notes=notes,
                before_event_id=before_event, after_event_id=after_event,
            )

        fresh_warnings: list[rules.Issue] = []
        for _attempt in range(6):
            issues = rules.plan_event(c.conn, app_id, event, on_date, round_id=round_id,
                                      scheduled_for=scheduled_for, scheduled_time=scheduled_time,
                                      timezone=timezone)
            errors = [issue for issue in issues if issue.is_error]
            if errors:
                if not c.interactive:
                    raise UsageError("; ".join(issue.message for issue in errors))
                _print_issues(errors)
                round_id = _remediate(c, app_id, event, on_date, round_id, errors, created)
                continue
            warnings = [issue for issue in issues if issue.severity != "error"]
            regressions = [issue for issue in warnings if issue.code == "state_regression"]
            if warnings:
                _print_issues(warnings)
                if strict:
                    raise UsageError("warnings are errors in --strict mode")
            consent = _regression_consent(c, issues, consent)
            if (warnings and c.interactive and not regressions
                    and not ui.confirm("Save the event anyway?", default=True)):
                raise Cancelled("event not saved")
            try:
                event_id, fresh_warnings = _write_validated(c, app_id, _add,
                                                           allow_regression=consent)
            except HistoryConflict as conflict:
                if not c.interactive:
                    raise UsageError(_conflict_message(conflict)) from None
                _print_issues(conflict.issues)
                if all(issue.code == "state_regression" for issue in conflict.issues):
                    if ui.confirm("Save it and move the application back to 'in_progress'?",
                                  default=False):
                        consent = True
                        continue
                    raise Cancelled("state regression declined")
                round_id = _remediate(c, app_id, event, on_date, round_id, conflict.issues, created)
                continue
            break
        else:
            raise UsageError("the workflow rules could not be satisfied for this event")
    except BaseException:
        _discard_rounds(c, created)
        raise
    if fresh_warnings:
        _print_issues(fresh_warnings)
    if c.json_output:
        _dump(dict(repo.get_event(c.conn, event_id)))
        return event_id
    ui.console.print(f"[green]logged[/green] {meta['label']} on {on_date}"
                     + (f" (round #{round_id})" if round_id else ""))
    _show_status(c, app_id)
    return event_id


@app.command()
def log(
    ctx: typer.Context,
    ident: str = typer.Argument(None, help="Application id, id prefix, company, or role."),
    event: str = typer.Option(None, "--event", help="Event type code, e.g. completed."),
    on_date: str = typer.Option(None, "--date", help="When it happened (YYYY-MM-DD, today/yesterday)."),
    round_flag: str = typer.Option(None, "--round", help="Round id, sequence number, 'new', or 'none'."),
    round_type: str = typer.Option(None, "--round-type", help="Round type for a new round."),
    phase: str = typer.Option(None, "--phase", help="screening, interview, or assessment."),
    fmt: str = typer.Option(None, "--format", help="Interview format code for a new round."),
    participants: str = typer.Option(None, "--participants", help="Comma-separated participant roles."),
    scheduled_for: str = typer.Option(None, "--scheduled-for", help="Appointment date."),
    scheduled_time: str = typer.Option(None, "--scheduled-time", help="Appointment local time HH:MM."),
    timezone: str = typer.Option(None, "--timezone", help="IANA timezone for the appointment time."),
    reason: str = typer.Option(None, "--reason", help="Optional short reason code."),
    notes: str = typer.Option(None, "--notes", help="Optional notes."),
    before_event: int = typer.Option(None, "--before-event", help="Insert before this event on the same day."),
    after_event: int = typer.Option(None, "--after-event", help="Insert after this event on the same day."),
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors."),
    allow_state_regression: bool = typer.Option(
        False, "--allow-state-regression",
        help="Confirm moving an offered, accepted, or started application back to in_progress."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Log what happened next (the most common action)."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    app = _resolve(c, ident, "Which application do you want to update?")
    parts = [p.strip() for p in (participants or "").split(",") if p.strip()]
    _log_flow(c, app, event=event, on_date=on_date, round_flag=round_flag, round_type=round_type,
              phase=phase, fmt=fmt, participants=parts or None, scheduled_for=scheduled_for,
              scheduled_time=scheduled_time, timezone=timezone, reason=reason, notes=notes,
              before_event=before_event, after_event=after_event, strict=strict,
              allow_state_regression=allow_state_regression)


# --------------------------------------------------------------------------
# list / show
# --------------------------------------------------------------------------
def _render_overview(c: Ctx, rows, *, title: str = "Applications") -> None:
    """Print the application table with its counters below it.

    The counters describe the whole tracker (reporting.tracker_summary), so a
    filtered table still answers "how many do I have, how many are in progress,
    how many were rejected"; the row count of the table is appended when it
    differs. The same summary decides which in_progress rows already have an
    employer response, which is what the status colours show.
    """
    ui.render_applications(rows, title=title,
                           summary=reporting.tracker_summary(c.conn, c.layout))


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    status: str = typer.Option(None, "--status", help="Filter by current status."),
    search: str = typer.Option(None, "--search", help="Substring of company or role."),
    open_only: bool = typer.Option(False, "--open", help="Only in_progress and offer_pending."),
    limit: int = typer.Option(None, "--limit", help="Show at most this many rows."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Browse and search applications."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    rows = reporting.overview(c.conn, c.layout, search=search, status=status)
    if open_only:
        rows = [row for row in rows if row["current_status"] in repo.ACTIVE_STATUSES]
    if limit:
        rows = rows[:limit]
    if c.json_output:
        _dump([dict(row) for row in rows])
        return
    if not rows:
        ui.console.print("[yellow]no applications match[/yellow]")
        return
    _render_overview(c, rows)


def _show_application(c: Ctx, app_id: str) -> None:
    row = repo.get_application(c.conn, app_id)
    posting_sources = _posting_sources(c, row)
    if c.json_output:
        rounds = repo.rounds_for(c.conn, app_id)
        payload = {
            "application": dict(row),
            "timeline": [dict(r) for r in repo.timeline(c.conn, app_id)],
            "rounds": [
                dict(r) | {"participants": repo.participants_for(c.conn, int(r["id"]))}
                for r in rounds
            ],
            "artifacts": [dict(r) for r in repo.artifacts_for(c.conn, app_id)],
            "posting_file": posting_sources[0].label if posting_sources else None,
        }
        _dump(payload)
        return
    fields = [
        ("id", row["id"]),
        ("company", row["company"]),
        ("role", row["role"]),
        ("status", str(row["current_status"])),
        ("status since", row["status_since"]),
        ("submitted on", row["submitted_on"]),
        ("intake folder", row["intake_folder"] or "-"),
        ("last activity", row["last_activity_on"]),
        ("discovery source", row["discovery_source"]),
        ("submission channel", row["submission_channel"]),
        ("recruiter", row["recruiter_type"]),
        ("platform", row["platform_name"] or "-"),
        ("location", row["location"] or "-"),
        ("url", row["job_url"] or "-"),
        ("posting", posting_sources[0].label if posting_sources else "-"),
        ("interviews / screens / assessments",
         f"{row['completed_interviews']} / {row['completed_screens']} / {row['completed_assessments']}"),
        ("notes", row["notes"] or "-"),
    ]
    ui.console.print(Panel.fit(
        "\n".join(f"[bold]{name}[/bold]: {value}" for name, value in fields),
        title=f"{row['company']} - {row['role']} [{ui.status_text(str(row['current_status']))}]",
    ))
    ui.render_timeline(repo.timeline(c.conn, app_id))
    rounds = repo.rounds_for(c.conn, app_id)
    participants = {int(r["id"]): repo.participants_for(c.conn, int(r["id"])) for r in rounds}
    ui.render_rounds(rounds, participants)
    artifact_rows = repo.artifacts_for(c.conn, app_id)
    ui.render_table("Artifacts", ("id", "type", "path", "label"),
                    [(r["id"], r["artifact_type"], r["relative_path"], r["label"] or "")
                     for r in artifact_rows],
                    empty="no artifacts yet")
    short = repo.short_id(str(row["id"]))
    if posting_sources:
        ui.console.print(f"[dim]read the stored posting again with `job posting {short}`[/dim]")
    else:
        ui.console.print("[dim]no stored posting text (capture one with ./posting)[/dim]")


@app.command()
def show(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Application id, id prefix, company, or role."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Show one application in full: attributes, timeline, rounds, files."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    row = repo.resolve_application(c.conn, ident)
    _show_application(c, str(row["id"]))


# --------------------------------------------------------------------------
# posting text (read the captured posting again)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PostingSource:
    """One stored file that holds the captured text of a job posting."""

    path: Path
    label: str
    origin: str


def _posting_sources(c: Ctx, app: sqlite3.Row) -> list[PostingSource]:
    """The stored posting copies of one application, best source first.

    The posting capture tool (./posting) writes ``posting.txt`` into the intake
    folder under applications/, so that copy comes first. A posting can also sit
    in the application's own folder, either as another ``posting.txt`` or as a
    file registered as a ``job_posting`` artifact. A missing file is skipped;
    a file that exists but cannot be read becomes a problem in the reader.
    """
    found: list[PostingSource] = []
    seen: set[Path] = set()

    def add(path: Path, origin: str) -> None:
        if path in seen:
            return
        try:
            # A symlink to a file is listed here on purpose: read_posting_file
            # refuses it with a problem instead of silently showing nothing.
            if not path.is_file():
                return
        except OSError:  # pragma: no cover - defensive
            return
        seen.add(path)
        try:
            label = str(path.relative_to(c.layout.root))
        except ValueError:  # pragma: no cover - defensive
            label = str(path)
        found.append(PostingSource(path=path, label=label, origin=origin))

    intake_folder = str(app["intake_folder"] or "")
    if intake_folder:
        add(c.layout.applications_dir / intake_folder / intake.POSTING_FILENAME,
            f"the intake folder applications/{intake_folder}")
    add(c.layout.applications_dir / str(app["id"]) / intake.POSTING_FILENAME,
        "the application folder")
    for row in repo.artifacts_for(c.conn, str(app["id"])):
        if str(row["artifact_type"]) != "job_posting":
            continue
        relative = str(row["relative_path"])
        if not relative.startswith("applications/"):
            continue
        add(c.layout.root / relative, "an attached posting file")
    return found


def _posting_text(sources: Sequence[PostingSource]) -> tuple[str | None, str | None]:
    """The text of the best source, or ``(None, None)`` when there is none."""
    if not sources:
        return None, None
    return intake.read_posting_file(sources[0].path)


def _posting_payload(c: Ctx, app: sqlite3.Row, sources: Sequence[PostingSource],
                     text: str | None, problem: str | None) -> dict:
    return {
        "application_id": str(app["id"]),
        "company": app["company"],
        "role": app["role"],
        "current_status": app["current_status"],
        "submitted_on": app["submitted_on"],
        "job_url": app["job_url"],
        "intake_folder": app["intake_folder"],
        "posting_file": sources[0].label if sources else None,
        "posting_origin": sources[0].origin if sources else None,
        "other_posting_files": [source.label for source in sources[1:]],
        "text": text,
        "problem": problem,
    }


def _report_no_posting(c: Ctx, app: sqlite3.Row) -> None:
    """Say why there is nothing to read. Goes to stderr, so --json stdout stays clean."""
    ui.note(f"[yellow]no stored posting text[/yellow] for"
            f" {repo.short_id(str(app['id']))}  {app['company']} - {app['role']}")
    if app["job_url"]:
        ui.note(f"job url: {app['job_url']}")
    intake_folder = str(app["intake_folder"] or "")
    if intake_folder:
        ui.note(f"[dim]applications/{intake_folder} has no {intake.POSTING_FILENAME};"
                " capture the posting with ./posting[/dim]")
    else:
        ui.note("[dim]no intake folder is linked: capture the posting with ./posting, or attach"
                " the text with `job attach <id> <file> --type job_posting`[/dim]")


def _show_posting(c: Ctx, app: sqlite3.Row, *, raw: bool = False) -> None:
    """Print the stored posting text of one application.

    ``raw`` leaves out the header, so ``job posting <id> --raw`` can be piped
    straight into a pager. A posting file that exists but cannot be read is an
    error; an application that never had a posting captured is not.
    """
    sources = _posting_sources(c, app)
    text, problem = _posting_text(sources)
    if c.json_output:
        _dump(_posting_payload(c, app, sources, text, problem))
        return
    if text is None:
        if problem is not None and sources:
            raise UsageError(f"cannot read {sources[0].label}: {problem}")
        _report_no_posting(c, app)
        return
    source = sources[0]
    if not raw:
        fields = [
            ("application",
             f"{repo.short_id(str(app['id']))}  {app['company']} - {app['role']}"),
            ("submitted", app["submitted_on"]),
            ("status", str(app["current_status"])),
            ("job url", app["job_url"] or "-"),
            ("posting file", source.label),
        ]
        ui.console.print(Panel.fit(
            "\n".join(f"[bold]{name}[/bold]: {value}" for name, value in fields),
            title="Job posting (stored copy)",
        ))
        for extra in sources[1:]:
            ui.note(f"[dim]also stored: {extra.label} ({extra.origin})[/dim]")
    ui.print_stored_text(text, wrap=not raw)


@app.command()
def posting(
    ctx: typer.Context,
    ident: str = typer.Argument(None, help="Application id, id prefix, company, or role."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    raw: bool = typer.Option(False, "--raw", help="Print only the stored text (no header)."),
) -> None:
    """Read the job posting of an application again.

    The text is the copy captured when the application was prepared: posting.txt
    in the intake folder under applications/ that ./posting wrote, or a file
    attached with --type job_posting. Nothing is fetched from the network, so
    what you read is exactly what was captured. See also `job show`.
    """
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    app = _resolve(c, ident, "Which posting do you want to read?")
    _show_posting(c, app, raw=raw)


# --------------------------------------------------------------------------
# intake folders (applications/)
# --------------------------------------------------------------------------
def _intake_payload(folder: intake.IntakeFolder, app_id: str | None) -> dict:
    return {
        "folder": folder.name,
        "company": folder.company,
        "posting_id": folder.posting_id,
        "source": intake.source_host(folder.source_url) if folder.source_url else "missing",
        "source_url": folder.source_url,
        "source_problem": folder.source_problem,
        "files": list(folder.files),
        "file_count": len(folder.files),
        "application_id": app_id,
    }


@app.command("intake")
def intake_cmd(
    ctx: typer.Context,
    pending_only: bool = typer.Option(False, "--pending",
                                      help="Only folders with no application registered yet."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """List the intake folders under applications/ and the application each one produced.

    A folder is named <company>[_<posting-id>] by the posting capture tool. The
    tracker only reads it: it is never renamed, moved, or deleted here.
    """
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    found = intake.scan(c.layout.applications_dir, exclude=repo.application_ids(c.conn))
    linked = repo.linked_intake_folders(c.conn)
    unlinked = intake.pending(found.folders, linked)
    folders = unlinked if pending_only else found.folders
    payload = [_intake_payload(folder, linked.get(folder.name)) for folder in folders]
    if c.json_output:
        _dump(payload)
        return
    title = f"Intake folders under {c.layout.applications_dir}"
    if pending_only:
        title += " (no application yet)"
    ui.render_table(
        title,
        ("folder", "company", "posting id", "source", "files", "application"),
        [
            (row["folder"], row["company"], row["posting_id"] or "-", row["source"],
             row["file_count"],
             repo.short_id(row["application_id"]) if row["application_id"] else "-")
            for row in payload
        ],
        empty=("every folder already has an application" if pending_only
               else "no intake folder here (capture a posting with ./posting)"),
    )
    if found.skipped:
        ui.note("[dim]skipped: " + ", ".join(f"{item.name} ({item.reason})"
                                             for item in found.skipped) + "[/dim]")
    if unlinked:
        ui.console.print(f"[dim]{len(unlinked)} folder(s) have no application yet. Register one"
                         " with `job add --from-folder <folder>`, or run `job add` and pick it"
                         " from the list.[/dim]")
    _proposals, ambiguous = intake.plan_links(found.folders, _link_rows(c))
    for item in ambiguous:
        candidates = ", ".join(repo.short_id(one) for one in item.application_ids)
        ui.console.print(f"[yellow]applications/{item.folder} could belong to {candidates}; link"
                         f" it with `job edit <id> --set intake_folder={item.folder}`[/yellow]")


# --------------------------------------------------------------------------
# upcoming / waiting
# --------------------------------------------------------------------------
@app.command()
def upcoming(
    ctx: typer.Context,
    as_of: str = typer.Option(None, "--as-of", help="Reference date (default today)."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Scheduled rounds that still need attention, including past-due ones."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    reference = rules.validate_date(as_of, field="as-of", allow_future=True) if as_of else rules.today()
    rows = reporting.upcoming(c.conn, c.layout, reference)
    if c.json_output:
        _dump([dict(row) for row in rows])
        return
    _print_upcoming(c, reference)


@app.command()
def waiting(
    ctx: typer.Context,
    as_of: str = typer.Option(None, "--as-of", help="Reference date (default today)."),
    stale_days: int = typer.Option(30, "--stale-days", help="Days without activity before a hint."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Waiting applications; hints about stale ones without closing anything."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    reference = rules.validate_date(as_of, field="as-of", allow_future=True) if as_of else rules.today()
    rows = reporting.stale(c.conn, c.layout, reference, stale_days)
    if c.json_output:
        _dump([dict(row) for row in rows])
        return
    _print_waiting(c, reference, stale_days)


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
@app.command()
def stats(
    ctx: typer.Context,
    as_of: str = typer.Option(None, "--as-of", help="Cutoff date (default today)."),
    cohort_from: str = typer.Option(None, "--from", help="Cohort start (submitted_on >=)."),
    cohort_to: str = typer.Option(None, "--to", help="Cohort end (submitted_on <=)."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Aggregate statistics for a submission cohort. Contains no personal data."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    start = rules.validate_date(cohort_from, field="from", allow_future=True) if cohort_from else None
    end = rules.validate_date(cohort_to, field="to", allow_future=True) if cohort_to else None
    reference = rules.validate_date(as_of, field="as-of", allow_future=True) if as_of else None
    if start and end and start > end:
        raise UsageError("--from is after --to")
    result = reporting.statistics(c.conn, c.layout, as_of=reference, cohort_from=start,
                                  cohort_to=end)
    if c.json_output:
        _dump(result)
        return
    ui.render_stats(result)


def _render_upcoming(c: Ctx, reference: str) -> None:
    _print_upcoming(c, reference)


# --------------------------------------------------------------------------
# edit / undo / delete
# --------------------------------------------------------------------------
def _parse_sets(values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise UsageError(f"--set expects field=value, got '{item}'")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise UsageError(f"--set expects field=value, got '{item}'")
        parsed[key] = value.strip()
    return parsed


def _guard(write):
    try:
        return write()
    except sqlite3.IntegrityError as exc:
        raise UsageError(f"the database rejected that change: {exc}") from exc


def _checked_intake_link(c: Ctx, value: str | None) -> str | None:
    """Validate an intake-folder link against the folders that exist on disk.

    Blank, ``none``, ``null``, and ``-`` clear the link; an unknown folder name
    is refused instead of being stored as a link that `job intake` cannot
    resolve.
    """
    name = intake.normalize_link(value)
    if name is None:
        return None
    folder = intake.find(c.layout.applications_dir, name)
    if repo.application_id_exists(c.conn, folder.name):
        raise UsageError(
            f"applications/{folder.name} belongs to an existing application; link the folder"
            " the application was registered from instead"
        )
    return folder.name


@app.command()
def edit(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Application id, id prefix, company, or role."),
    set_values: list[str] = typer.Option(None, "--set", help="field=value (repeatable)."),
) -> None:
    """Correct application attributes (not history: use edit-event for that)."""
    c = _ctx(ctx)
    row = repo.resolve_application(c.conn, ident)
    app_id = str(row["id"])
    updates = _parse_sets(set_values)
    if not updates:
        ui.require_interactive()
        field = ui.choice(
            "Which attribute?",
            [(name, name) for name in repo.APPLICATION_FIELDS],
        )
        current = row[field]
        if field in ("discovery_source", "submission_channel", "recruiter_type"):
            # The stored value is the recommendation, so it is listed first and
            # Enter still keeps it; the cursor starts on the first entry.
            value = ui.choice(f"New value for {field}",
                              ui.first_default(_lookup_choices(c, field), str(current)),
                              default=str(current))
        elif field == "submitted_on":
            value = ui.date(f"New submission date (currently {current})", default=str(current))
        elif field == "intake_folder":
            value = ui.text(f"Intake folder below applications/ (currently {current or '-'};"
                            " blank clears it)", allow_blank=True)
        else:
            value = ui.text(f"New value for {field} (currently {current})", allow_blank=True)
        updates = {field: value}
    if "intake_folder" in updates:
        updates["intake_folder"] = _checked_intake_link(c, updates["intake_folder"])
    if "submitted_on" in updates:
        updates["submitted_on"] = rules.validate_date(updates["submitted_on"],
                                                      field="submission date")
        latest = repo.timeline(c.conn, app_id)
        if len(latest) > 1:
            ui.console.print("[dim]Later events keep their own dates; the database rejects a"
                             " submission date after an existing event.[/dim]")
    changed = _guard(lambda: repo.update_application(c.conn, app_id, **updates))
    if c.json_output:
        _dump(dict(repo.get_application(c.conn, app_id)))
        return
    ui.console.print(f"[green]updated[/green] {', '.join(changed)}")
    _show_status(c, app_id)


@app.command("edit-round")
def edit_round_cmd(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Application id, id prefix, company, or role."),
    round_flag: str = typer.Option(..., "--round", help="Round id or sequence number."),
    set_values: list[str] = typer.Option(None, "--set", help="field=value (repeatable)."),
    participants: str = typer.Option(None, "--participants", help="Replace participant roles."),
) -> None:
    """Correct a round: phase, type, format, label, notes, participants."""
    c = _ctx(ctx)
    app = repo.resolve_application(c.conn, ident)
    round_row = repo.find_round(c.conn, str(app["id"]), round_flag)
    round_id = int(round_row["id"])
    updates = _parse_sets(set_values)
    if updates:
        _guard(lambda: repo.update_round(c.conn, round_id, **updates))
    if participants is not None:
        roles = [p.strip() for p in participants.split(",") if p.strip()]
        _guard(lambda: repo.set_participants(c.conn, round_id, roles))
    if not updates and participants is None:
        raise UsageError("nothing to change: pass --set and/or --participants")
    ui.console.print(f"[green]updated round {round_id}[/green]")
    _show_rounds(c, str(app["id"]))


def _show_rounds(c: Ctx, app_id: str) -> None:
    rounds = repo.rounds_for(c.conn, app_id)
    participants = {int(r["id"]): repo.participants_for(c.conn, int(r["id"])) for r in rounds}
    ui.render_rounds(rounds, participants)


def _coerce_event_updates(updates: dict[str, str]) -> dict[str, object]:
    """--set values arrive as text; convert the typed event fields."""
    unknown = set(updates) - set(repo.EVENT_FIELDS)
    if unknown:
        raise UsageError(
            "cannot update event field(s): " + ", ".join(sorted(unknown))
            + " (allowed: " + ", ".join(repo.EVENT_FIELDS) + ")"
        )
    coerced: dict[str, object] = {}
    for name, value in updates.items():
        if name == "occurred_on":
            coerced[name] = rules.validate_date(value, field="event date")
        elif name == "round_id":
            text = str(value).strip().lower()
            if text in ("", "none", "-", "null"):
                coerced[name] = None
            elif text.lstrip("-").isdigit():
                coerced[name] = int(text)
            else:
                raise UsageError(f"round_id must be a number, got '{value}'")
        elif name == "scheduled_time":
            coerced[name] = rules.validate_time(value) if value else None
        elif name == "scheduled_for":
            coerced[name] = (rules.validate_date(value, field="appointment date",
                                                 allow_future=True) if value else None)
        elif name == "timezone":
            coerced[name] = value or None
        else:
            coerced[name] = value
    return coerced


def _event_proposal(c: Ctx, event: sqlite3.Row, updates: dict[str, object]) -> dict:
    proposed = {name: event[name] for name in repo.EVENT_FIELDS}
    proposed.update(updates)
    return proposed


def _preview_event_edit(c: Ctx, event: sqlite3.Row, proposed: dict) -> None:
    changes = []
    for name in repo.EVENT_FIELDS:
        old_value, new_value = event[name], proposed.get(name)
        if old_value != new_value:
            changes.append(f"{name}: {old_value!r} -> {new_value!r}")
    before = repo.get_application(c.conn, str(event["application_id"]))
    ui.console.print(
        f"correcting event {int(event['id'])} ({event['event_label']}); "
        f"current application state '{before['current_status']}'"
    )
    for change in changes:
        ui.console.print(f"  [cyan]{change}[/cyan]")
    if not c.interactive:
        return
    if not ui.confirm("Apply this correction?", default=True):
        raise Cancelled("correction declined")


@app.command("edit-event")
def edit_event_cmd(
    ctx: typer.Context,
    event_id: int = typer.Argument(..., help="Event id (see show)."),
    set_values: list[str] = typer.Option(None, "--set", help="field=value (repeatable)."),
    allow_state_regression: bool = typer.Option(
        False, "--allow-state-regression",
        help="Confirm moving an offered, accepted, or started application back to in_progress."),
) -> None:
    """Correct one recorded event, including its date, after validating the history."""
    c = _ctx(ctx)
    updates = _coerce_event_updates(_parse_sets(set_values))
    if not updates:
        raise UsageError("nothing to change: pass --set field=value")
    event = repo.get_event(c.conn, event_id)
    app_id = str(event["application_id"])
    proposed = _event_proposal(c, event, updates)
    if "round_id" in updates and proposed["round_id"] is not None:
        repo.find_round_by_id(c.conn, int(proposed["round_id"]), app_id)
    schedule_issues = rules.check_event_schedule(
        str(proposed["event_type"]), str(proposed["occurred_on"]),
        proposed["scheduled_for"], proposed["scheduled_time"], proposed["timezone"],
    )
    errors = [issue for issue in schedule_issues if issue.is_error]
    if errors:
        raise UsageError("; ".join(issue.message for issue in errors))
    for issue in schedule_issues:
        _print_issues([issue])
    _preview_event_edit(c, event, proposed)

    consent = allow_state_regression
    fresh_warnings: list[rules.Issue] = []
    for _attempt in range(4):
        try:
            _, fresh_warnings = _write_validated(
                c, app_id, lambda: repo.update_event(c.conn, event_id, **updates),
                allow_regression=consent,
            )
            break
        except HistoryConflict as conflict:
            if not c.interactive:
                raise UsageError(_conflict_message(conflict)) from None
            _print_issues(conflict.issues)
            if all(issue.code == "state_regression" for issue in conflict.issues):
                if ui.confirm("Apply it and move the application back to 'in_progress'?",
                              default=False):
                    consent = True
                    continue
                raise Cancelled("state regression declined")
            raise UsageError(_conflict_message(conflict)) from None
    else:
        raise UsageError("the correction could not be validated")
    event = repo.get_event(c.conn, event_id)
    ui.console.print(f"[green]updated[/green] event {event_id} ({event['event_label']})")
    _print_issues(fresh_warnings)
    _show_status(c, app_id)


@app.command()
def undo(
    ctx: typer.Context,
    ident: str = typer.Argument(None, help="Application id, id prefix, company, or role."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the deletion."),
) -> None:
    """Review and delete the most recently entered event (recover from a mistake)."""
    c = _ctx(ctx)
    c.yes = c.yes or yes
    app = _resolve(c, ident, "Which application?")
    app_id = str(app["id"])
    event = repo.last_event(c.conn, app_id)
    if event is None:
        raise UsageError("this application has no events to undo")
    meta = repo.event_type_row(c.conn, str(event["event_type"]))
    event_id = int(event["id"])
    ui.console.print(f"latest entered event: [bold]{meta['label']}[/bold]"
                     f" on {event['occurred_on']} (order {event['day_order']}),"
                     f" recorded {event['recorded_at']}")
    round_id = event["round_id"]
    round_row = None
    disposable = False
    if round_id is not None:
        round_row = repo.round_row(c.conn, int(round_id))
        disposable = repo.round_is_disposable(c.conn, int(round_id), event_id)
        if disposable:
            ui.console.print(f"[yellow]round #{round_row['sequence_no']} was created with this"
                             " update and holds nothing else; it would be removed too[/yellow]")
        else:
            ui.console.print(f"[dim]round #{round_row['sequence_no']} stays (it has other events,"
                             " participants, or notes)[/dim]")
    if not _confirm(c, "Delete this event?", default=False):
        raise Cancelled("undo declined")
    remove_round = False
    if disposable:
        remove_round = True if c.yes else _confirm(
            c, f"Also delete the now-empty round #{round_row['sequence_no']}?", default=True
        )
    _backup_before_change(c, "delete")

    def _undo_write():
        repo.delete_event(c.conn, event_id)
        if remove_round:
            repo.delete_round(c.conn, int(round_id))

    _validated_delete(c, app_id, _undo_write)
    ui.console.print("[green]deleted[/green]" + (" the event and its empty round" if remove_round
                                                 else ""))
    if not remove_round and round_row is not None and repo.round_is_disposable(
            c.conn, int(round_id), event_id):
        ui.console.print(f"[yellow]note[/yellow] empty round #{round_row['sequence_no']} remains"
                         f" (remove it with `job delete round {round_id}`)")
    _show_status(c, app_id)


def _delete_target(c: Ctx, kind: str, ident: str, *, remove_files: bool = False) -> None:
    if kind == "event":
        event_id = int(ident)
        preview = repo.deletion_preview(c.conn, "event", event_id)
        ui.console.print(f"Deleting event: {preview['event']}")
        if not _confirm(c, "Delete this event?", default=False):
            raise Cancelled("event not deleted")
        _backup_before_change(c, "delete")
        _validated_delete(c, str(preview["application"]),
                          lambda: repo.delete_event(c.conn, event_id))
        ui.console.print("[green]deleted[/green]")
        return
    if kind == "round":
        round_id = int(ident)
        preview = repo.deletion_preview(c.conn, "round", round_id)
        ui.console.print(f"Deleting round {preview['round']} also removes {preview['events']}"
                         f" event(s) and {preview['participants']} participant link(s).")
        if not _confirm(c, "Delete this round and its events?", default=False):
            raise Cancelled("round not deleted")
        _backup_before_change(c, "delete")
        _validated_delete(c, str(preview["application"]),
                          lambda: repo.delete_round(c.conn, round_id))
        ui.console.print("[green]deleted[/green]")
        return
    app = repo.resolve_application(c.conn, ident)
    app_id = str(app["id"])
    preview = repo.deletion_preview(c.conn, "application", app_id)
    # Check the folder before touching the database: an unsafe path must abort
    # everything instead of leaving a deleted row plus a half-finished cleanup.
    directory = artifacts.deletion_directory(c.layout, app_id) if remove_files else None
    ui.console.print(f"Deleting {preview['application']} removes {preview['events']} event(s),"
                     f" {preview['rounds']} round(s), and {preview['artifacts']} artifact record(s)."
                     + (f" The folder {directory} and its files stay on disk unless confirmed."
                        if remove_files else " Artifact files stay on disk."))
    intake_link = app["intake_folder"] if "intake_folder" in app.keys() else None
    if intake_link and (c.layout.applications_dir / intake_link).is_dir():
        ui.console.print(f"[dim]the intake folder applications/{intake_link} keeps its posting,"
                         " source and CVs; remove it yourself if you want to[/dim]")
    if not _confirm(c, "Delete this application?", default=False):
        raise Cancelled("application not deleted")
    remove_folder = False
    if remove_files and directory is not None:
        remove_folder = _confirm(c, f"Also delete the folder {directory} and its files?",
                                 default=False)
    _backup_before_change(c, "delete")
    _guard(lambda: repo.delete_application(c.conn, app_id))
    ui.console.print("[green]deleted[/green]")
    if remove_files:
        if directory is None:
            ui.console.print("[dim]no artifact folder on disk to remove[/dim]")
        elif not remove_folder:
            ui.console.print(f"[yellow]folder kept[/yellow] {directory}")
        else:
            removed = artifacts.remove_application_dir(c.layout, app_id)
            ui.console.print(f"[green]removed[/green] {removed}")
    elif (c.layout.applications_dir / app_id).is_dir():
        directory = c.layout.applications_dir / app_id
        files = sorted(p.name for p in directory.iterdir())
        ui.console.print(f"[yellow]files kept on disk[/yellow] in {directory}"
                         + (f": {', '.join(files)}" if files else ""))


@delete_app.command("event")
def delete_event_cmd(ctx: typer.Context, event_id: int = typer.Argument(...),
                     yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete one event (with a preview and a backup)."""
    c = _ctx(ctx)
    c.yes = c.yes or yes
    _delete_target(c, "event", str(event_id))


@delete_app.command("round")
def delete_round_cmd(ctx: typer.Context, round_id: int = typer.Argument(...),
                     yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete one round, its events, and its participant links."""
    c = _ctx(ctx)
    c.yes = c.yes or yes
    _delete_target(c, "round", str(round_id))


@delete_app.command("application")
def delete_application_cmd(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Application id, id prefix, company, or role."),
    remove_files: bool = typer.Option(False, "--remove-files",
                                      help="Also delete the artifact folder after confirmation."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete one application and its dependent rows. Artifact files stay unless asked."""
    c = _ctx(ctx)
    c.yes = c.yes or yes
    _delete_target(c, "application", ident, remove_files=remove_files)


# --------------------------------------------------------------------------
# attach
# --------------------------------------------------------------------------
def _attach_flow(c: Ctx, app: sqlite3.Row, path: str | None = None,
                 artifact_type: str | None = None, name: str | None = None,
                 label: str | None = None, overwrite: bool = False) -> None:
    app_id = str(app["id"])
    ui.require_interactive()
    if not path:
        path = ui.text("Path of the file to copy into the application folder")
    if not artifact_type:
        artifact_type = ui.choice(
            "Artifact type", ui.first_default(_lookup_choices(c, "artifact_type"), "other"),
            default="other")
    result = artifacts.import_artifact(c.layout, c.conn, app_id, path, artifact_type=artifact_type,
                                       name=name, label=label, overwrite=overwrite)
    ui.console.print(f"[green]attached[/green] {result.relative_path}")
    ui.console.print("[dim]the original file was copied, not moved[/dim]")


@app.command()
def attach(
    ctx: typer.Context,
    ident: str = typer.Argument(..., help="Application id, id prefix, company, or role."),
    file: str = typer.Argument(..., help="File to copy into applications/<uuid>/."),
    artifact_type: str = typer.Option("other", "--type", help="job_posting, cv, cover_letter, other."),
    name: str = typer.Option(None, "--name", help="Store under this filename."),
    label: str = typer.Option(None, "--label", help="Free-text label."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing file."),
) -> None:
    """Copy a posting, CV, cover letter, or other file into the application folder."""
    c = _ctx(ctx)
    app = repo.resolve_application(c.conn, ident)
    result = _guard(lambda: artifacts.import_artifact(
        c.layout, c.conn, str(app["id"]), file, artifact_type=artifact_type, name=name,
        label=label, overwrite=overwrite,
    ))
    if c.json_output:
        _dump({"artifact_id": result.artifact_id, "relative_path": result.relative_path,
               "replaced": result.replaced})
        return
    ui.console.print(f"[green]attached[/green] {result.relative_path}")
    ui.console.print("[dim]the original file was copied, not moved[/dim]")


# --------------------------------------------------------------------------
# db maintenance
# --------------------------------------------------------------------------
@db_app.command("status")
def db_status(
    ctx: typer.Context,
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Show database path, version, pending migrations, and row counts."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    state = migrations.inspect(c.layout.db_path, c.layout.migrations_dir, conn=c.conn)
    counts = {}
    for table in ("applications", "rounds", "events", "artifacts", "round_participants"):
        counts[table] = int(dbmod.scalar(c.conn, f"SELECT count(*) FROM {table}") or 0)
    info = {
        "database": str(c.layout.db_path),
        "project_root": str(c.layout.root),
        "size_bytes": c.layout.db_path.stat().st_size if c.layout.db_path.exists() else 0,
        "sqlite_runtime": dbmod.sqlite_runtime_version(),
        "user_version": state.user_version,
        "applied_migrations": {f"{v:03d}": n for v, n in state.applied.items()},
        "pending_migrations": [m.display for m in state.pending],
        "row_counts": counts,
    }
    if c.json_output:
        _dump(info)
        return
    ui.console.print(Panel.fit("\n".join(f"[bold]{k}[/bold]: {v}" for k, v in info.items()),
                               title="Database status"))


@db_app.command("migrate")
def db_migrate(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Only list what would be applied."),
) -> None:
    """Apply pending numbered migrations (with a backup for an existing database)."""
    c = _ctx(ctx)
    state = migrations.inspect(c.layout.db_path, c.layout.migrations_dir, conn=c.conn)
    if not state.pending:
        ui.console.print("[green]database is up to date at version"
                         f" {state.current_version}[/green]")
        return
    listing = ", ".join(m.display for m in state.pending)
    if dry_run:
        ui.console.print(f"would apply: {listing}")
        return
    if not c.yes and c.interactive:
        if not ui.confirm(f"Apply {listing}?", default=True):
            raise Cancelled("migration declined")
    applied, backup_path, problems = _apply_migrations(c.layout, state)
    ui.console.print("[green]applied " + ", ".join(m.display for m in applied) + "[/green]")
    if backup_path:
        ui.console.print(f"[dim]backup: {backup_path}[/dim]")
    if problems:
        raise MigrationError("post-migration checks failed: " + "; ".join(problems))


@db_app.command("backup")
def db_backup(ctx: typer.Context, label: str = typer.Option("job", "--label")) -> None:
    """Copy the database into data/backups/ with the SQLite backup API."""
    c = _ctx(ctx)
    path = migrations.backup(c.conn, c.layout.backups_dir, label=label)
    ui.console.print(f"[green]backup written[/green] {path}")


@db_app.command("restore")
def db_restore(
    ctx: typer.Context,
    backup_file: str = typer.Argument(..., help="Backup file to restore."),
    skip_backup: bool = typer.Option(False, "--no-backup", help="Do not back up the current file."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Replace the database with a backup (taking a safety backup first)."""
    c = _ctx(ctx)
    c.yes = c.yes or yes
    source = Path(backup_file).expanduser()
    if not source.exists():
        raise UsageError(f"backup file not found: {source}")
    if not _confirm(c, f"Replace {c.layout.db_path} with {source}?", default=False):
        raise Cancelled("restore declined")
    safety = None
    if not skip_backup and c.layout.db_path.exists():
        safety = migrations.backup_file(c.layout.db_path, c.layout.backups_dir, label="pre-restore")
        ui.console.print(f"[dim]safety backup: {safety}[/dim]")
    c.conn.close()
    migrations.restore(source, c.layout.db_path)
    ui.console.print(f"[green]restored[/green] {c.layout.db_path}")


@db_app.command("doctor")
def db_doctor(
    ctx: typer.Context,
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Run integrity, foreign key, and artifact checks."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    problems = migrations.verify(c.conn)
    report = artifacts.doctor_report(c.layout, c.conn)
    payload = {"integrity": problems, **report}
    if c.json_output:
        _dump(payload)
        return
    if problems:
        _print_issues([rules.Issue("error", "integrity", p) for p in problems])
    else:
        ui.console.print("[green]integrity_check and foreign_key_check are clean[/green]")
    ui.render_table("Artifacts registered but missing on disk",
                    ("path",), [(p,) for p in report["missing_files"]], empty="none")
    ui.render_table("Files on disk that are not registered",
                    ("path",), [(p,) for p in report["unregistered_files"]], empty="none")
    if report["untracked_folders"]:
        ui.render_table(
            "Files outside registered applications (intake folders, left as they are)",
            ("path",), [(p,) for p in report["untracked_folders"]],
        )
    if report["unsafe_paths"]:
        ui.render_table("Unsafe artifact paths", ("path",), [(p,) for p in report["unsafe_paths"]])


# --------------------------------------------------------------------------
# exports
# --------------------------------------------------------------------------
def _export_filters(as_of: str | None, cohort_from: str | None, cohort_to: str | None):
    start = rules.validate_date(cohort_from, field="from", allow_future=True) if cohort_from else None
    end = rules.validate_date(cohort_to, field="to", allow_future=True) if cohort_to else None
    reference = rules.validate_date(as_of, field="as-of", allow_future=True) if as_of else None
    if start and end and start > end:
        raise UsageError("--from is after --to")
    return reference, start, end


@export_app.command("snapshot")
def export_snapshot(
    ctx: typer.Context,
    as_of: str = typer.Option(None, "--as-of"),
    cohort_from: str = typer.Option(None, "--from"),
    cohort_to: str = typer.Option(None, "--to"),
    output: str = typer.Option(None, "--output", help="File or directory (default exports/)."),
    private: bool = typer.Option(False, "--private",
                                 help="Acknowledge that this file is private."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Write a private row-level snapshot of the cohort (identified data)."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    if not private:
        raise UsageError(
            "the snapshot contains company names, roles, urls, and paths;"
            " pass --private to confirm you want a private file (use `export sankey` for a"
            " shareable aggregate)"
        )
    reference, start, end = _export_filters(as_of, cohort_from, cohort_to)
    path = reporting.write_snapshot(c.conn, c.layout, as_of=reference, cohort_from=start,
                                   cohort_to=end, output=output)
    ui.console.print(f"[green]private snapshot written[/green] {path}")


@export_app.command("sankey")
def export_sankey(
    ctx: typer.Context,
    as_of: str = typer.Option(None, "--as-of"),
    cohort_from: str = typer.Option(None, "--from"),
    cohort_to: str = typer.Option(None, "--to"),
    output: str = typer.Option(None, "--output",
                               help="Directory for the whole bundle."),
    chart: bool = typer.Option(True, "--chart/--no-chart",
                               help="Also write the node/link JSON and the rendered chart."),
    png: bool = typer.Option(True, "--png/--no-png",
                             help="Also rasterize the chart to PNG (needs a local converter)."),
) -> None:
    """Write one timestamped aggregate bundle: edges CSV, metadata, chart data, image."""
    c = _ctx(ctx)
    reference, start, end = _export_filters(as_of, cohort_from, cohort_to)
    bundle = reporting.export_sankey(c.conn, c.layout, as_of=reference, cohort_from=start,
                                     cohort_to=end, output=output, chart=chart, png=png)
    metadata = json.loads(bundle.metadata.read_text(encoding="utf-8"))
    if not chart and png:
        ui.note("[yellow]--no-chart is set, so no chart data, SVG, or PNG was written[/yellow]")
    if c.json_output:
        _dump({
            "directory": str(bundle.directory),
            "edges": str(bundle.edges),
            "metadata": str(bundle.metadata),
            "node_link": None if bundle.node_link is None else str(bundle.node_link),
            "svg": None if bundle.svg is None else str(bundle.svg),
            "png": None if bundle.png is None else str(bundle.png),
            "png_tool": bundle.png_tool,
            "png_note": bundle.png_note,
            "cohort_size": metadata["cohort_size"],
            "edges_count": metadata["edge_count"],
            "artifacts": metadata["artifacts"],
        })
        return
    _print_bundle(bundle, metadata)
    if bundle.png_note:
        ui.note(f"[yellow]{bundle.png_note}[/yellow]")


def _print_bundle(bundle: reporting.SankeyExport, metadata: dict) -> None:
    """List what one export wrote. The directory name is the daily timestamp."""
    ui.console.print(f"[green]aggregate export written[/green] into {bundle.directory}")
    ui.console.print(f"  {bundle.edges.name}  edge list (source, target, value)")
    ui.console.print(f"  {bundle.metadata.name}  filters, counting rules, frozen replay data")
    if bundle.node_link is not None:
        ui.console.print(f"  {bundle.node_link.name}  node/link data for a chart tool")
    if bundle.svg is not None:
        ui.console.print(f"  {bundle.svg.name}  rendered chart (vector)")
    if bundle.png is not None:
        ui.console.print(f"  {bundle.png.name}  rendered chart (raster, via {bundle.png_tool})")
    ui.console.print(f"cohort {metadata['cohort_size']} applications,"
                     f" {metadata['edge_count']} edges; no identifying fields")


@export_app.command("replay")
def export_replay(
    ctx: typer.Context,
    metadata_file: str = typer.Argument(..., help="sankey-metadata.json from an earlier export."),
    json_flag: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
) -> None:
    """Re-run a saved snapshot query and compare with the stored aggregate edges."""
    c = _ctx(ctx)
    if json_flag:
        c.json_output = True
    result = reporting.replay_sankey(c.conn, metadata_file, layout=c.layout)
    if c.json_output:
        _dump(result)
        return
    if result["reproduced"]:
        ui.console.print(f"[green]reproduced[/green] the stored aggregate"
                         f" ({result['replayed_cohort_size']} applications)")
    else:
        ui.render_table("Differences", ("edge", "replayed", "stored"),
                        [(f"{d['edge'][0]} -> {d['edge'][1]}", d["replayed"], d["stored"])
                         for d in result["differences"]])
        ui.console.print(f"[yellow]replay differs[/yellow] (cohort now"
                         f" {result['replayed_cohort_size']}, stored {result['stored_cohort_size']})"
                         " - later corrections change an as-of result by design")


# --------------------------------------------------------------------------
# interactive menu
# --------------------------------------------------------------------------
def _choose_from_rows(c: Ctx, rows, message: str = "Select an application") -> sqlite3.Row:
    options = [
        (
            str(row["id"]),
            f"{repo.short_id(str(row['id']))}  {row['company']} - {row['role']}"
            f"  | {row['current_status']} | {row['submitted_on']}",
        )
        for row in rows[:40]
    ]
    if not options:
        raise UsageError("no applications to choose from")
    return repo.get_application(c.conn, ui.choice(message, options))


def _print_upcoming(c: Ctx, reference: str) -> None:
    rows = reporting.upcoming(c.conn, c.layout, reference)
    ui.render_table(
        f"Upcoming and past-due rounds (as of {reference})",
        ("date", "time", "company", "role", "round", "state", "note"),
        [
            (
                row["scheduled_for"],
                row["scheduled_time"] or "-",
                row["company"],
                row["role"],
                f"#{row['sequence_no']} {row['phase']} {row['round_type']}",
                row["round_status"],
                "needs update: appointment has passed" if row["past_due"] else "",
            )
            for row in rows
        ],
        empty="nothing scheduled and waiting",
    )


def _print_waiting(c: Ctx, reference: str, stale_days: int) -> None:
    rows = reporting.stale(c.conn, c.layout, reference, stale_days)
    if not rows:
        ui.console.print(f"[green]no waiting application is stale at {stale_days} days[/green]")
        return
    ui.render_table(
        f"Waiting for a response (no activity for >= {stale_days} days, as of {reference})",
        ("id", "company", "role", "status", "last activity", "days quiet", "interviews"),
        [
            (repo.short_id(str(row["id"])), row["company"], row["role"], row["current_status"],
             row["last_activity_on"], row["days_since_activity"], row["completed_interviews"])
            for row in rows
        ],
    )
    ui.render_overview_summary(reporting.tracker_summary(c.conn, c.layout), shown=len(rows))
    ui.console.print("[dim]Review hint only: nothing is closed automatically and these are not"
                     " counted as rejections.[/dim]")


def _print_stats(c: Ctx, as_of: str | None = None, cohort_from: str | None = None,
                 cohort_to: str | None = None) -> None:
    reference, start, end = _export_filters(as_of, cohort_from, cohort_to)
    ui.render_stats(reporting.statistics(c.conn, c.layout, as_of=reference, cohort_from=start,
                                         cohort_to=end))


def _post_add(c: Ctx, app_id: str) -> None:
    row = repo.get_application(c.conn, app_id)
    if c.json_output:
        _dump(dict(row))
        return
    ui.console.print(f"[green]saved[/green] {repo.short_id(app_id)}  {row['company']} - {row['role']}"
                     f" (submitted {row['submitted_on']})")
    if not c.interactive:
        return
    action = ui.choice(
        "Next step",
        [("log", "Log an update"), ("posting", "Read the job posting"),
         ("attach", "Attach documents"), ("show", "Show the application"),
         ("back", "Back to the menu")],
        default="log",
    )
    if action == "log":
        _log_flow(c, row, event=None, on_date=None, round_flag=None)
    elif action == "posting":
        _show_posting(c, row)
    elif action == "attach":
        _attach_flow(c, row)
    elif action == "show":
        _show_application(c, app_id)


def _menu_add(c: Ctx) -> None:
    app_id = _add_application(c, company=None, role=None, submitted_on=None, force=False)
    _post_add(c, app_id)


def _menu_log(c: Ctx) -> None:
    app = _prompt_application(c, "Which application do you want to update?")
    _log_flow(c, app, event=None, on_date=None, round_flag=None)


def _menu_posting(c: Ctx) -> None:
    app = _prompt_application(c, "Which posting do you want to read?")
    _show_posting(c, app)


def _menu_browse(c: Ctx) -> None:
    search = ui.text("Search company or role (blank lists recent)", allow_blank=True)
    rows = reporting.overview(c.conn, c.layout, search=search or None)
    if not rows:
        ui.console.print("[yellow]no applications match[/yellow]")
        return
    _render_overview(c, rows)
    if ui.confirm("Open one of these?", default=False):
        app = _choose_from_rows(c, rows)
        _show_application(c, str(app["id"]))


def _ask_date_filter(message: str) -> str | None:
    """Interactive date filter with the same validation as the command flags."""
    while True:
        value = ui.optional_text(message)
        if not value:
            return None
        try:
            return rules.validate_date(value, field="date", allow_future=True)
        except UsageError as exc:
            ui.console.print(f"[red]{exc}[/red]")


def _ask_cohort_filters() -> tuple[str | None, str | None]:
    while True:
        start = _ask_date_filter("Cohort from (blank = all)")
        end = _ask_date_filter("Cohort to (blank = all)")
        if start and end and start > end:
            ui.console.print("[red]the cohort start is after the cohort end; try again[/red]")
            continue
        return start, end


def _ask_positive_int(message: str, default: int) -> int:
    while True:
        value = ui.text(message, default=str(default)).strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
        ui.console.print("[red]enter a whole number greater than zero[/red]")


def _menu_waiting(c: Ctx) -> None:
    reference = ui.date("Reference date", default=rules.today())
    reference = rules.validate_date(reference, field="reference date")
    _print_upcoming(c, reference)
    stale_days = _ask_positive_int("Stale threshold in days", 30)
    _print_waiting(c, reference, stale_days)


def _menu_stats(c: Ctx) -> None:
    action = ui.choice(
        "Statistics and export",
        [
            ("stats", "Show statistics"),
            ("sankey", "Export aggregate Sankey bundle (data + image, shareable)"),
            ("snapshot", "Export private snapshot (identifying data)"),
            ("replay", "Verify a saved Sankey export"),
            ("back", "Back"),
        ],
        default="stats",
    )
    if action == "back":
        return
    if action == "stats":
        _print_stats(c)
        return
    as_of = _ask_date_filter("Cutoff date (blank = today)")
    cohort_from, cohort_to = _ask_cohort_filters()
    if action == "sankey":
        bundle = reporting.export_sankey(c.conn, c.layout, as_of=as_of,
                                         cohort_from=cohort_from, cohort_to=cohort_to)
        metadata = json.loads(bundle.metadata.read_text(encoding="utf-8"))
        _print_bundle(bundle, metadata)
        if bundle.png_note:
            ui.note(f"[yellow]{bundle.png_note}[/yellow]")
    elif action == "snapshot":
        ui.console.print("[yellow]This file will contain company names, roles, urls, and paths.[/yellow]")
        if ui.confirm("Write the private snapshot into exports/?", default=False):
            path = reporting.write_snapshot(c.conn, c.layout, as_of=as_of,
                                            cohort_from=cohort_from, cohort_to=cohort_to)
            ui.console.print(f"[green]written[/green] {path}")
    elif action == "replay":
        meta_file = ui.text("Path to sankey-metadata.json")
        result = reporting.replay_sankey(c.conn, meta_file, layout=c.layout)
        ui.console.print("[green]reproduced[/green]" if result["reproduced"]
                         else f"[yellow]replay differs[/yellow] {result['differences']}")


def _menu_db(c: Ctx) -> None:
    action = ui.choice(
        "Database maintenance",
        [
            ("status", "Status and row counts"),
            ("backup", "Write a backup into data/backups/"),
            ("doctor", "Integrity, foreign key, and artifact checks"),
            ("migrate", "Apply pending migrations"),
            ("back", "Back"),
        ],
        default="status",
    )
    if action == "back":
        return
    if action == "status":
        _print_db_status(c)
    elif action == "backup":
        path = migrations.backup(c.conn, c.layout.backups_dir)
        ui.console.print(f"[green]backup written[/green] {path}")
    elif action == "doctor":
        _print_doctor(c)
    elif action == "migrate":
        state = migrations.inspect(c.layout.db_path, c.layout.migrations_dir, conn=c.conn)
        if not state.pending:
            ui.console.print("[green]database is up to date[/green]")
            return
        if ui.confirm("Apply " + ", ".join(m.display for m in state.pending) + "?", default=True):
            applied, backup_path, problems = _apply_migrations(c.layout, state)
            ui.console.print("[green]applied " + ", ".join(m.display for m in applied) + "[/green]")
            if backup_path:
                ui.console.print(f"[dim]backup: {backup_path}[/dim]")
            if problems:
                ui.console.print("[red]" + "; ".join(problems) + "[/red]")


def _print_db_status(c: Ctx) -> None:
    state = migrations.inspect(c.layout.db_path, c.layout.migrations_dir, conn=c.conn)
    counts = {
        table: int(dbmod.scalar(c.conn, f"SELECT count(*) FROM {table}") or 0)
        for table in ("applications", "rounds", "events", "artifacts")
    }
    ui.console.print(f"[bold]database[/bold]: {c.layout.db_path}")
    ui.console.print(f"version {state.user_version}, sqlite {dbmod.sqlite_runtime_version()},"
                     f" pending {len(state.pending)}, rows {counts}")


def _print_doctor(c: Ctx) -> None:
    problems = migrations.verify(c.conn)
    report = artifacts.doctor_report(c.layout, c.conn)
    if problems:
        for problem in problems:
            ui.console.print(f"[red]{problem}[/red]")
    else:
        ui.console.print("[green]integrity_check and foreign_key_check are clean[/green]")
    ui.render_table("Missing files", ("path",), [(p,) for p in report["missing_files"]],
                    empty="none")
    ui.render_table("Unregistered files", ("path",),
                    [(p,) for p in report["unregistered_files"]], empty="none")
    if report.get("untracked_folders"):
        ui.render_table("Files outside registered applications (left as they are)",
                        ("path",), [(p,) for p in report["untracked_folders"]])


def _menu(c: Ctx) -> None:
    ui.require_interactive()
    while True:
        try:
            # No default: the menu is a fresh list and its cursor belongs on the
            # first entry, "1. Add submitted application" (the entries carry their
            # own numbers, so moving one to the front is not an option).
            action = ui.choice(
                "Job tracker",
                [
                    ("add", "1. Add submitted application"),
                    ("log", "2. Log an update"),
                    ("browse", "3. Browse/search applications"),
                    ("posting", "4. Read a job posting"),
                    ("waiting", "5. Upcoming interviews / waiting applications"),
                    ("stats", "6. Statistics and export"),
                    ("db", "7. Database maintenance"),
                    ("exit", "8. Exit"),
                ],
            )
        except (Cancelled, KeyboardInterrupt):
            return
        if action == "exit":
            return
        try:
            {
                "add": _menu_add,
                "log": _menu_log,
                "browse": _menu_browse,
                "posting": _menu_posting,
                "waiting": _menu_waiting,
                "stats": _menu_stats,
                "db": _menu_db,
            }[action](c)
        except Cancelled as exc:
            detail = str(exc)
            ui.console.print("[yellow]cancelled[/yellow]"
                             + (f" {detail}" if detail and detail != "cancelled" else ""))
        except (KeyboardInterrupt, EOFError):
            ui.console.print("[yellow]cancelled[/yellow]")
        except JobError as exc:
            ui.console.print(f"[red]error:[/red] {exc}")


if __name__ == "__main__":  # pragma: no cover
    main()
