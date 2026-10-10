"""The applications/ intake folders: scan, identifiers, and the add link.

The posting capture tool (tools/posting_entry) writes
``applications/<company>[_<posting-id>]/posting.txt`` plus ``source.txt``; the CV
batch tool writes the CVs next to them.  The tracker reads those folders when a
new application starts from one and never changes them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_cli import intake
from job_cli.errors import UsageError

from helpers import find_id, payload, rows

SOURCE_URL = "https://www.linkedin.com/jobs/view/4471519418/"


def make_folder(project_root: Path, name: str, *, url: str | None = SOURCE_URL,
                posting: str | None = "Details zum Jobangebot\nJava Developer\n",
                cvs: bool = True) -> Path:
    """Write one intake folder the way the capture tool does."""
    folder = project_root / "applications" / name
    folder.mkdir(parents=True, exist_ok=True)
    if posting is not None:
        (folder / "posting.txt").write_text(posting, encoding="utf-8")
    if url is not None:
        (folder / "source.txt").write_text(url + "\n", encoding="utf-8")
    if cvs:
        # The CV file names are the cv tool's business; the tracker only has to
        # tolerate extra files in the folder.
        (folder / f"CV_DE_{name}.pdf").write_text("%PDF-1.4\n", encoding="utf-8")
        (folder / f"CV_EN_{name}.pdf").write_text("%PDF-1.4\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------
# the identifier <company>[_<posting-id>]
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "company", "posting_id"),
    [
        ("Nimbus", "Nimbus", None),
        ("Aperture", "Aperture", None),
        ("Acme GmbH_2463", "Acme GmbH", "2463"),
        ("Bronto", "Bronto", None),
        ("Cogswell", "Cogswell", None),
        ("Hooli AG", "Hooli AG", None),
        ("Globex_155331", "Globex", "155331"),
        ("Globex_155461", "Globex", "155461"),
        ("Vertex GmbH", "Vertex GmbH", None),
        ("Initech Bank_2024-0091", "Initech Bank", "2024-0091"),
        ("Cyberdyne GmbH", "Cyberdyne GmbH", None),
        ("Umbrella-Corp", "Umbrella-Corp", None),
        ("Soylent GmbH", "Soylent GmbH", None),
        # A trailer that is not a posting id keeps the underscore with the company.
        ("no_idea", "no_idea", None),
        ("Foo_123_bar", "Foo_123_bar", None),
    ],
)
def test_split_identifier_uses_the_last_underscore_before_a_posting_id(
        name, company, posting_id):
    assert intake.split_identifier(name) == (company, posting_id)
    assert intake.build_identifier(company, posting_id) == name


def test_normalize_link_clears_blank_words_and_refuses_a_path():
    for cleared in (None, "", "   ", "none", "NULL", "-"):
        assert intake.normalize_link(cleared) is None
    assert intake.normalize_link(" Globex_155461 ") == "Globex_155461"
    for bad in ("../escape", "applications/x", "a/b", "."):
        with pytest.raises(UsageError):
            intake.normalize_link(bad)


# --------------------------------------------------------------------------
# reading one folder
# --------------------------------------------------------------------------
def test_scan_lists_intake_folders_and_skips_everything_else(project_root):
    make_folder(project_root, "Globex_155461")
    applications = project_root / "applications"
    (applications / ".staging").mkdir()
    (applications / ".staging" / "posting.txt").write_text("hidden")
    (applications / "plain-file.txt").write_text("not a folder")
    (applications / "notes-only").mkdir()
    (applications / "notes-only" / "thoughts.md").write_text("no posting")
    (applications / "linked").symlink_to(applications / "Globex_155461")

    result = intake.scan(applications)
    assert [folder.name for folder in result.folders] == ["Globex_155461"]
    reasons = {item.name: item.reason for item in result.skipped}
    assert "hidden" in reasons[".staging"]
    assert "symlink" in reasons["linked"]
    assert "posting.txt" in reasons["notes-only"]
    assert "plain-file.txt" not in reasons  # a plain file is not reported at all


def test_source_url_uses_the_first_non_blank_line_and_refuses_a_symlink(
        project_root, tmp_path):
    folder = make_folder(project_root, "Aperture", url=None)
    (folder / "source.txt").write_text("\nhttps://example.com/job\n", encoding="utf-8")
    assert intake.read_source_url(folder) == ("https://example.com/job", None)

    (folder / "source.txt").unlink()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("https://example.com/secret\n", encoding="utf-8")
    (folder / "source.txt").symlink_to(outside)
    url, problem = intake.read_source_url(folder)
    assert url is None and "symlink" in problem

    (folder / "source.txt").unlink()
    (folder / "source.txt").write_text("mailto:someone@example.com\n", encoding="utf-8")
    url, problem = intake.read_source_url(folder)
    assert url is None and "http(s)" in problem


def test_scan_reports_a_folder_without_a_usable_source(project_root):
    folder = make_folder(project_root, "Bronto", url=None)
    (folder / "posting.txt").write_text("a posting without a source")
    (found,) = intake.scan(project_root / "applications").folders
    assert found.source_url is None and "no source.txt" in found.source_problem
    assert found.company == "Bronto" and found.has_posting


def test_newest_prefers_the_folder_that_changed_last(project_root):
    import os
    older = make_folder(project_root, "Aperture")
    newer = make_folder(project_root, "Bronto")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    folders = intake.scan(project_root / "applications").folders
    assert intake.newest(folders).name == "Bronto"
    assert intake.newest([]) is None


def test_find_refuses_a_name_outside_the_applications_directory(project_root, tmp_path):
    make_folder(project_root, "Aperture")
    outside = tmp_path / "Aperture"
    outside.mkdir()
    with pytest.raises(UsageError) as excinfo:
        intake.find(project_root / "applications", str(outside))
    assert "directly inside" in str(excinfo.value)
    assert intake.find(project_root / "applications", "Aperture").name == "Aperture"


# --------------------------------------------------------------------------
# add --from-folder
# --------------------------------------------------------------------------
def test_add_from_folder_takes_the_company_and_the_source_url(run, project_root):
    make_folder(project_root, "Globex_155461")
    result = run("add", "--from-folder", "Globex_155461", "--role", "Java Developer",
                 "--submitted-on", "2026-01-05", "--json")
    assert result.ok, result.output
    saved = json.loads(result.output)
    assert saved["company"] == "Globex"
    assert saved["job_url"] == SOURCE_URL
    assert saved["intake_folder"] == "Globex_155461"
    assert "Globex_155461" in result.stderr

    detail = payload(run, "show", saved["id"], "--json")["application"]
    assert detail["intake_folder"] == "Globex_155461"
    assert detail["job_url"] == SOURCE_URL


def test_add_from_folder_accepts_the_applications_prefix(run, project_root):
    make_folder(project_root, "Aperture")
    result = run("add", "--from-folder", "applications/Aperture", "--role", "Dev",
                 "--submitted-on", "2026-01-05", "--json")
    assert result.ok, result.output
    assert json.loads(result.output)["company"] == "Aperture"


def test_add_from_folder_keeps_explicit_company_and_url(run, project_root):
    make_folder(project_root, "Globex_155461")
    result = run("add", "--from-folder", "Globex_155461", "--company", "Globex GmbH",
                 "--role", "Java Developer", "--url", "https://example.com/other",
                 "--submitted-on", "2026-01-05", "--json")
    assert result.ok, result.output
    saved = json.loads(result.output)
    assert saved["company"] == "Globex GmbH"
    assert saved["job_url"] == "https://example.com/other"
    assert saved["intake_folder"] == "Globex_155461"


def test_add_from_folder_without_a_source_leaves_the_url_empty(run, project_root):
    make_folder(project_root, "Bronto", url=None)
    result = run("add", "--from-folder", "Bronto", "--role", "Dev",
                 "--submitted-on", "2026-01-05", "--json")
    assert result.ok, result.output
    saved = json.loads(result.output)
    assert saved["company"] == "Bronto"
    assert saved["job_url"] is None
    assert "source.txt" in result.stderr


def test_add_from_folder_does_not_register_the_files_as_artifacts(run, project_root):
    make_folder(project_root, "Aperture")
    assert run("add", "--from-folder", "Aperture", "--role", "Dev",
               "--submitted-on", "2026-01-05").ok
    app_id = find_id(run, "Aperture")
    detail = payload(run, "show", app_id, "--json")
    assert detail["artifacts"] == []
    assert (project_root / "applications" / "Aperture" / "posting.txt").is_file()


def test_add_from_folder_refuses_missing_escaping_and_application_folders(
        run, new_app, project_root, tmp_path):
    make_folder(project_root, "Aperture")
    missing = run("add", "--from-folder", "nope", "--role", "Dev",
                  "--submitted-on", "2026-01-05")
    assert missing.exit_code == 2 and "no intake folder" in missing.output

    escaped = run("add", "--from-folder", "../elsewhere", "--role", "Dev",
                  "--submitted-on", "2026-01-05")
    assert escaped.exit_code == 2 and "path separator" in escaped.output

    app_id = new_app()
    source = tmp_path / "cv.txt"
    source.write_text("x")
    assert run("attach", app_id, str(source), "--type", "cv").ok
    own = run("add", "--from-folder", app_id, "--role", "Dev",
              "--submitted-on", "2026-01-05")
    assert own.exit_code == 2 and "belongs to an existing application" in own.output


# --------------------------------------------------------------------------
# job intake
# --------------------------------------------------------------------------
def test_intake_lists_folders_and_their_application(run, project_root):
    make_folder(project_root, "Aperture")
    make_folder(project_root, "Bronto")
    fresh = json.loads(run("intake", "--json").output)
    assert [row["folder"] for row in fresh] == ["Aperture", "Bronto"]
    assert all(row["application_id"] is None for row in fresh)
    assert fresh[0]["company"] == "Aperture" and fresh[0]["posting_id"] is None
    assert fresh[0]["source_url"] == SOURCE_URL
    assert fresh[0]["source"] == "www.linkedin.com"
    assert fresh[0]["file_count"] == 4

    assert run("add", "--from-folder", "Aperture", "--role", "Dev",
               "--submitted-on", "2026-01-05").ok
    app_id = find_id(run, "Aperture")
    linked = {row["folder"]: row for row in json.loads(run("intake", "--json").output)}
    assert linked["Aperture"]["application_id"] == app_id
    assert linked["Bronto"]["application_id"] is None

    pending = json.loads(run("intake", "--pending", "--json").output)
    assert [row["folder"] for row in pending] == ["Bronto"]


def test_intake_is_empty_without_an_applications_directory(run):
    assert json.loads(run("intake", "--json").output) == []
    assert run("intake").ok


def test_edit_can_set_replace_and_clear_the_folder_link(run, new_app, project_root):
    app_id = new_app()
    make_folder(project_root, "Aperture")
    changed = run("edit", app_id, "--set", "intake_folder=Aperture")
    assert changed.ok, changed.output
    assert payload(run, "show", app_id, "--json")["application"]["intake_folder"] == "Aperture"

    cleared = run("edit", app_id, "--set", "intake_folder=-")
    assert cleared.ok, cleared.output
    assert payload(run, "show", app_id, "--json")["application"]["intake_folder"] is None

    refused = run("edit", app_id, "--set", "intake_folder=missing")
    assert refused.exit_code == 2 and "no intake folder" in refused.output
    assert payload(run, "show", app_id, "--json")["application"]["intake_folder"] is None


def test_delete_application_keeps_the_intake_folder_on_disk(run, project_root):
    folder = make_folder(project_root, "Globex_155461")
    assert run("add", "--from-folder", "Globex_155461", "--role", "Dev",
               "--submitted-on", "2026-01-05").ok
    app_id = find_id(run, "Globex")
    removed = run("delete", "application", app_id, "--yes")
    assert removed.ok, removed.output
    assert "intake folder applications/Globex_155461" in removed.output
    assert (folder / "posting.txt").is_file() and (folder / "source.txt").is_file()
    assert rows(run, "list", "--json") == []
    still_there = json.loads(run("intake", "--pending", "--json").output)
    assert [row["folder"] for row in still_there] == ["Globex_155461"]


# --------------------------------------------------------------------------
# linking applications that were entered before their folder existed
# --------------------------------------------------------------------------
def test_scan_can_exclude_application_directories(project_root):
    application_id = "6f1b2c3d-0000-4000-8000-000000000000"
    make_folder(project_root, application_id)
    make_folder(project_root, "Aperture")
    applications = project_root / "applications"
    assert [folder.name for folder in intake.scan(applications).folders] == [
        application_id, "Aperture"]
    excluded = intake.scan(applications, exclude={application_id})
    assert [folder.name for folder in excluded.folders] == ["Aperture"]
    reasons = {item.name: item.reason for item in excluded.skipped}
    assert "existing application" in reasons[application_id]


def test_plan_links_matches_the_job_url_before_the_employer(project_root):
    make_folder(project_root, "Globex_155331", url=None)
    make_folder(project_root, "Globex_155461")
    folders = intake.scan(project_root / "applications").folders
    applications = [{"id": "a1", "company": "Globex", "job_url": SOURCE_URL,
                     "intake_folder": None}]
    proposals, ambiguous = intake.plan_links(folders, applications)
    assert [(item.folder, item.application_id) for item in proposals] == [
        ("Globex_155461", "a1")]
    assert ambiguous == []


def test_plan_links_reports_an_employer_with_several_applications(project_root):
    make_folder(project_root, "Aperture", url=None)
    folders = intake.scan(project_root / "applications").folders
    applications = [
        {"id": "a1", "company": "Aperture", "job_url": None, "intake_folder": None},
        {"id": "a2", "company": "Aperture", "job_url": None, "intake_folder": None},
    ]
    proposals, ambiguous = intake.plan_links(folders, applications)
    assert proposals == []
    assert [(item.folder, item.application_ids) for item in ambiguous] == [
        ("Aperture", ("a1", "a2"))]


def test_startup_links_by_job_url_and_keeps_json_stdout_clean(run, project_root):
    make_folder(project_root, "Nimbus")
    saved = json.loads(run("add", "--company", "Nimbus", "--role", "Java Software Engineer",
                           "--submitted-on", "2026-01-05", "--url", SOURCE_URL, "--json").output)
    before = payload(run, "--no-link", "show", saved["id"], "--json")["application"]
    assert before["intake_folder"] is None

    linked = run("list", "--json")
    assert linked.ok, linked.output
    assert "linked" in linked.stderr and "Nimbus" in linked.stderr
    assert json.loads(linked.output)                     # notes never reach --json stdout
    after = payload(run, "--no-link", "show", saved["id"], "--json")["application"]
    assert after["intake_folder"] == "Nimbus"

    # a second start has nothing left to do
    again = run("list")
    assert again.ok and "linked" not in again.stderr


def test_startup_links_the_only_application_of_an_employer(run, project_root):
    make_folder(project_root, "Aperture", url=None)
    saved = json.loads(run("add", "--company", "Aperture", "--role", "Dev",
                           "--submitted-on", "2026-01-05", "--json").output)
    assert run("list").ok
    detail = payload(run, "--no-link", "show", saved["id"], "--json")["application"]
    assert detail["intake_folder"] == "Aperture"


def test_startup_never_guesses_between_two_folders_of_one_employer(run, project_root):
    make_folder(project_root, "Globex_155331", url=None)
    make_folder(project_root, "Globex_155461", url=None)
    saved = json.loads(run("add", "--company", "Globex", "--role", "Java Developer",
                           "--submitted-on", "2026-01-05", "--json").output)
    started = run("list")
    assert started.ok and "linked" not in started.stderr
    detail = payload(run, "--no-link", "show", saved["id"], "--json")["application"]
    assert detail["intake_folder"] is None

    hint = run("intake")
    assert hint.ok, hint.output
    assert "could belong to" in hint.output
    assert saved["id"][:8] in hint.output


def test_two_postings_of_one_employer_become_two_linked_applications(run, project_root):
    make_folder(project_root, "Globex_155331", url="https://www.linkedin.com/jobs/view/4472333071/")
    make_folder(project_root, "Globex_155461")
    first = json.loads(run("add", "--from-folder", "Globex_155331", "--role", "Java Developer",
                           "--submitted-on", "2026-01-05", "--json").output)
    second = run("add", "--from-folder", "Globex_155461", "--role", "Java Developer",
                 "--submitted-on", "2026-01-06", "--json")
    assert second.ok, second.output          # a second posting is not a duplicate
    assert "a different posting" in second.stderr

    listed = {row["folder"]: row for row in json.loads(run("intake", "--json").output)}
    assert listed["Globex_155331"]["application_id"] == first["id"]
    assert listed["Globex_155461"]["application_id"] == json.loads(second.output)["id"]
    assert len(rows(run, "list", "--json")) == 2
    assert run("intake", "--pending", "--json").output.strip() == "[]"
