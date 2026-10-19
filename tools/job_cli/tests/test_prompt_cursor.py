"""Every option list opens with the cursor on its first entry.

questionary moves the cursor to the ``default`` value, so a call site that
pre-points at a later entry opens a fresh list with the pointer in the middle:
the main menu used to pre-point at "2. Log an update" as soon as the database had
any data. These tests drive the real prompt sites with a recording stand-in for
ui.choice and check the invariant on every captured call.
"""
from __future__ import annotations

from helpers import make_app

from job_cli import cli, db as dbmod, repo, rules, ui
from job_cli.paths import build_layout

INTAKE = "Sample_4711"


class Recorder:
    """A stand-in for ui.choice: it records the call and answers from a script."""

    #: A prompt whose message is not scripted may be answered with its first
    #: entry, but only a few times: an unknown prompt must fail the test instead
    #: of driving the menu in a loop forever (a wrong prompt *key* used to hang
    #: the suite, because "add" was always answered).
    MAX_UNSCRIPTED_ANSWERS = 20

    def __init__(self, answers: dict[str, list[str]] | None = None):
        self.calls: list[tuple[str, list[tuple[str, str]], str | None]] = []
        self.answers = {message: list(values) for message, values in (answers or {}).items()}
        self.unscripted = 0

    def choice(self, message, options, *, default=None, back=False):
        self.calls.append((message, list(options), default))
        queued = self.answers.get(message)
        if queued:
            return queued.pop(0)
        # The default answer is the first entry: pressing Enter on a fresh list.
        self.unscripted += 1
        assert self.unscripted <= self.MAX_UNSCRIPTED_ANSWERS, (
            f"prompt {message!r} is not scripted and keeps being asked "
            f"({self.unscripted} unscripted answers); scripted prompts: "
            f"{sorted(self.answers)}"
        )
        return options[0][0]

    @property
    def messages(self) -> list[str]:
        return [message for message, _options, _default in self.calls]

    def assert_every_list_starts_on_its_first_entry(self) -> None:
        for message, options, default in self.calls:
            assert default is None or default == options[0][0], (
                f"{message!r} pre-points at {default!r} instead of the first entry"
                f" {options[0][0]!r}"
            )


def _stub_prompts(monkeypatch, answers: dict[str, list[str]] | None = None) -> Recorder:
    """Replace every prompt with a scripted stand-in; only ui.choice is recorded."""
    recorder = Recorder(answers)
    monkeypatch.setattr(ui, "choice", recorder.choice)
    monkeypatch.setattr(ui, "text", lambda message, *, default=None, allow_blank=False,
                        required_hint="a value is required", back=False: str(default or "Typed"))
    monkeypatch.setattr(ui, "optional_text", lambda message, *, default=None, back=False: default)
    monkeypatch.setattr(ui, "date", lambda message, *, default=None,
                        back=False: default or "2026-01-05")
    monkeypatch.setattr(ui, "confirm", lambda message, *, default=False, back=False: True)
    monkeypatch.setattr(ui, "checkbox", lambda message, options, default=(), back=False: [])
    monkeypatch.setattr(ui, "require_interactive", lambda what="this command": None)
    monkeypatch.setattr(ui, "interactive_available", lambda: True)
    return recorder


def _intake_folder(project_root):
    folder = project_root / "applications" / INTAKE
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "posting.txt").write_text("Stellen-ID: 4711\nBackend.\n", encoding="utf-8")
    (folder / "source.txt").write_text("https://example.com/jobs/4711\n", encoding="utf-8")
    return folder


def _ctx(run) -> cli.Ctx:
    layout = build_layout(str(run.db), str(run.root))
    return cli.Ctx(layout, dbmod.connect(layout.db_path, must_exist=True))


def test_the_main_menu_starts_on_its_first_entry(monkeypatch, run):
    """The reported bug: with any data the menu opened on "2. Log an update"."""
    make_app(run, "Acme GmbH", "Backend", "2026-01-05")
    recorder = _stub_prompts(monkeypatch, {
        "Job tracker": ["stats", "db", "exit"],
        "Statistics and export": ["back"],
        "Database maintenance": ["back"],
    })
    result = run()
    assert result.ok, result.output
    assert recorder.messages.count("Job tracker") == 3  # stats, db, exit
    assert [default for message, _options, default in recorder.calls
            if message == "Job tracker"] == [None, None, None]
    recorder.assert_every_list_starts_on_its_first_entry()


def test_the_add_flow_starts_every_list_on_its_first_entry(monkeypatch, run, project_root):
    _intake_folder(project_root)
    recorder = _stub_prompts(monkeypatch, {"What happened?": ["invited"]})
    result = run("add")
    assert result.ok, result.output
    for message in ("Which folder?", "Discovery source", "Submission channel",
                    "Recruiter involvement", "Next step", "What happened?", "Which round?",
                    "Round type", "Phase", "Format"):
        assert message in recorder.messages, f"the {message!r} prompt was never reached"
    recorder.assert_every_list_starts_on_its_first_entry()


def test_edit_and_attach_start_every_list_on_its_first_entry(monkeypatch, run, tmp_path):
    app_id = make_app(run, "Acme GmbH", "Backend", "2026-01-05")
    recorder = _stub_prompts(monkeypatch, {"Which attribute?": ["discovery_source"]})
    assert run("edit", app_id).ok
    assert "New value for discovery_source" in recorder.messages

    document = tmp_path / "cv.txt"
    document.write_text("cv", encoding="utf-8")
    c = _ctx(run)
    try:
        row = repo.get_application(c.conn, app_id)
        cli._attach_flow(c, row, path=str(document))
    finally:
        c.conn.close()
    assert "Artifact type" in recorder.messages
    recorder.assert_every_list_starts_on_its_first_entry()


def test_the_round_and_remediate_pickers_start_on_their_first_entry(monkeypatch, run):
    app_id = make_app(run, "Acme GmbH", "Backend", "2026-01-05")
    for day in ("2026-01-06", "2026-01-09"):
        assert run("log", "Acme GmbH", "--event", "invited", "--date", day, "--round", "new",
                   "--phase", "interview", "--round-type", "general", "--format", "video").ok
    recorder = _stub_prompts(monkeypatch)
    c = _ctx(run)
    try:
        app = repo.get_application(c.conn, app_id)
        chosen = cli._choose_round(c, app, "invited", None)
        assert chosen in [int(row["id"]) for row in repo.rounds_for(c.conn, app_id)]
        assert cli._choose_round(c, app, "feedback_received", None) is None
        cli._remediate(c, app_id, "invited", "2026-01-10", None,
                       [rules.Issue("error", "already_completed", "that round is completed")])
    finally:
        c.conn.close()
    assert "Which round?" in recorder.messages
    assert "Attach to which round?" in recorder.messages
    recorder.assert_every_list_starts_on_its_first_entry()


def test_first_default_rotates_only_when_the_value_is_later(monkeypatch):
    """The helper behind the rotated lists: free of surprises on odd input."""
    options = [("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")]
    assert list(ui.first_default(options, "c")) == [
        ("c", "Charlie"), ("a", "Alpha"), ("b", "Bravo")]
    assert list(ui.first_default(options, "a")) == options
    assert list(ui.first_default(options, None)) == options
    assert list(ui.first_default(options, "missing")) == options
    assert list(ui.first_default([], "a")) == []
