"""Reading a stored job posting again: `job posting`, `show`, and the menu.

The text is a snapshot: whatever ./posting captured into posting.txt (or a file
attached as a job_posting artifact). Nothing here touches the network.
"""
from __future__ import annotations

from pathlib import Path

from helpers import make_app, payload

POSTING = (
    "Details zum Jobangebot\n"
    "Stellen-ID: 155461\n"
    "Backend mit Java und Go.\n"
)
OTHER = "Eine andere Stellenanzeige.\n"
URL = "https://example.com/jobs/155461"


def _intake_folder(project_root: Path, name: str = "Globex_155461",
                   text: str = POSTING, url: str = URL) -> Path:
    folder = project_root / "applications" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "posting.txt").write_text(text, encoding="utf-8")
    (folder / "source.txt").write_text(url + "\n", encoding="utf-8")
    return folder


def _app_from_folder(run, project_root: Path, name: str = "Globex_155461",
                     company: str = "Globex", role: str = "Backend Entwickler",
                     url: str = URL) -> str:
    _intake_folder(project_root, name, url=url)
    return make_app(run, company, role, "2026-10-05", "--from-folder", name)


def test_posting_prints_the_text_captured_with_the_application(run, project_root):
    app_id = _app_from_folder(run, project_root)
    result = run("posting", app_id)
    assert result.ok, result.output
    assert "Backend mit Java und Go." in result.output
    assert "Job posting" in result.output
    assert "applications/Globex_155461/posting.txt" in result.output


def test_posting_json_payload_carries_the_text_and_the_source(run, project_root):
    app_id = _app_from_folder(run, project_root)
    detail = payload(run, "posting", app_id, "--json")
    assert detail["text"] == POSTING
    assert detail["problem"] is None
    assert detail["posting_file"] == "applications/Globex_155461/posting.txt"
    assert detail["posting_origin"] == "the intake folder applications/Globex_155461"
    assert detail["job_url"] == URL
    assert detail["other_posting_files"] == []
    assert detail["application_id"] == app_id


def test_raw_prints_only_the_stored_text(run, project_root):
    """--raw exists so the text can go straight into a pager."""
    app_id = _app_from_folder(run, project_root)
    result = run("posting", app_id, "--raw")
    assert result.ok, result.output
    assert result.output.strip() == POSTING.strip()
    assert "posting file" not in result.output


def test_posting_accepts_the_same_identifiers_as_show(run, project_root):
    app_id = _app_from_folder(run, project_root)
    _app_from_folder(run, project_root, name="Globex_155331", role="Backend Developer",
                     url="https://example.com/jobs/155331")

    ambiguous = run("posting", "Globex")
    assert ambiguous.exit_code == 2
    assert "matches several applications" in ambiguous.output

    by_role = run("posting", "Backend Entwickler")
    assert by_role.ok
    assert "155461" in by_role.output


def test_posting_needs_an_application_when_there_is_no_terminal(run, project_root):
    _app_from_folder(run, project_root)
    result = run("posting")
    assert result.exit_code == 2
    assert "interactive terminal" in result.output


def test_an_application_without_a_captured_posting_says_so(run):
    """Nothing captured is not an error: the job url is still worth printing."""
    app_id = make_app(run, "Ohne Anzeige GmbH", "Backend Engineer", "2026-01-05",
                      "--url", "https://example.com/jobs/9")
    result = run("posting", app_id)
    assert result.ok, result.output
    assert "no stored posting text" in result.stderr
    assert "https://example.com/jobs/9" in result.stderr
    detail = payload(run, "posting", app_id, "--json")
    assert detail["text"] is None and detail["posting_file"] is None
    assert detail["problem"] is None


def test_a_linked_folder_without_a_posting_points_at_the_capture_tool(run, project_root):
    app_id = _app_from_folder(run, project_root)
    (project_root / "applications" / "Globex_155461" / "posting.txt").unlink()

    result = run("posting", app_id)
    assert result.ok, result.output
    assert "has no posting.txt" in result.stderr
    assert "capture the posting with ./posting" in result.stderr
    assert payload(run, "posting", app_id, "--json")["text"] is None


def test_a_posting_swapped_for_a_symlink_is_refused(run, project_root, tmp_path):
    app_id = _app_from_folder(run, project_root)
    folder = project_root / "applications" / "Globex_155461"
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("secret\n", encoding="utf-8")
    (folder / "posting.txt").unlink()
    (folder / "posting.txt").symlink_to(elsewhere)

    result = run("posting", app_id)
    assert result.exit_code == 2
    assert "refusing to follow it" in result.output
    assert "secret" not in result.output


def test_an_empty_posting_file_is_reported(run, project_root):
    app_id = _app_from_folder(run, project_root)
    (project_root / "applications" / "Globex_155461" / "posting.txt").write_text("\n")
    result = run("posting", app_id)
    assert result.exit_code == 2
    assert "posting.txt is empty" in result.output


def test_an_attached_posting_file_is_used_when_no_folder_is_linked(run, tmp_path):
    app_id = make_app(run, "Angehängt GmbH", "Backend Engineer", "2026-01-05")
    source = tmp_path / "stellenanzeige.txt"
    source.write_text(POSTING, encoding="utf-8")
    assert run("attach", app_id, str(source), "--type", "job_posting").ok

    result = run("posting", app_id)
    assert result.ok, result.output
    assert "Backend mit Java und Go." in result.output
    detail = payload(run, "posting", app_id, "--json")
    assert detail["posting_file"] == f"applications/{app_id}/stellenanzeige.txt"
    assert detail["posting_origin"] == "an attached posting file"


def test_the_intake_folder_copy_wins_over_the_application_folder_copy(run, project_root):
    app_id = _app_from_folder(run, project_root)
    own = project_root / "applications" / app_id
    own.mkdir(parents=True, exist_ok=True)
    (own / "posting.txt").write_text(OTHER, encoding="utf-8")

    result = run("posting", app_id)
    assert result.ok, result.output
    assert "Backend mit Java und Go." in result.output
    assert OTHER.strip() not in result.output
    assert "also stored" in result.stderr
    detail = payload(run, "posting", app_id, "--json")
    assert detail["posting_file"] == "applications/Globex_155461/posting.txt"
    assert detail["other_posting_files"] == [f"applications/{app_id}/posting.txt"]


def test_show_points_at_the_posting_of_the_application(run, project_root):
    app_id = _app_from_folder(run, project_root)
    shown = run("show", app_id)
    assert shown.ok, shown.output
    assert "applications/Globex_155461/posting.txt" in shown.output
    assert f"job posting {app_id[:8]}" in shown.output
    assert payload(run, "show", app_id, "--json")["posting_file"] == (
        "applications/Globex_155461/posting.txt")


def test_show_says_when_no_posting_was_captured(run):
    app_id = make_app(run, "Ohne Anzeige GmbH", "Backend Engineer", "2026-01-05")
    shown = run("show", app_id)
    assert shown.ok, shown.output
    assert "no stored posting text" in shown.output
    assert payload(run, "show", app_id, "--json")["posting_file"] is None


def test_a_huge_posting_is_truncated_instead_of_read_into_memory(monkeypatch, tmp_path):
    from job_cli import intake

    monkeypatch.setattr(intake, "MAX_POSTING_BYTES", 32)
    path = tmp_path / "posting.txt"
    path.write_text("x" * 200, encoding="utf-8")
    text, problem = intake.read_posting_file(path)
    assert problem is None
    assert text.startswith("x" * 32)
    assert "truncated at 32 bytes" in text
    assert len(text) < 100


def test_a_posting_file_that_is_a_directory_is_refused(tmp_path):
    from job_cli import intake

    path = tmp_path / "posting.txt"
    path.mkdir()
    text, problem = intake.read_posting_file(path)
    assert text is None
    assert "is not a regular file" in problem
