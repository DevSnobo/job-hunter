"""Interactive behaviour under a real pseudo-terminal.

Covers the review's F2 requirement: every destructive confirmation must honour a
"No" answer, tested through the installed console script and a genuine pty (not
mocked prompts). Also covers the F11 add-review screen and filter validation.
"""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import time
from pathlib import Path

import pytest

from helpers import make_app, payload

REAL_ROOT = Path(__file__).resolve().parents[3]  # the repository root (job-hunter)
CONSOLE_SCRIPT = REAL_ROOT / ".venv" / "bin" / "job"

pytestmark = pytest.mark.skipif(not CONSOLE_SCRIPT.exists(),
                               reason="console script not installed")


class _PtySession:
    """A CLI process attached to a pseudo-terminal, driven prompt by prompt."""

    def __init__(self, project_root: Path, db: Path, args: list[str]):
        self.master, slave = pty.openpty()
        env = dict(os.environ)
        env.update({"TERM": "xterm", "JOB_ROOT": str(project_root), "JOB_DB": str(db),
                    "COLUMNS": "120", "LINES": "45"})
        self.process = subprocess.Popen(
            [str(CONSOLE_SCRIPT), *[str(a) for a in args]],
            stdin=slave, stdout=slave, stderr=slave, env=env, cwd="/",
            preexec_fn=os.setsid, close_fds=True,
        )
        os.close(slave)
        self.buffer = ""
        self.position = 0
        self.closed = False

    def _read(self) -> bool:
        ready, _, _ = select.select([self.master], [], [], 0.2)
        if not ready:
            return True
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return False
        if not data:
            return False
        self.buffer += data.decode("utf-8", "replace")
        return True

    def expect(self, sentinel: str, keys: bytes = b"", timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._read():
                break
            if sentinel in self.buffer[self.position:]:
                self.position = len(self.buffer)
                self._send(keys)
                return True
            if self.process.poll() is not None:
                break
        self._send(keys)
        return False

    def settle(self, idle: float = 0.7, max_time: float = 20.0) -> None:
        """Read until the process has been quiet for `idle` seconds."""
        deadline = time.time() + max_time
        quiet_since = time.time()
        while time.time() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                if time.time() - quiet_since >= idle:
                    return
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            self.buffer += data.decode("utf-8", "replace")
            quiet_since = time.time()
            self.position = len(self.buffer)

    def _send(self, keys: bytes) -> None:
        if not keys:
            return
        try:
            os.write(self.master, keys)
        except OSError:  # the process already exited
            pass

    def finish(self, timeout: float = 20.0) -> tuple[str, int]:
        deadline = time.time() + timeout
        while time.time() < deadline and self.process.poll() is None:
            if not self._read():
                break
        try:
            code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - failure path
            self.close()
            code = -9
        while self._read():  # drain whatever is left
            pass
        self.close()
        return self.buffer, code

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except OSError:  # pragma: no cover - defensive
                pass
            self.process.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:  # pragma: no cover - defensive
            pass


def _run_interactive(project_root: Path, db: Path, args: list[str],
                     steps: list[tuple[str, bytes]], timeout: float = 40.0) -> tuple[str, int]:
    session = _PtySession(project_root, db, args)
    try:
        for sentinel, keys in steps:
            assert session.expect(sentinel, keys), (
                f"never saw {sentinel!r}; output so far:\n{session.buffer}"
            )
        return session.finish(timeout)
    finally:
        session.close()


def _run_menu(project_root: Path, db: Path, steps, timeout: float = 40.0):
    return _run_interactive(project_root, db, [], steps, timeout)


# --------------------------------------------------------------------------
# menu basics
# --------------------------------------------------------------------------
def test_menu_exits_cleanly(project_root, tmp_path):
    db = tmp_path / "menu.sqlite3"
    # seven "down" keys from the first entry ("1. Add submitted application", where every
    # fresh option list starts) to "8. Exit"
    output, code = _run_menu(project_root, db, [("Job tracker", b"\x1b[B" * 7 + b"\r")])
    assert "Job tracker" in output
    assert "Add submitted application" in output
    assert "8. Exit" in output
    assert "Traceback" not in output
    assert code == 0


def test_ctrl_c_at_the_menu_leaves_no_traceback_and_no_data(project_root, tmp_path):
    db = tmp_path / "menu2.sqlite3"
    output, code = _run_menu(project_root, db, [("Job tracker", b"\x03")])
    assert "Traceback" not in output
    assert code in (0, 1, 130)
    if db.exists():
        from job_cli import db as dbmod
        conn = dbmod.connect(db, read_only=True)
        try:
            assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
        finally:
            conn.close()


# --------------------------------------------------------------------------
# F2: every destructive confirmation must honour "No"
# --------------------------------------------------------------------------
def _seed_note(run, company="Acme GmbH"):
    """One application with one note event."""
    app_id = make_app(run, company, "Backend Engineer", "2026-01-05")
    assert run("log", company, "--event", "note", "--date", "2026-01-06",
               "--notes", "keep me").ok
    detail = payload(run, "show", app_id, "--json")
    event_id = detail["timeline"][-1]["event_id"]
    return app_id, int(event_id)


def _event_types(run, app_id):
    return [event["event_type"] for event in payload(run, "show", app_id, "--json")["timeline"]]


def test_delete_event_no_keeps_the_row(run, project_root):
    app_id, event_id = _seed_note(run)
    output, code = _run_interactive(
        project_root, run.db, ["delete", "event", str(event_id)],
        [("Delete this event?", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    assert "deleted" not in output.split("Cancelled")[0]
    assert _event_types(run, app_id) == ["submitted", "note"]


def test_delete_round_no_keeps_the_round_and_its_events(run, project_root):
    app_id = _seed_note(run)[0]
    assert run("log", "Acme GmbH", "--event", "invited", "--date", "2026-01-07",
               "--round", "new", "--phase", "interview", "--round-type", "general").ok
    round_id = payload(run, "show", app_id, "--json")["rounds"][0]["id"]
    output, code = _run_interactive(
        project_root, run.db, ["delete", "round", str(round_id)],
        [("Delete this round and its events?", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    detail = payload(run, "show", app_id, "--json")
    assert [row["id"] for row in detail["rounds"]] == [round_id]
    assert _event_types(run, app_id) == ["submitted", "note", "invited"]


def test_delete_application_no_keeps_the_row_and_the_files(run, project_root, tmp_path):
    app_id, _ = _seed_note(run)
    source = tmp_path / "cv.txt"
    source.write_text("keep me")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    stored = project_root / "applications" / app_id / "cv.txt"
    assert stored.read_text() == "keep me"

    output, code = _run_interactive(
        project_root, run.db, ["delete", "application", app_id, "--remove-files"],
        [("Delete this application?", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    assert stored.read_text() == "keep me"
    assert any(row["id"] == app_id for row in payload(run, "list", "--json"))


def test_remove_files_second_no_keeps_the_folder(run, project_root, tmp_path):
    app_id, _ = _seed_note(run)
    source = tmp_path / "cv.txt"
    source.write_text("keep me")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    stored = project_root / "applications" / app_id / "cv.txt"

    output, code = _run_interactive(
        project_root, run.db, ["delete", "application", app_id, "--remove-files"],
        [("Delete this application?", b"y"), ("Also delete the folder", b"n")],
    )
    assert code == 0
    assert "folder kept" in output
    assert stored.read_text() == "keep me"
    assert not any(row["id"] == app_id for row in payload(run, "list", "--json"))


def test_remove_files_confirmed_removes_the_folder(run, project_root, tmp_path):
    app_id, _ = _seed_note(run)
    source = tmp_path / "cv.txt"
    source.write_text("delete me")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    folder = project_root / "applications" / app_id

    output, code = _run_interactive(
        project_root, run.db, ["delete", "application", app_id, "--remove-files"],
        [("Delete this application?", b"y"), ("Also delete the folder", b"y")],
    )
    assert code == 0
    assert not folder.exists()


def test_undo_no_keeps_the_latest_event(run, project_root):
    app_id, _ = _seed_note(run)
    output, code = _run_interactive(
        project_root, run.db, ["undo", app_id], [("Delete this event?", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    assert _event_types(run, app_id) == ["submitted", "note"]


def test_db_restore_no_keeps_the_database(run, project_root, tmp_path):
    app_id, _ = _seed_note(run)
    assert run("db", "backup", "--label", "safety").ok
    backups = sorted((project_root / "data" / "backups").glob("safety-*.sqlite3"))
    assert backups
    output, code = _run_interactive(
        project_root, run.db, ["db", "restore", str(backups[-1])],
        [("Replace ", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    assert _event_types(run, app_id) == ["submitted", "note"]


def test_remove_files_answer_no_keeps_everything_for_application(run, project_root, tmp_path):
    """The review's exact repro: No to the application and to the folder prompt."""
    app_id, _ = _seed_note(run)
    source = tmp_path / "cv.txt"
    source.write_text("keep me")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    folder = project_root / "applications" / app_id

    output, code = _run_interactive(
        project_root, run.db, ["delete", "application", app_id, "--remove-files"],
        [("Delete this application?", b"n")],
    )
    assert code == 130
    assert folder.is_dir()
    assert any(row["id"] == app_id for row in payload(run, "list", "--json"))


def test_interactive_state_regression_confirmation_is_honoured(run, project_root):
    """F6: declining the regression prompt must leave offer_pending untouched."""
    app_id = make_app(run, "Acme GmbH", "Backend Engineer", "2026-01-05")
    assert run("log", "Acme GmbH", "--event", "offer_received", "--date", "2026-01-10").ok

    output, code = _run_interactive(
        project_root, run.db,
        ["log", "Acme GmbH", "--event", "completed", "--date", "2026-01-11",
         "--round", "new", "--phase", "interview", "--round-type", "general",
         "--format", "video"],
        [("Participants", b"\r"), ("move the application back", b"n")],
    )
    assert code == 130
    assert "Cancelled" in output
    detail = payload(run, "show", app_id, "--json")
    assert detail["application"]["current_status"] == "offer_pending"
    assert detail["rounds"] == []


# --------------------------------------------------------------------------
# F11: interactive add review and filter validation
# --------------------------------------------------------------------------
def test_interactive_add_reviews_before_saving(run, project_root):
    output, code = _run_interactive(
        project_root, run.db, ["add"],
        [
            ("Company", b"Guided GmbH\r"),
            ("Role", b"Backend Engineer\r"),
            ("Submission date", b"\r"),
            ("Add details now", b"n"),
            ("Save this application?", b"y"),
            ("Next step", b"\x1b[B" * 4 + b"\r"),  # "Back to the menu"
        ],
    )
    assert code == 0, output
    assert "Review the new application" in output
    assert "saved" in output
    companies = [row["company"] for row in payload(run, "list", "--json")]
    assert "Guided GmbH" in companies


def test_interactive_add_review_can_be_declined(run, project_root):
    output, code = _run_interactive(
        project_root, run.db, ["add"],
        [
            ("Company", b"Declined GmbH\r"),
            ("Role", b"Backend Engineer\r"),
            ("Submission date", b"\r"),
            ("Add details now", b"n"),
            ("Save this application?", b"n"),
        ],
    )
    assert code == 130
    assert "Cancelled" in output
    assert payload(run, "list", "--json") == []


def test_interactive_add_can_start_from_an_intake_folder(run, project_root):
    """The folder question supplies the company name and the job url from source.txt."""
    folder = project_root / "applications" / "Globex_155461"
    folder.mkdir(parents=True)
    (folder / "posting.txt").write_text("Details zum Jobangebot\nJava Developer\n")
    (folder / "source.txt").write_text("https://www.linkedin.com/jobs/view/4471519418/\n")
    output, code = _run_interactive(
        project_root, run.db, ["add"],
        [
            ("Start from an intake folder", b"y"),
            ("Which folder?", b"\r"),
            ("Company (from the folder identifier)", b"\r"),
            ("Role", b"Java Developer\r"),
            ("Submission date", b"\r"),
            ("Add details now", b"n"),
            ("Save this application?", b"y"),
            ("Next step", b"\x1b[B" * 4 + b"\r"),  # "Back to the menu"
        ],
    )
    assert code == 0, output
    assert "Globex_155461" in output
    listed = payload(run, "list", "--json")
    assert [row["company"] for row in listed] == ["Globex"]
    detail = payload(run, "show", listed[0]["id"], "--json")["application"]
    assert detail["role"] == "Java Developer"
    assert detail["intake_folder"] == "Globex_155461"
    assert detail["job_url"] == "https://www.linkedin.com/jobs/view/4471519418/"


def test_interactive_statistics_rejects_a_bad_date(run, project_root):
    """Menu navigation is driven by output settling, not by fragile prompt matching."""
    _seed_note(run)
    session = _PtySession(project_root, run.db, [])
    try:
        session.settle()
        session._send(b"\x1b[B" * 5 + b"\r")   # "6. Statistics and export" (from entry 1)
        session.settle()
        session._send(b"\x1b[B\r")             # "Export aggregate Sankey CSV"
        session.settle()
        session._send(b"not-a-date\r")          # must be rejected and asked again
        session.settle()
        session._send(b"\x03")                  # leave the prompt, back to the menu
        session.settle()
        session._send(b"\x1b[B" * 7 + b"\r")   # "8. Exit" (from entry 1)
        output, code = session.finish()
    finally:
        session.close()
    assert "Cutoff date" in output
    assert "is not a valid calendar date" in output
    assert "Traceback" not in output
    assert code == 0


# --------------------------------------------------------------------------
# the posting is reachable from the menu, and the cursor is the only marker
# --------------------------------------------------------------------------
def _seed_intake_folder(project_root: Path, name: str = "Globex_155461") -> Path:
    folder = project_root / "applications" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "posting.txt").write_text("Stellen-ID: 155461\nBackend mit Java und Go.\n",
                                        encoding="utf-8")
    (folder / "source.txt").write_text("https://example.com/jobs/155461\n", encoding="utf-8")
    return folder


def test_the_menu_reads_a_stored_posting_again(run, project_root):
    """Menu entry 4 finds the application and prints the captured text."""
    _seed_intake_folder(project_root)
    make_app(run, "Globex", "Backend Entwickler", "2026-10-05",
             "--from-folder", "Globex_155461")
    session = _PtySession(project_root, run.db, [])
    try:
        session.settle()
        session._send(b"\x1b[B" * 3 + b"\r")   # "4. Read a job posting" (from entry 1)
        session.settle()
        session._send(b"Globex\r")               # search the application
        session.settle()
        session._send(b"\r")                       # take the only match
        session.settle()
        session._send(b"\x03")
        session.settle()
        session._send(b"\x1b[B" * 7 + b"\r")      # "8. Exit" (from entry 1)
        output, code = session.finish()
    finally:
        session.close()
    assert "Backend mit Java und Go." in output
    assert "applications/Globex_155461/posting.txt" in output
    assert "Traceback" not in output
    assert code == 0


def test_the_menu_cursor_starts_on_the_first_entry(run, project_root):
    """A fresh option list opens with the pointer on its first line.

    The menu used to pre-point at "2. Log an update" as soon as the database had
    any data, so the pointer started in the middle of the list.
    """
    _seed_note(run)
    session = _PtySession(project_root, run.db, [])
    try:
        session.settle()
        assert "\u00bb 1. Add submitted application" in session.buffer
        assert "\u00bb 2. Log an update" not in session.buffer
        session._send(b"\x1b[B")   # the arrow keys still move the pointer
        session.settle()
        assert "\u00bb 2. Log an update" in session.buffer
        session._send(b"\x03")
        output, code = session.finish()
    finally:
        session.close()
    assert "Traceback" not in output
    assert code == 0


def test_the_menu_shows_the_cursor_without_a_text_highlight(run, project_root):
    """The pre-selected entry must not stay reverse-highlighted.

    prompt_toolkit's ``selected`` class is reverse video, so every choice that
    questionary marks as pre-selected used to be drawn inverted for the whole
    prompt, which disagreed with the cursor.
    """
    _seed_note(run)
    session = _PtySession(project_root, run.db, [])
    try:
        session.settle()
        session._send(b"\x1b[B")   # the old highlight stayed on the default entry
        session.settle()
        session._send(b"\x03")
        session.settle()
        session._send(b"\x1b[B" * 7 + b"\r")   # "8. Exit" (from entry 1)
        output, code = session.finish()
    finally:
        session.close()
    assert "1. Add submitted application" in output
    assert "2. Log an update" in output
    assert not re.search(r"\x1b\[[0-9;]*7m", output), "a menu entry was drawn in reverse video"
    assert code == 0


# --------------------------------------------------------------------------
# every prompt is styled so that only the pointer marks the cursor
# --------------------------------------------------------------------------
def test_every_prompt_uses_the_neutral_cursor_style(monkeypatch):
    import questionary

    from job_cli import ui

    captured: list[tuple[str, object]] = []

    class FakeQuestion:
        def __init__(self, answer):
            self.answer = answer

        def ask(self):
            return self.answer

    def fake(kind: str, answer):
        def _fake(message, *args, **kwargs):
            captured.append((kind, kwargs.get("style")))
            return FakeQuestion(answer)
        return _fake

    monkeypatch.setattr(questionary, "text", fake("text", "value"))
    monkeypatch.setattr(questionary, "select", fake("select", "value"))
    monkeypatch.setattr(questionary, "checkbox", fake("checkbox", ["value"]))
    monkeypatch.setattr(questionary, "confirm", fake("confirm", True))

    ui.text("m", default="d")
    ui.optional_text("m")
    ui.date("m", default="2026-01-01")
    ui.choice("m", [("value", "Value"), ("other", "Other")])
    ui.checkbox("m", [("value", "Value")], default=("value",))
    ui.confirm("m")

    assert [kind for kind, _style in captured] == [
        "text", "text", "text", "select", "checkbox", "confirm"]
    assert all(style is ui.PROMPT_STYLE for _kind, style in captured)


def test_the_prompt_style_clears_prompt_toolkits_reverse_selected_class():
    """An empty style rule inherits, so the clearing rule has to name noreverse."""
    import questionary

    from job_cli import ui

    question = questionary.select(
        "Menu",
        choices=[questionary.Choice(title="one", value="one"),
                 questionary.Choice(title="two", value="two")],
        default="two",
        style=ui.PROMPT_STYLE,
    )
    merged = question.application._merged_style
    for class_name in ("class:selected", "class:highlighted"):
        assert merged.get_attrs_for_style_str(class_name).reverse is False


def test_the_prompt_style_pins_the_typed_input_colour():
    """The text the user types keeps the colour the sibling tools use for it.

    ./posting and ./cv style their prompt the same way, so the rule is written
    out here instead of being inherited from questionary's DEFAULT_STYLE: the
    three tools must stay consistent even if questionary changes its default.
    """
    import questionary

    from job_cli import ui

    assert ("answer", "fg:#FF9D00 bold") in ui.PROMPT_STYLE.style_rules
    merged = questionary.text("Answer", style=ui.PROMPT_STYLE).application._merged_style
    attrs = merged.get_attrs_for_style_str("class:answer")
    assert attrs.color == "FF9D00" and attrs.bold is True


def test_the_add_flow_can_read_the_posting_immediately(run, project_root):
    """"Read the job posting" is one of the next steps after saving."""
    output, code = _run_interactive(
        project_root, run.db, ["add"],
        [
            ("Company", b"Guided GmbH\r"),
            ("Role", b"Backend Engineer\r"),
            ("Submission date", b"\r"),
            ("Add details now", b"n"),
            ("Save this application?", b"y"),
            ("Next step", b"\x1b[B\r"),   # "Read the job posting"
        ],
    )
    assert code == 0, output
    assert "no stored posting text" in output
