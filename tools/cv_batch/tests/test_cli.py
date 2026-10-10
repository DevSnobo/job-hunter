"""Command-line surface: interactive flow, guards and exit codes."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from cv_batch import apps as apps_mod
from cv_batch.cli import main
from cv_batch.runner import BatchInterrupted, BatchSummary, JobResult, JobSpec

from conftest import el_entry_id, selection_answer, sp_entry_id, target_name
from test_runner import make_runner

EL = el_entry_id()
SP = sp_entry_id()
ANSWER = selection_answer({EL: [f"{EL}/el1-short"], SP: [f"{SP}/b1"]})


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty(monkeypatch):
    def install(text: str):
        stream = FakeTTY(text)
        monkeypatch.setattr(sys, "stdin", stream)
        return stream

    return install


def base_args(project, fake_agent, fake_renderer):
    return [
        "--root",
        str(project.root),
        "--model-command",
        fake_agent.exe,
        "--generator-exe",
        fake_renderer.exe,
        "--plain",
    ]


def test_explain_modes_documents_the_tradeoffs(capsys):
    assert main(["--explain-modes"]) == 0
    out = capsys.readouterr().out
    assert "not a token promise" in out.lower()
    assert "deepseek-v4-flash" in out
    assert "never Pro" in out
    assert "deepseek (default)" in out
    assert "--resume <id>" in out


def test_dry_run_writes_nothing(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    project.add_app("Beta")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "Alpha" in out
    assert not project.state.exists()
    assert fake_agent.calls() == []
    assert sorted(p.name for p in (project.root / "applications" / "Alpha").iterdir()) == [
        "posting.txt",
        "source.txt",
    ]


def test_non_interactive_selection_without_yes_is_refused(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1"])
    assert code == 2
    assert "refusing to start without --yes" in capsys.readouterr().err


def test_reuse_with_parallel_jobs_is_rejected(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--jobs",
            "2",
            "--session-mode",
            "reuse",
        ]
    )
    assert code == 2
    assert "never shared" in capsys.readouterr().err


def test_jobs_above_the_cap_is_rejected(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--jobs", "9"])
    assert code == 2
    assert "between 1 and 4" in capsys.readouterr().err


def test_out_of_range_and_reversed_selection(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    common = [*base_args(project, fake_agent, fake_renderer), "--language", "de", "--yes"]
    assert main([*common, "--select", "9"]) == 2
    assert "out of range" in capsys.readouterr().err
    assert main([*common, "--select", "2-1"]) == 2
    assert "reversed range" in capsys.readouterr().err
    assert fake_agent.calls() == []


def test_no_usable_postings_is_reported(tmp_path, capsys):
    root = tmp_path / "empty-jobs"
    (root / "applications" / "no-posting-here").mkdir(parents=True)
    code = main(["--root", str(root), "--language", "de", "--select", "1", "--yes"])
    assert code == 2
    assert "no usable posting folders" in capsys.readouterr().err


def test_all_option_runs_every_missing_cv(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    project.add_app("Beta")
    project.add_app("Gamma")
    project.target("Gamma", "de").write_bytes(b"%PDF-1.4 present")

    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--yes"])

    assert code == 0
    out = capsys.readouterr().out
    assert "summary: ok 2, skipped 0, failed 0" in out
    assert project.target("Alpha", "de").exists()
    assert project.target("Beta", "de").exists()
    assert project.target("Gamma", "de").read_bytes() == b"%PDF-1.4 present"
    assert len(fake_agent.calls()) == 2


def test_failed_job_reports_exit_code_one(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"exit": 1, "stderr": "error: upstream exploded"}])
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--yes"])
    captured = capsys.readouterr()
    assert code == 1
    assert "summary: ok 0, skipped 0, failed 1" in captured.out
    assert "upstream exploded" in captured.err


def test_valid_targets_are_not_numbered(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    project.target("Alpha", "de").write_bytes(b"%PDF-1.4 ok")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "  1. Beta" in out
    assert "Alpha" not in out.split("Missing tailored CVs:")[1]


def test_invalid_targets_are_listed_but_never_numbered(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    project.target("Alpha", "de").write_bytes(b"")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Invalid targets: 1" in out
    assert "empty (0 bytes)" in out
    assert "  1. Beta" in out


def test_interactive_blank_language_defaults_to_de_and_blank_selection_cancels(project, fake_agent, fake_renderer, tty, capsys):
    project.add_app("Alpha")
    tty("\n\n")
    code = main(base_args(project, fake_agent, fake_renderer))
    out = capsys.readouterr().out
    assert code == 0
    assert "Language       : DE" in out
    assert "cancelled: nothing selected" in out
    assert fake_agent.calls() == []


def test_interactive_full_flow_selects_a_range_and_confirms(project, fake_agent, fake_renderer, tty, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    for name in ("Alpha", "Beta", "Gamma", "Delta"):
        project.add_app(name)
    tty("en\n2-3\ny\n")
    code = main(base_args(project, fake_agent, fake_renderer))
    out = capsys.readouterr().out
    assert code == 0
    assert "Ready to tailor 2 CV(s) in EN." in out
    # display order is case-insensitive alphabetical: Alpha, Beta, Delta, Gamma
    assert "  2. Beta" in out and "  3. Delta" in out
    assert project.target("Beta", "en").exists()
    assert project.target("Delta", "en").exists()
    assert not project.target("Alpha", "en").exists()
    assert not project.target("Gamma", "en").exists()


def test_interactive_declined_confirmation_writes_nothing(project, fake_agent, fake_renderer, tty, capsys):
    project.add_app("Alpha")
    tty("de\n1\nn\n")
    assert main(base_args(project, fake_agent, fake_renderer)) == 0
    assert "cancelled" in capsys.readouterr().out
    assert fake_agent.calls() == []
    assert not project.state.exists()


def test_interactive_bad_language_reprompts_then_succeeds(project, fake_agent, fake_renderer, tty, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    tty("spanish\nen\n1\ny\n")
    assert main(base_args(project, fake_agent, fake_renderer)) == 0
    captured = capsys.readouterr()
    assert "only 'de' or 'en' is accepted" in captured.out
    assert project.target("Alpha", "en").exists()


def test_ctrl_c_at_the_language_prompt_exits_130(project, fake_agent, fake_renderer, monkeypatch, capsys):
    project.add_app("Alpha")

    def boom(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    code = main(base_args(project, fake_agent, fake_renderer))
    assert code == 130
    assert "cancelled" in capsys.readouterr().out


def test_ctrl_c_during_a_batch_exits_130_and_keeps_finished_work(project, fake_agent, fake_renderer, monkeypatch, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")

    finished = JobResult(
        JobSpec(
            "Alpha",
            project.applications / "Alpha",
            project.applications / "Alpha" / "posting.txt",
            "de",
            naming=project.naming("de"),
        ),
        "ok",
        "wrote something",
    )

    def interrupt(*_args, **_kwargs):
        raise BatchInterrupted(BatchSummary([finished]))

    monkeypatch.setattr("cv_batch.cli.run_batch", interrupt)
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--yes"])
    out = capsys.readouterr().out
    assert code == 130
    assert "interrupted" in out and "partial batch: ok 1" in out
    assert "finished:" in out


def test_explicit_deepseek_choice_never_falls_back(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"exit": 1, "stderr": "error: quota exceeded", "answer": "x"}])
    project.add_app("Alpha")
    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--model",
            "deepseek",
        ]
    )
    assert code == 1
    calls = fake_agent.calls()
    assert len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("--provider") + 1] == "deepseek"
    assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "deepseek-v4-flash"


def test_default_model_is_deepseek_flash(project, fake_agent, fake_renderer, capsys):
    """No --model flag: DeepSeek Flash is chosen, and the plan says so."""
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Model          : deepseek (default) - DeepSeek Flash only" in out
    calls = fake_agent.calls()
    assert len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("--provider") + 1] == "deepseek"
    assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "deepseek-v4-flash"


def test_default_model_never_falls_back_to_astra(project, fake_agent, fake_renderer, capsys):
    """A quota failure on the default model fails the job instead of calling Astra."""
    fake_agent.steps([{"exit": 1, "stderr": "error: quota exceeded for deepseek", "answer": "x"}])
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--yes"])
    captured = capsys.readouterr()
    assert code == 1
    calls = fake_agent.calls()
    assert len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("--provider") + 1] == "deepseek"
    assert "Astra was unusable" not in captured.out


def test_explicit_auto_choice_still_starts_on_astra(project, fake_agent, fake_renderer, capsys):
    """--model auto keeps the old Astra-first policy with the Flash fallback."""
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--model",
            "auto",
        ]
    )
    assert code == 0
    calls = fake_agent.calls()
    assert calls[0]["argv"][calls[0]["argv"].index("--provider") + 1] == "openai-codex"
    assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "gpt-6-astra"


def test_unusable_posting_is_reported_as_a_warning(project, fake_agent, fake_renderer, capsys):
    good = project.add_app("Alpha")
    empty = project.add_app("Empty")
    (empty / "posting.txt").write_text("", encoding="utf-8")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "warn: Empty: posting.txt is empty" in out
    assert good.exists()


# --------------------------------------------------------------- F1: invalid targets

def test_invalid_targets_are_numbered_only_with_the_explicit_optin(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    project.target("Alpha", "de").write_bytes(b"")
    common = [*base_args(project, fake_agent, fake_renderer), "--language", "de", "--dry-run"]

    assert main([*common, "--select", "1"]) == 0
    out = capsys.readouterr().out
    assert "  1. Beta" in out
    assert "REPLACE" not in out
    assert "not numbered" in out

    assert main([*common, "--select", "2"]) == 2
    assert "out of range" in capsys.readouterr().err.replace("error: ", "")

    assert main([*common, "--regenerate-invalid", "--select", "2"]) == 0
    out = capsys.readouterr().out
    assert "  2. Alpha  [REPLACE: target file is empty (0 bytes)]" in out
    assert "REPLACING INVALID TARGET" in out
    assert "REPLACE: " in out


def test_invalid_only_default_run_does_not_claim_every_cv_is_valid(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.target("Alpha", "de").write_bytes(b"junk")

    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de"])

    out = capsys.readouterr().out
    assert code == 0
    assert "already has a valid" not in out
    assert "invalid" in out and "--regenerate-invalid" in out
    assert fake_agent.calls() == []
    assert project.target("Alpha", "de").read_bytes() == b"junk"


def test_symlink_and_directory_targets_are_never_selectable(project, fake_agent, fake_renderer, tmp_path, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    project.add_app("Gamma")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    os.symlink(outside, project.target("Alpha", "de"))
    project.target("Beta", "de").mkdir()

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--regenerate-invalid",
            "--all",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Never replaceable" in out
    assert "  1. Gamma" in out
    assert "2." not in out.split("Never replaceable")[0]
    assert fake_agent.calls() == []
    assert outside.read_bytes() == b"%PDF-1.4 elsewhere"


def test_unreplaceable_only_invalid_targets_report_safely(project, fake_agent, fake_renderer, tmp_path, capsys):
    project.add_app("Alpha")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    os.symlink(outside, project.target("Alpha", "de"))

    code = main(base_args(project, fake_agent, fake_renderer) + ["--language", "de", "--regenerate-invalid"])

    out = capsys.readouterr().out
    assert code == 0
    assert "cannot be replaced safely" in out
    assert fake_agent.calls() == []
    assert outside.read_bytes() == b"%PDF-1.4 elsewhere"


def test_regenerate_invalid_replaces_and_backs_up_the_old_file(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"junk that is not a pdf")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--regenerate-invalid",
            "--select",
            "1",
            "--yes",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "summary: ok 1, skipped 0, failed 0" in out
    assert "Replaceable invalid targets (explicit opt-in" in out
    assert "  1. Alpha  [REPLACE: target is not a PDF (bad header)]" in out
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"junk that is not a pdf"


def test_invalid_selection_without_the_optin_still_refuses_to_replace(project, fake_agent, fake_renderer, capsys):
    """The runner-level guard stays in place even if a job for an invalid target is built."""
    from cv_batch.runner import JobSpec

    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"junk")
    scan = apps_mod.scan(project.applications)
    job = JobSpec.from_application(scan.applications[0], "de", project.naming("de"))
    runner = make_runner(project, fake_agent, fake_renderer)

    result = runner.run_job(job)

    assert result.status == "failed"
    assert "--regenerate-invalid" in result.message
    assert target.read_bytes() == b"junk"


# ------------------------------------------------- overwrite existing CVs (--overwrite)

def test_existing_valid_cv_is_not_numbered_without_overwrite(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 existing tailored CV")
    common = [*base_args(project, fake_agent, fake_renderer), "--language", "de", "--dry-run"]

    assert main([*common, "--select", "1"]) == 0
    out = capsys.readouterr().out
    assert "Existing CVs   : 1 valid [not numbered; pass --overwrite to replace an existing CV]" in out
    assert "  1. Beta" in out
    assert "OVERWRITE" not in out

    # the existing CV is not selectable, so index 2 does not exist yet
    assert main([*common, "--select", "2"]) == 2
    assert "out of range" in capsys.readouterr().err.replace("error: ", "")

    assert target.read_bytes() == b"%PDF-1.4 existing tailored CV"
    assert fake_agent.calls() == []


def test_overwrite_numbers_the_existing_cvs(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 existing tailored CV")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--overwrite",
            "--select",
            "2",
            "--dry-run",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Existing CVs   : 1 valid [--overwrite: selectable for replacement" in out
    assert "Existing CVs (explicit --overwrite; each old file is backed up" in out
    assert "  2. Alpha  [OVERWRITE: valid CV already exists]" in out
    assert "[OVERWRITING EXISTING CV]" in out
    assert fake_agent.calls() == []
    assert target.read_bytes() == b"%PDF-1.4 existing tailored CV"


def test_valid_only_run_points_at_the_overwrite_option(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 existing tailored CV")

    code = main(base_args(project, fake_agent, fake_renderer) + ["--language", "de"])

    out = capsys.readouterr().out
    assert code == 0
    assert "already has a valid DE CV" in out
    assert "--overwrite" in out
    assert fake_agent.calls() == []
    assert target.read_bytes() == b"%PDF-1.4 existing tailored CV"


def test_overwrite_replaces_an_existing_cv_and_backs_both_files_up(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 the previously tailored CV")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--overwrite",
            "--select",
            "1",
            "--yes",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "summary: ok 1, skipped 0, failed 0" in out
    assert "ok  : Alpha [de]" in out
    assert f"backup={target_name('Alpha', 'de')}." in out
    assert target.read_bytes().startswith(b"%PDF-")
    assert target.read_bytes() != b"%PDF-1.4 the previously tailored CV"
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"%PDF-1.4 the previously tailored CV"
    saved = json.loads(list(project.state.glob("inputs/*.json"))[0].read_text(encoding="utf-8"))
    assert saved["job"]["existing_target_state"] == "valid"
    assert saved["job"]["backup"] == str(backups[0])


def test_overwrite_also_makes_safe_invalid_targets_selectable(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"junk that is not a pdf")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--overwrite",
            "--select",
            "1",
            "--yes",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "  1. Alpha  [REPLACE: target is not a PDF (bad header)]" in out
    assert f"backup={target_name('Alpha', 'de')}." in out
    assert target.read_bytes().startswith(b"%PDF-")
    backups = list(project.state.glob("replaced/*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"junk that is not a pdf"


def test_overwrite_never_touches_a_symlinked_target(project, fake_agent, fake_renderer, tmp_path, capsys):
    project.add_app("Alpha")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    os.symlink(outside, project.target("Alpha", "de"))

    code = main(base_args(project, fake_agent, fake_renderer) + ["--language", "de", "--overwrite", "--all", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert "cannot be replaced safely" in out
    assert fake_agent.calls() == []
    assert outside.read_bytes() == b"%PDF-1.4 elsewhere"


def test_interactive_confirmation_names_the_overwrite(project, fake_agent, fake_renderer, tty, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 the previously tailored CV")
    tty("de\n1\ny\n")

    code = main(base_args(project, fake_agent, fake_renderer) + ["--overwrite"])

    out = capsys.readouterr().out
    assert code == 0
    assert "overwrites 1 existing CV file" in out
    assert "backed up" in out
    assert target.read_bytes().startswith(b"%PDF-")


# --------------------------------------------------------------- F10: limit bounds

@pytest.mark.parametrize(
    ("flag", "value", "fragment"),
    [
        ("--timeout", "0", "--timeout must be a finite number"),
        ("--timeout", "nan", "--timeout must be a finite number"),
        ("--timeout", "inf", "--timeout must be a finite number"),
        ("--timeout", "-5", "--timeout must be a finite number"),
        ("--timeout", "99999", "--timeout must be a finite number"),
        ("--render-timeout", "0", "--render-timeout must be a finite number"),
        ("--render-timeout", "nan", "--render-timeout must be a finite number"),
        ("--content-retries", "9", "--content-retries must be an integer between 0 and 3"),
        ("--trim-attempts", "0", "--trim-attempts must be an integer between 1 and 32"),
        ("--max-bullets-per-entry", "0", "--max-bullets-per-entry must be an integer between 1 and 50"),
        ("--max-bullets-total", "99", "--max-bullets-total must be an integer between 1 and 50"),
        ("--max-headline", "-1", "--max-headline must be an integer between 1 and 200"),
        ("--max-posting-chars", "0", "--max-posting-chars must be an integer between 1 and 200000"),
    ],
)
def test_out_of_range_limits_are_rejected(project, flag, value, fragment, capsys):
    project.add_app("Alpha")
    code = main(["--root", str(project.root), "--language", "de", "--select", "1", "--yes", "--plain", flag, value])
    assert code == 2
    assert fragment in capsys.readouterr().err


def test_default_limits_still_run_the_normal_flow(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--yes"])
    assert code == 0
    assert "summary: ok 1" in capsys.readouterr().out


def test_interactive_confirmation_names_the_invalid_replacement(project, fake_agent, fake_renderer, tty, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    project.target("Alpha", "de").write_bytes(b"junk")
    tty("de\n1\ny\n")

    code = main(base_args(project, fake_agent, fake_renderer) + ["--regenerate-invalid"])

    out = capsys.readouterr().out
    assert code == 0
    assert "replaces 1 existing invalid target" in out
    assert "backed up" in out


# ------------------------------------------------- layout (pluggable renderers)

def test_default_layout_is_classic_and_reaches_the_generator(project, fake_agent, fake_renderer, capsys):
    """No --layout: cv_batch sends the generator's own default explicitly."""
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")

    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--select", "1", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Layout         : classic (default)   (renderer --layout)" in out
    argv = fake_renderer.calls()[0]["argv"]
    assert argv[argv.index("--layout") + 1] == "classic"
    assert project.target("Alpha", "de").exists()


def test_explicit_layout_is_forwarded_unchanged(project, fake_agent, fake_renderer, capsys):
    """A third-party layout name is passed through verbatim, without a local list."""
    fake_agent.steps([{"answer": ANSWER}])
    fake_renderer.configure(layouts=("classic", "compact-2026"))
    project.add_app("Alpha")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--layout",
            "compact-2026",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Layout         : compact-2026   (renderer --layout)" in out
    assert "classic (default)" not in out
    argv = fake_renderer.calls()[0]["argv"]
    assert argv[argv.index("--layout") + 1] == "compact-2026"
    # the output file name never depends on the layout
    assert project.target("Alpha", "de").exists()


def test_interactive_confirmation_shows_the_layout(project, fake_agent, fake_renderer, tty, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    fake_renderer.configure(layouts=("classic", "sidebar"))
    project.add_app("Alpha")
    tty("de\n1\ny\n")

    code = main(base_args(project, fake_agent, fake_renderer) + ["--layout", "sidebar"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Layout: sidebar" in out


def test_unknown_layout_fails_with_the_generator_message(project, fake_agent, fake_renderer, capsys):
    """A bad name is not pre-validated; it surfaces as the renderer's own error."""
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--language",
            "de",
            "--select",
            "1",
            "--yes",
            "--layout",
            "does-not-exist",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "unknown layout" in captured.err
    assert "RenderError" in captured.err
    assert not project.target("Alpha", "de").exists()


def test_explain_modes_documents_the_layout_flag(capsys):
    assert main(["--explain-modes"]) == 0
    out = capsys.readouterr().out
    assert "--layout NAME" in out
    assert "cvgen render --layout NAME" in out
    assert "classic" in out
    assert "third-party" in out


# ------------------------------- --folder: restrict the run to one applications/<NAME> folder

def test_folder_option_renders_only_that_folder(project, fake_agent, fake_renderer, capsys):
    """The postings tool's call shape: one folder, --all, --yes, no prompt."""
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    project.add_app("Beta")

    code = main(
        [*base_args(project, fake_agent, fake_renderer), "--folder", "Alpha", "--language", "de", "--all", "--yes"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "summary: ok 1, skipped 0, failed 0" in out
    assert project.target("Alpha", "de").exists()
    assert not project.target("Beta", "de").exists()
    assert len(fake_agent.calls()) == 1


def test_unknown_folder_is_refused_and_never_falls_back_to_every_folder(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")

    code = main(
        [*base_args(project, fake_agent, fake_renderer), "--folder", "Nope", "--language", "de", "--all", "--yes"]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert f"--folder Nope: no such folder under {project.applications.resolve()}" in captured.err
    assert fake_agent.calls() == []
    assert fake_renderer.calls() == []
    assert not project.target("Alpha", "de").exists()
    assert not project.state.exists()


def test_folder_without_a_posting_is_refused_with_the_scan_reason(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    (project.root / "applications" / "NoPosting").mkdir()
    empty = project.root / "applications" / "EmptyPosting"
    empty.mkdir()
    (empty / "posting.txt").write_text("", encoding="utf-8")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--folder", "NoPosting",
            "--folder", "EmptyPosting",
            "--language", "de", "--all", "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--folder NoPosting: no posting.txt" in captured.err
    assert "--folder EmptyPosting: posting.txt is empty" in captured.err
    assert fake_agent.calls() == []
    assert not project.target("Alpha", "de").exists()
    assert not project.state.exists()


@pytest.mark.parametrize("name", ["", "   ", ".", "..", ".staging", "Nested/Alpha", "../Alpha"])
def test_folder_names_that_are_not_plain_basenames_are_refused(project, name, capsys):
    """A malformed name is refused before the language prompt and without a TTY."""
    project.add_app("Alpha")

    code = main(["--root", str(project.root), "--folder", name, "--all", "--yes", "--plain"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--folder must be a bare folder name below" in captured.err
    assert f"(got '{name}')" in captured.err
    assert "Language" not in captured.out
    assert not project.state.exists()


def test_folder_is_validated_before_the_language_prompt(project, capsys):
    """No --language and no TTY: the refused name still answers with exit code 2."""
    project.add_app("Alpha")

    code = main(["--root", str(project.root), "--folder", "Nope", "--all", "--yes", "--plain"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--folder Nope: no such folder under" in captured.err
    assert "Language" not in captured.out
    assert not project.state.exists()


def test_folder_filter_line_reports_the_scope(project, fake_agent, fake_renderer, capsys):
    for name in ("Alpha", "Beta", "Gamma"):
        project.add_app(name)

    code = main(
        [*base_args(project, fake_agent, fake_renderer), "--folder", "Beta", "--language", "de", "--all", "--dry-run"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Posting folders: 1 usable, 0 ignored, 0 unusable" in out
    assert "Folder filter  : Beta   (1 of 3 usable folders in scope)" in out
    assert "  1. Beta" in out
    assert fake_agent.calls() == []


def test_two_folder_values_both_run(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    for name in ("Alpha", "Beta", "Gamma"):
        project.add_app(name)

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--folder", "Gamma",
            "--folder", "Alpha",
            "--language", "de", "--all", "--yes",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Folder filter  : Gamma, Alpha   (2 of 3 usable folders in scope)" in out
    assert "summary: ok 2, skipped 0, failed 0" in out
    assert project.target("Gamma", "de").exists()
    assert project.target("Alpha", "de").exists()
    assert not project.target("Beta", "de").exists()
    assert len(fake_agent.calls()) == 2


def test_a_repeated_folder_name_runs_once(project, fake_agent, fake_renderer, capsys):
    fake_agent.steps([{"answer": ANSWER}])
    project.add_app("Alpha")
    project.add_app("Beta")

    code = main(
        [
            *base_args(project, fake_agent, fake_renderer),
            "--folder", "Alpha",
            "--folder", "Alpha",
            "--language", "de", "--all", "--yes",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Folder filter  : Alpha   (1 of 2 usable folders in scope)" in out
    assert "summary: ok 1, skipped 0, failed 0" in out
    assert len(fake_agent.calls()) == 1


def test_folder_with_an_existing_valid_cv_only_reports_nothing_to_do(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")
    target = project.target("Alpha", "de")
    target.write_bytes(b"%PDF-1.4 existing tailored CV")

    code = main(
        [*base_args(project, fake_agent, fake_renderer), "--folder", "Alpha", "--language", "de", "--all", "--yes"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to do" in out
    assert "already has a valid DE CV" in out
    assert fake_agent.calls() == []
    assert target.read_bytes() == b"%PDF-1.4 existing tailored CV"
    assert not project.target("Beta", "de").exists()


def test_folder_never_implies_overwrite_or_regenerate_invalid(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    target = project.target("Alpha", "de")
    target.write_bytes(b"junk that is not a pdf")

    code = main(
        [*base_args(project, fake_agent, fake_renderer), "--folder", "Alpha", "--language", "de", "--all", "--yes"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "--regenerate-invalid" in out
    assert fake_agent.calls() == []
    assert target.read_bytes() == b"junk that is not a pdf"


def test_without_a_folder_option_the_scope_stays_every_folder(project, fake_agent, fake_renderer, capsys):
    project.add_app("Alpha")
    project.add_app("Beta")

    code = main([*base_args(project, fake_agent, fake_renderer), "--language", "de", "--all", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Posting folders: 2 usable, 0 ignored, 0 unusable" in out
    assert "Folder filter" not in out
    assert "  1. Alpha" in out and "  2. Beta" in out
