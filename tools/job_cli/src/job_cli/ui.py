"""Prompt helpers (questionary) and output rendering (rich)."""
from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence

import questionary
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import repo
from .db import all_rows, one
from .errors import Cancelled, UsageError

#: Style for every prompt.
#:
#: ``answer`` is the colour of the text the user types. The value is the shared
#: contract with the sibling tools (./posting and ./cv use the same rule in a
#: prompt_toolkit session), so it is written out here instead of being inherited
#: from questionary's DEFAULT_STYLE: the three tools must keep typed input
#: looking the same even if questionary changes its own default.
#:
#: The pointer symbol alone shows where the cursor is: prompt_toolkit's built-in
#: ``selected`` class (reverse video) would otherwise stay on the pre-selected
#: choice after the cursor has moved away from it, so the highlighted line and
#: the cursor would disagree. ``noreverse`` is explicit because an empty style
#: rule inherits instead of clearing.
PROMPT_STYLE = questionary.Style([
    ("answer", "fg:#FF9D00 bold"),
    ("selected", "noreverse"),
    ("highlighted", "noreverse"),
])

def _default_width() -> int | None:
    """Fixed wide width when output is piped or the terminal is dumb (TERM=dumb),
    so tables do not fold into unreadable 80-column columns."""
    override = os.environ.get("JOB_WIDTH")
    if override and override.isdigit():
        return int(override)
    if os.environ.get("TERM", "").lower() in ("dumb", "unknown"):
        return 120
    try:
        if sys.stdout.isatty():
            return None
    except (ValueError, AttributeError):  # pragma: no cover
        return None
    return 120


def _console_kwargs(stderr: bool = False) -> dict:
    width = _default_width()
    kwargs = {"stderr": stderr} if stderr else {}
    if width is not None:
        kwargs["width"] = width
        kwargs["height"] = 60
    return kwargs


console = Console(**_console_kwargs())
err_console = Console(**_console_kwargs(stderr=True))

STATUS_STYLE = {
    "in_progress": "cyan",
    "offer_pending": "bright_green",
    "accepted": "green",
    "started": "green",
    "rejected": "red",
    "withdrawn": "yellow",
    "offer_declined": "yellow",
    "position_closed": "grey50",
    "inactive": "magenta",
    "invited": "cyan",
    "scheduled": "bright_cyan",
    "completed": "green",
    "cancelled": "grey50",
    "planned": "white",
}


def note(message: str) -> None:
    """Progress/status notes go to stderr so --json stdout stays machine-readable."""
    err_console.print(message)


def status_text(status: str) -> str:
    style = STATUS_STYLE.get(status, "white")
    return f"[{style}]{status}[/{style}]"


def interactive_available() -> bool:
    try:
        return sys.stdin.isatty()
    except (ValueError, AttributeError):  # pragma: no cover - odd stdin objects
        return False


def require_interactive(what: str = "this command") -> None:
    if not interactive_available():
        raise UsageError(
            f"{what} needs an interactive terminal; pass the equivalent flags"
            " (see --help) to run it non-interactively"
        )


def _answer(value):
    if value is None:
        raise Cancelled("cancelled")
    return value


def text(message: str, *, default: str | None = None, allow_blank: bool = False,
         required_hint: str = "a value is required") -> str:
    def validator(value: str) -> bool | str:
        if value is None:
            return True
        if not value.strip() and not allow_blank:
            return required_hint
        return True

    answer = questionary.text(message, default=default or "", validate=validator,
                              style=PROMPT_STYLE).ask()
    return str(_answer(answer)).strip()


def optional_text(message: str, *, default: str | None = None) -> str | None:
    answer = questionary.text(message, default=default or "", style=PROMPT_STYLE).ask()
    value = str(_answer(answer)).strip()
    return value or None


def date(message: str, *, default: str | None = None) -> str:
    from .rules import parse_date

    def validator(value: str) -> bool | str:
        try:
            parse_date(value)
        except UsageError as exc:
            return str(exc)
        return True

    answer = questionary.text(message, default=default or "", validate=validator,
                              style=PROMPT_STYLE).ask()
    return str(_answer(answer)).strip()


def first_default(options: Sequence[tuple[str, str]],
                  value: str | None) -> list[tuple[str, str]]:
    """Move the recommended entry to the front of a choice list.

    ``questionary`` points the cursor at the ``default`` value, so a default that
    sits further down would leave the cursor on a later line of a fresh list.
    Rotating the recommended entry to the front keeps the recommendation (Enter
    still picks it) and keeps the cursor on the first entry, which is what a
    fresh list must do. A list whose first entry already matches, or that does
    not contain the value at all, is returned unchanged.
    """
    if not options or value is None or options[0][0] == value:
        return list(options)
    recommended = [option for option in options if option[0] == value]
    if not recommended:
        return list(options)
    return recommended + [option for option in options if option[0] != value]


#: The keys that confirm the first entries of a list directly, in order: the
#: digits 1..9 and then 0 for the tenth entry.
NUMBER_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")


def _bind_number_keys(question, values: Sequence[str]) -> None:
    """Let the number keys confirm an entry of ``question`` in one keypress.

    questionary builds the prompt first, so the binding is added afterwards and
    made eager to win over the prompt's catch-all key handler. Only the first
    ten entries are numbered; a longer list keeps its remaining entries on the
    arrow keys. A prompt object without an ``application`` (a test stand-in for
    questionary) is left alone.
    """
    application = getattr(question, "application", None)
    if application is None:  # pragma: no cover - only a fake prompt replaces select
        return
    for key, value in zip(NUMBER_KEYS, values):
        def confirm(event, value=value):
            event.app.exit(result=value)

        application.key_bindings.add(key, eager=True)(confirm)


def _select_question(message: str, options: Sequence[tuple[str, str]], *,
                     default: str | None = None):
    """The questionary select behind :func:`choice`, with the number keys bound."""
    choices = [questionary.Choice(title=label, value=value) for value, label in options]
    question = questionary.select(message, choices=choices, default=default,
                                  style=PROMPT_STYLE,
                                  instruction="(Use arrow keys or a number)")
    _bind_number_keys(question, [value for value, _ in options])
    return question


def choice(message: str, options: Sequence[tuple[str, str]], *,
           default: str | None = None) -> str:
    """Ask for one entry of ``options``.

    The cursor starts on the first entry: a fresh list must never open with the
    pointer somewhere else. Pass a recommendation through :func:`first_default`
    instead of letting ``default`` point at a later entry. ``default`` stays
    supported for entries that are already first.

    Pressing a number confirms that entry at once (1..9, then 0 for the tenth),
    so a short list needs neither the arrow keys nor Enter; a longer list keeps
    its remaining entries on the arrow keys.
    """
    return str(_answer(_select_question(message, options, default=default).ask()))


def checkbox(message: str, options: Sequence[tuple[str, str]],
             default: Iterable[str] = ()) -> list[str]:
    defaults = set(default)
    choices = [
        questionary.Choice(title=label, value=value, checked=value in defaults)
        for value, label in options
    ]
    answer = questionary.checkbox(message, choices=choices, style=PROMPT_STYLE).ask()
    return list(_answer(answer))


def confirm(message: str, *, default: bool = False) -> bool:
    answer = questionary.confirm(message, default=default, style=PROMPT_STYLE).ask()
    return bool(_answer(answer))


def labelled_lookups(rows: Sequence[object]) -> list[tuple[str, str]]:
    return [(str(row["code"]), str(row["label"])) for row in rows]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def print_stored_text(text: str, *, wrap: bool = True) -> None:
    """Print stored text verbatim.

    Rich markup and rich's own highlighting stay off: a captured job posting is
    arbitrary text, so ``[bold]``-like sequences must appear as typed and no
    number or URL in it may be recoloured. With ``wrap=False`` the lines are
    printed exactly as stored, which keeps a pager or a redirect faithful.
    """
    console.print(Text(text), highlight=False, soft_wrap=not wrap)


def render_table(title: str, columns: Sequence[str], rows: Sequence[Sequence[object]],
                 *, empty: str | None = None) -> None:
    if not rows:
        if empty:
            console.print(f"[bold]{title}[/bold]: [green]{empty}[/green]")
        return
    table = Table(title=title, header_style="bold")
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


#: The two in_progress states of the overview table. "answered" means at least one
#: employer-origin event exists; "waiting" means no employer answered yet. The
#: STATUS_STYLE colour of in_progress (cyan) stays the one every other view uses.
ANSWERED_STYLE = "bold bright_green"
WAITING_STYLE = "yellow"
ANSWERED_MARKER = "\u25cf"   # they replied
WAITING_MARKER = "\u25cb"    # still quiet


def _status_of(row) -> str:
    """The status of a status/overview row: views call it current_status, the
    snapshot query calls it status."""
    keys = row.keys()
    return str(row["current_status"] if "current_status" in keys else row["status"])


def _app_id_of(row) -> str:
    keys = row.keys()
    return str(row["id"] if "id" in keys else row["application_id"])


def _overview_order(rows: Sequence[object]) -> list:
    """Rejected applications last, everything else where it was.

    The sort is stable, so the order inside both groups stays the query's order
    (last activity desc, submitted desc, company) and the accepted, offer and
    withdrawn rows keep the place they have today. Only the printed table is
    sorted: the SQL and the --json order are untouched.
    """
    return sorted(rows, key=lambda row: _status_of(row) == "rejected")


def _status_cell(status: str, answered_in_progress: bool) -> str:
    if status == "in_progress":
        if answered_in_progress:
            return f"[{ANSWERED_STYLE}]{status} {ANSWERED_MARKER}[/{ANSWERED_STYLE}]"
        return f"[{WAITING_STYLE}]{status} {WAITING_MARKER}[/{WAITING_STYLE}]"
    return status_text(status)


def _dim_cell(value) -> Text:
    """A cell of a faded (rejected) row. The status cell keeps its own colour."""
    return Text(str(value), style="dim")


def render_overview_summary(summary: dict, *, shown: int | None = None) -> None:
    """The counter line under a printed application table.

    Total applications tracked, one count per status, and the in_progress split
    into answered (an employer event exists) and waiting (no response yet). The
    counters come from reporting and describe the whole tracker, so they keep
    answering "how many do I have" under a filtered table. ``shown`` is the
    number of rows that table actually printed: it is added at the end only when
    it differs, so an unfiltered table keeps its single clean line.
    """
    parts: list[str] = []
    for status, count in summary["status_counts"].items():
        if status == "in_progress":
            parts.append(
                f"{status_text(status)} {count} ([{ANSWERED_STYLE}]{summary['answered']}"
                f" answered[/{ANSWERED_STYLE}], [{WAITING_STYLE}]{summary['waiting']}"
                f" waiting[/{WAITING_STYLE}])"
            )
        else:
            parts.append(f"{status_text(status)} {count}")
    total = summary["total"]
    line = f"[bold]{total} application{'' if total == 1 else 's'} tracked[/bold]"
    if parts:
        line += " \u00b7 " + " \u00b7 ".join(parts)
    if shown is not None and shown != total:
        line += f" - showing {shown}"
    # highlight off: rich would otherwise recolour every bare number, which hides
    # the two colours that carry the meaning here.
    console.print(line, highlight=False)


def render_applications(rows: Sequence[object], *, title: str = "Applications",
                        summary: dict | None = None) -> None:
    """The application overview table plus its legend and counters.

    ``summary`` is the counter block from ``reporting.overview_summary`` for
    exactly these rows; it also tells the table which in_progress applications
    already have an employer response. Pass it wherever a table is printed for a
    human reader (the human path only: --json output never reaches this).
    """
    rows = _overview_order(rows)
    answered_ids = frozenset() if summary is None else summary["answered_ids"]
    table = Table(title=title, header_style="bold")
    for column in ("id", "company", "role", "status", "submitted", "interviews",
                   "last activity", "next appointment"):
        table.add_column(column, overflow="fold")
    for row in rows:
        status = _status_of(row)
        # A rejected row stays complete and readable, but its text fades so the
        # open applications keep the foreground; its status cell stays red.
        cell = _dim_cell if status == "rejected" else str
        table.add_row(
            repo.short_id(_app_id_of(row)),
            cell(row["company"]),
            cell(row["role"]),
            _status_cell(status, _app_id_of(row) in answered_ids),
            cell(row["submitted_on"]),
            cell(row["completed_interviews"]),
            cell(row["last_activity_on"]),
            cell(row["next_appointment"] or "-"),
        )
    console.print(table)
    if any(_status_of(row) == "in_progress" for row in rows):
        console.print(
            f"[dim]legend:[/dim] [{ANSWERED_STYLE}]{ANSWERED_MARKER}[/{ANSWERED_STYLE}]"
            " [dim]answered by the employer[/dim]"
            f" \u00b7 [{WAITING_STYLE}]{WAITING_MARKER}[/{WAITING_STYLE}]"
            " [dim]still waiting[/dim]"
        )
    if summary is not None:
        render_overview_summary(summary, shown=len(rows))
    console.print("[dim]Use the short id or a company/role name with show/edit/log.[/dim]")


def render_timeline(rows: Sequence[object], *, title: str = "Timeline") -> None:
    table = Table(title=title, header_style="bold")
    for column in ("date", "order", "event", "round", "schedule", "notes"):
        table.add_column(column, overflow="fold")
    for row in rows:
        schedule = ""
        if row["scheduled_for"]:
            schedule = str(row["scheduled_for"])
            if row["scheduled_time"]:
                schedule += f" {row['scheduled_time']} {row['timezone'] or ''}"
        table.add_row(
            str(row["occurred_on"]),
            str(row["day_order"]),
            str(row["event_type"]),
            "" if row["round_id"] is None else f"#{row['round_id']}",
            schedule,
            (row["notes"] or "")[:60],
        )
    console.print(table)


def render_rounds(rows: Sequence[object],
                 participants: dict[int, list[str]] | None = None) -> None:
    participants = participants or {}
    table = Table(title="Rounds", header_style="bold")
    for column in ("id", "seq", "phase", "type", "format", "state", "appointment", "participants"):
        table.add_column(column, overflow="fold")
    for row in rows:
        appointment = str(row["scheduled_for"] or "-")
        if row["scheduled_time"]:
            appointment += f" {row['scheduled_time']} {row['timezone'] or ''}"
        table.add_row(
            str(row["id"]), str(row["sequence_no"]), str(row["phase"]), str(row["round_type"]),
            str(row["format"]), status_text(str(row["current_status"])), appointment,
            ", ".join(participants.get(int(row["id"]), [])),
        )
    console.print(table)


def render_stats(stats: dict) -> None:
    filters = stats["filters"]
    console.print(f"[bold]Statistics[/bold] (as of {filters['as_of']},"
                  f" cohort {filters['cohort_from'] or 'start'} .. {filters['cohort_to'] or 'now'})")
    table = Table(header_style="bold")
    table.add_column("status")
    table.add_column("applications", justify="right")
    for status, count in stats["status_counts"].items():
        table.add_row(status_text(status), str(count))
    table.add_row("[bold]total[/bold]", f"[bold]{stats['total_applications']}[/bold]")
    console.print(table)
    rows = [
        ("completed interviews", stats["completed_interviews"]),
        ("completed screens", stats["completed_screens"]),
        ("completed assessments", stats["completed_assessments"]),
        ("applications with >= 1 interview", stats["applications_with_interview"]),
        ("applications with an offer", stats["applications_with_offer"]),
        ("still open (in progress or offer pending)", stats["still_open"]),
    ]
    render_table("Counts", ("metric", "value"), rows)
    responses = stats["responses"]
    render_table(
        "First employer response (excludes your follow-ups, notes, applicant actions, and"
        " inactive markers)",
        ("metric", "value"),
        [
            ("applications answered", responses["applications_answered"]),
            ("applications without employer event", responses["applications_without_employer_event"]),
            ("median days to first response", responses["first_response_median_days"]),
            ("mean days to first response", responses["first_response_mean_days"]),
        ],
    )
    duration = stats["duration"]
    render_table(
        "Process duration",
        ("metric", "value"),
        [
            ("applications with a terminal state", duration["applications_with_terminal_state"]),
            ("median days to terminal state", duration["median_days_to_terminal_state"]),
            ("mean days to terminal state", duration["mean_days_to_terminal_state"]),
        ],
    )
    console.print(f"[dim]{stats['denominators']['still_in_progress_note']};"
                  " percentages use total applications as the denominator.[/dim]")


def next_day_order(conn, app_id: str, occurred_on: str) -> int:
    row = one(conn, "SELECT coalesce(max(day_order), 0) + 1 AS next FROM events"
                    " WHERE application_id = ? AND occurred_on = ?", (app_id, occurred_on))
    return int(row["next"])
