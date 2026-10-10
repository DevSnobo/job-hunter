"""End-to-end tests for the guided posting-capture CLI.

Every test runs in its own temporary directory; the real applications/
folder is never touched.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:  # POSIX only; guards the imports so the file stays importable elsewhere.
    import pty
    import select
except ImportError:  # pragma: no cover - non-POSIX host
    pty = None
    select = None

from posting_entry import cv as cv_module
from posting_entry.store import (
    InvalidInput,
    entry_name,
    normalize_component,
    normalize_source_url,
    save_entry,
)
from posting_entry.ui import Cancelled, Ui

REPO = Path(__file__).resolve().parents[3]
STAGING_PREFIX = ".posting-entry-"


def text_block(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="postings-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.apps = self.tmp / "applications"

    # ---------------------------------------------------------------- helpers
    def run_cli(self, args=(), stdin="", cwd=None, wrapper=False, env=None):
        if wrapper:
            command = [str(REPO / "posting"), *args]
        else:
            command = [sys.executable, "-m", "posting_entry", *args]
        environment = dict(os.environ)
        environment.pop("JOB_ROOT", None)
        if env:
            environment.update(env)
        return subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            cwd=str(cwd or self.tmp),
            env=environment,
            timeout=180,
        )

    def entries(self) -> list[str]:
        if not self.apps.exists():
            return []
        return sorted(p.name for p in self.apps.iterdir())

    def assert_no_staging_leftovers(self) -> None:
        if not self.apps.exists():
            return
        leftovers = [p.name for p in self.apps.iterdir() if p.name.startswith(STAGING_PREFIX)]
        self.assertEqual(leftovers, [], f"staging leftovers: {leftovers}")

    def assert_real_applications_untouched(self, before: list[str]) -> None:
        real = REPO / "applications"
        if real.exists():
            self.assertEqual(sorted(p.name for p in real.iterdir()), before)


# --------------------------------------------------------------------- tests
class HappyPathTests(CliTest):
    def test_single_entry_with_id(self) -> None:
        posting = text_block("Software Engineer", "", "Wir suchen dich.", "Ümlaute & Emoji 🚀")
        script = text_block("Muster GmbH", "A-42", "https://www.linkedin.com/jobs/view/12345/") + posting + text_block(".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.apps / "Muster GmbH_A-42"
        self.assertTrue(entry.is_dir())
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), posting)
        self.assertEqual((entry / "source.txt").read_text(encoding="utf-8"), "https://www.linkedin.com/jobs/view/12345/\n")
        self.assertEqual(self.entries(), ["Muster GmbH_A-42"])
        self.assert_no_staging_leftovers()

    def test_blank_posting_id_uses_company_only(self) -> None:
        script = text_block("Aperture", "", "https://www.linkedin.com/jobs/view/4473226064/", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Aperture"])

    def test_multiline_blank_lines_unicode_and_length_are_preserved(self) -> None:
        body = ["Zeile 1 — Ünïcödé", "", "  eingerückt", "", "日本語テキスト", "", "Emoji: 🚀✅", ""]
        body += [f"Zeile {i} mit ß und Größe" for i in range(200)]
        posting = text_block(*body)
        self.assertGreater(len(posting), 4000)
        script = text_block("Ünïcödé Firma GmbH", "xyz", "https://example.com/job/1") + posting + text_block(".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.apps / "Ünïcödé Firma GmbH_xyz"
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), posting)
        self.assertIn("Zeile 199 mit ß und Größe\n", (entry / "posting.txt").read_text(encoding="utf-8"))

    def test_names_with_spaces_are_kept_readable(self) -> None:
        script = text_block("Deutsche Bahn AG", "155461", "https://www.instaffo.com/jobs/abc", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Deutsche Bahn AG_155461"])
        self.assertIn("Saved", proc.stdout)

    def test_continuous_entry_two_postings(self) -> None:
        first = text_block("Firma Eins", "1", "https://example.com/1", "Erster Text", ".", "y", "y")
        second = text_block("Firma Zwei", "", "https://example.com/2", "Zweiter Text", "", "mit Leerzeile", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], first + second)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Firma Eins_1", "Firma Zwei"])
        self.assertEqual((self.apps / "Firma Zwei" / "posting.txt").read_text(encoding="utf-8"), "Zweiter Text\n\nmit Leerzeile\n")
        self.assertEqual((self.apps / "Firma Zwei" / "source.txt").read_text(encoding="utf-8"), "https://example.com/2\n")
        self.assertEqual(proc.stdout.count("--- review ---"), 2)

    def test_blank_source_is_rejected_then_required(self) -> None:
        # A blank source is refused and re-prompted; a valid link is required.
        script = text_block("Pflicht Quelle", "", "   ", "https://example.com/q", "Nur Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("source URL must not be empty", proc.stderr)
        entry = self.apps / "Pflicht Quelle"
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), "Nur Text\n")
        self.assertEqual((entry / "source.txt").read_text(encoding="utf-8"), "https://example.com/q\n")

    def test_once_mode_captures_single_posting(self) -> None:
        script = text_block("Einmal GmbH", "9", "https://example.com/9", "Text A", ".", "y")
        proc = self.run_cli(["--root", str(self.apps), "--once"], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Einmal GmbH_9"])
        self.assertNotIn("Another posting?", proc.stdout)

    def test_text_file_input(self) -> None:
        posting = text_block("Aus Datei", "", "Ünïcödé 🚀", "")
        path = self.tmp / "posting.txt"
        path.write_text(posting, encoding="utf-8")
        proc = self.run_cli(["--root", str(self.apps), "--company", "Datei Firma", "--posting-id", "", "--source", "https://example.com/datei", "--text-file", str(path), "--once"], "y\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.apps / "Datei Firma" / "posting.txt").read_text(encoding="utf-8"), posting)


class ValidationTests(CliTest):
    def test_empty_company_and_empty_text_are_rejected(self) -> None:
        script = text_block("", "   ", "Gute Firma", "77", "https://example.com/77", ".", ".", "Echter Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("company name must not be empty", proc.stderr)
        self.assertIn("posting text is empty", proc.stderr)
        self.assertEqual((self.apps / "Gute Firma_77" / "posting.txt").read_text(encoding="utf-8"), "Echter Text\n")

    def test_invalid_source_urls_are_rejected(self) -> None:
        script = text_block("URL Firma", "", "notaurl", "www.linkedin.com/jobs/view/1", "ftp://example.com/x", "https://www.linkedin.com/jobs/view/1/", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("must start with http:// or https://", proc.stderr)
        self.assertEqual((self.apps / "URL Firma" / "source.txt").read_text(encoding="utf-8"), "https://www.linkedin.com/jobs/view/1/\n")

    def test_blank_source_preset_fails_without_writing(self) -> None:
        proc = self.run_cli(
            ["--root", str(self.apps), "--company", "Keine Quelle", "--posting-id", "", "--source", "", "--once"],
            "",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("source URL must not be empty", proc.stderr)
        self.assertEqual(self.entries(), [])

    def test_traversal_company_is_refused_and_nothing_escapes_root(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        script = text_block("../../evil", "../etc", "Gut GmbH", "", "https://example.com/x", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("must not start with '.'", proc.stderr)
        self.assertEqual(self.entries(), ["Gut GmbH"])
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["applications", "outside"])

    def test_control_characters_and_dot_names_are_rejected(self) -> None:
        script = text_block("..", "AC\u0001ME", ".hidden", "Gut GmbH", "", "https://example.com/x", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("control character", proc.stderr)
        self.assertIn("must not start with '.'", proc.stderr)
        self.assertIn("company name must not be '..'", proc.stderr)
        self.assertEqual(self.entries(), ["Gut GmbH"])

    def test_stray_line_after_company_becomes_the_posting_id(self) -> None:
        # a company pasted over two lines cannot smuggle a path separator in
        proc = self.run_cli(
            ["--root", str(self.apps)],
            text_block("Acme", "Corp", "", "https://example.com/x", "Text", ".", "y", "n"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.entries(), ["Acme_Corp"])

    def test_invalid_preset_company_fails_without_writing(self) -> None:
        proc = self.run_cli(["--root", str(self.apps), "--company", "../etc", "--once"], "")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("must not start with '.'", proc.stderr)
        self.assertEqual(self.entries(), [])


class CollisionTests(CliTest):
    def seed(self, name: str, text: str = "BESTEHEND\n") -> Path:
        entry = self.apps / name
        entry.mkdir(parents=True)
        (entry / "posting.txt").write_text(text, encoding="utf-8")
        (entry / "source.txt").write_text("https://old.example.com/\n", encoding="utf-8")
        return entry

    def test_skip_keeps_existing_folder(self) -> None:
        self.seed("Globex_155461")
        script = text_block("Globex", "155461", "https://new.example.com/x", "s", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Destination already exists", proc.stdout)
        self.assertIn("directory (posting.txt, source.txt)", proc.stdout)
        entry = self.apps / "Globex_155461"
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), "BESTEHEND\n")
        self.assertEqual((entry / "source.txt").read_text(encoding="utf-8"), "https://old.example.com/\n")
        self.assertEqual(self.entries(), ["Globex_155461"])
        self.assert_no_staging_leftovers()

    def test_other_posting_id_creates_new_folder_and_keeps_old(self) -> None:
        self.seed("Globex_155461")
        script = text_block("Globex", "155461", "https://new.example.com/x", "o", "155331", "Neuer Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Globex_155331", "Globex_155461"])
        self.assertEqual((self.apps / "Globex_155461" / "posting.txt").read_text(encoding="utf-8"), "BESTEHEND\n")
        self.assertEqual((self.apps / "Globex_155331" / "posting.txt").read_text(encoding="utf-8"), "Neuer Text\n")

    def test_preset_posting_id_collision_asks_for_new_id_once(self) -> None:
        # With --posting-id preset, choosing 'o' must prompt for a new id at once
        # instead of instantly re-colliding on the preset value.
        self.seed("Acme Review_A-1")
        script = text_block("o", "A-2", "Neuer Text", ".", "y")
        proc = self.run_cli(
            [
                "--root", str(self.apps),
                "--company", "Acme Review",
                "--posting-id", "A-1",
                "--source", "https://example.com/new",
                "--once",
            ],
            script,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.count("Destination already exists"), 1)
        self.assertEqual(self.entries(), ["Acme Review_A-1", "Acme Review_A-2"])
        self.assertEqual((self.apps / "Acme Review_A-1" / "posting.txt").read_text(encoding="utf-8"), "BESTEHEND\n")
        self.assertEqual((self.apps / "Acme Review_A-2" / "posting.txt").read_text(encoding="utf-8"), "Neuer Text\n")
        self.assertEqual((self.apps / "Acme Review_A-2" / "source.txt").read_text(encoding="utf-8"), "https://example.com/new\n")

    def test_cancel_at_collision_writes_nothing(self) -> None:
        self.seed("Aperture")
        script = text_block("Aperture", "", "https://example.com/x", "c")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 130, proc.stdout + proc.stderr)
        self.assertEqual(self.entries(), ["Aperture"])
        self.assertEqual((self.apps / "Aperture" / "posting.txt").read_text(encoding="utf-8"), "BESTEHEND\n")
        self.assert_no_staging_leftovers()

    def test_symlink_destination_is_refused(self) -> None:
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("keep\n", encoding="utf-8")
        self.apps.mkdir()
        (self.apps / "Symlink Firma").symlink_to(target, target_is_directory=True)
        script = text_block("Symlink Firma", "", "https://example.com/x", "c")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 130, proc.stdout + proc.stderr)
        self.assertIn("symlink", proc.stdout)
        self.assertTrue((self.apps / "Symlink Firma").is_symlink())
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["keep.txt"])
        self.assert_no_staging_leftovers()

    def test_store_refuses_existing_destination(self) -> None:
        self.seed("Direkt")
        with self.assertRaises(Exception):
            save_entry(self.apps, "Direkt", "Neu\n", "https://example.com/")
        self.assertEqual((self.apps / "Direkt" / "posting.txt").read_text(encoding="utf-8"), "BESTEHEND\n")
        self.assert_no_staging_leftovers()


class CancellationTests(CliTest):
    def test_eof_before_text_writes_nothing(self) -> None:
        proc = self.run_cli(["--root", str(self.apps)], text_block("Acme", "1", "https://example.com/x"))
        self.assertEqual(proc.returncode, 130, proc.stdout + proc.stderr)
        self.assertEqual(self.entries(), [])
        self.assert_no_staging_leftovers()

    def test_eof_at_first_prompt_writes_nothing(self) -> None:
        proc = self.run_cli(["--root", str(self.apps)], "")
        self.assertEqual(proc.returncode, 130, proc.stdout + proc.stderr)
        self.assertFalse(self.apps.exists())

    def test_eof_after_text_content_writes_nothing(self) -> None:
        proc = self.run_cli(["--root", str(self.apps)], text_block("Acme", "1", "https://example.com/x", "Text ohne Punkt"))
        self.assertEqual(proc.returncode, 130, proc.stdout + proc.stderr)
        self.assertIn("input ended without the '.' terminator", proc.stderr)
        self.assertEqual(self.entries(), [])

    def test_review_no_discards_entry(self) -> None:
        script = text_block("Verwerfen GmbH", "3", "https://example.com/3", "Text", ".", "n", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Discarded", proc.stdout)
        self.assertEqual(self.entries(), [])

    def test_eof_at_another_posting_stops_normally_after_save(self) -> None:
        # EOF between entries is a normal stop: exit 0, no cancellation message.
        script = text_block("Gespeichert GmbH", "42", "https://example.com/42", "Text", ".", "y")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("cancelled", proc.stderr)
        self.assertNotIn("nothing partial", proc.stderr)
        self.assertIn("End of input after the last saved entry", proc.stdout)
        self.assertEqual(self.entries(), ["Gespeichert GmbH_42"])
        self.assertEqual((self.apps / "Gespeichert GmbH_42" / "posting.txt").read_text(encoding="utf-8"), "Text\n")
        self.assertEqual((self.apps / "Gespeichert GmbH_42" / "source.txt").read_text(encoding="utf-8"), "https://example.com/42\n")
        self.assert_no_staging_leftovers()

    def test_sigint_cancels_cleanly(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "posting_entry", "--root", str(self.apps)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.tmp),
        )
        try:
            proc.stdin.write("Acme GmbH\n")
            proc.stdin.flush()
            time.sleep(1.5)
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertEqual(self.entries(), [])
        self.assert_no_staging_leftovers()


class ClipboardTests(CliTest):
    def test_missing_clipboard_tools_report_a_reason(self) -> None:
        from unittest import mock

        from posting_entry import ui as ui_module

        with mock.patch.dict(os.environ, {"PATH": ""}):
            text, detail = ui_module.read_clipboard()
        self.assertIsNone(text)
        self.assertIn("not installed", detail)

    def test_clipboard_failure_falls_back_to_manual_paste(self) -> None:
        script = text_block("Clip Firma", "", "https://example.com/c", "Manuell eingefügt 🚀", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps), "--clipboard"], script, env={"PATH": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clipboard not read", proc.stderr)
        self.assertEqual((self.apps / "Clip Firma" / "posting.txt").read_text(encoding="utf-8"), "Manuell eingefügt 🚀\n")

    def test_clipboard_request_line_inside_the_paste_falls_back(self) -> None:
        script = text_block("Clip Zwei", "", "https://example.com/c2", ":clipboard", "Handschrift", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script, env={"PATH": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clipboard not read", proc.stderr)
        self.assertEqual((self.apps / "Clip Zwei" / "posting.txt").read_text(encoding="utf-8"), "Handschrift\n")

    # --- success-path checks with fake executables (never the real clipboard)
    def fake_tool(self, bindir: Path, name: str) -> Path:
        bindir.mkdir(parents=True, exist_ok=True)
        script = bindir / name
        script.write_text(
            "#!/bin/sh\n"
            ': > "$ARGS_FILE"\n'
            'for a in "$@"; do printf "%s\\n" "$a" >> "$ARGS_FILE"; done\n'
            'printf "%s" "$CLIP_TEXT"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def read_with_fake_tool(self, tool: str, clip_text: str):
        from unittest import mock

        from posting_entry import ui as ui_module

        bindir = self.tmp / "fake-bin"
        args_file = self.tmp / f"{tool}-args.txt"
        self.fake_tool(bindir, tool)
        with mock.patch.dict(
            os.environ,
            {"PATH": str(bindir), "ARGS_FILE": str(args_file), "CLIP_TEXT": clip_text},
        ):
            text, detail = ui_module.read_clipboard()
        argv = args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else []
        return text, detail, argv

    def test_wl_paste_success_passes_no_extra_arguments(self) -> None:
        text, detail, argv = self.read_with_fake_tool("wl-paste", "wayland clip\n")
        self.assertEqual(text, "wayland clip\n")
        self.assertEqual(detail, "wl-paste")
        self.assertEqual(argv, [])

    def test_xclip_success_passes_correct_arguments(self) -> None:
        text, detail, argv = self.read_with_fake_tool("xclip", "x11 clip\n")
        self.assertEqual(text, "x11 clip\n")
        self.assertEqual(detail, "xclip")
        self.assertEqual(argv, ["-selection", "clipboard", "-o"])

    def test_xsel_success_passes_correct_arguments(self) -> None:
        text, detail, argv = self.read_with_fake_tool("xsel", "xsel clip\n")
        self.assertEqual(text, "xsel clip\n")
        self.assertEqual(detail, "xsel")
        self.assertEqual(argv, ["--clipboard", "--output"])


class ConfigTests(CliTest):
    """The config file of the capture tool (``config/posting.toml``).

    The rules match the tracker's: CLI flag -> environment variable -> config
    file -> built-in default, a missing file is silent, an invalid one is a
    usage error (exit 2) naming the file, and a relative path belongs to the
    job root. Every test builds its own temporary root.
    """

    def mark_job_root(self, root: Path | None = None) -> Path:
        """Give the temp tree the root marker, so the job root is discovered."""
        root = self.tmp if root is None else root
        (root / "sql" / "migrations").mkdir(parents=True, exist_ok=True)
        return root

    def write_config(self, text: str, *, root: Path | None = None) -> Path:
        config = (self.tmp if root is None else root) / "config" / "posting.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")
        return config

    def make_clipboard_tool(self, text: str, name: str = "wl-paste") -> Path:
        """A fake clipboard reader: the real clipboard is never touched."""
        bindir = self.tmp / "fake-bin"
        bindir.mkdir(parents=True, exist_ok=True)
        script = bindir / name
        script.write_text(
            "#!/bin/sh\nprintf \"%s\" " + shlex.quote(text) + "\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return bindir

    def saved_text(self, folder: str) -> str:
        return (self.apps / folder / "posting.txt").read_text(encoding="utf-8")

    # ------------------------------------------------------------ the defaults
    def test_a_missing_config_file_keeps_the_built_in_defaults(self) -> None:
        self.mark_job_root()
        script = text_block("Ohne Konfig", "", "https://example.com/1", "Gepasteter Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Applications directory: {self.apps}", proc.stdout)
        self.assertEqual(self.saved_text("Ohne Konfig"), "Gepasteter Text\n")
        self.assertFalse((self.tmp / "config").exists(), "nothing may be created")

    # ------------------------------------------------------- the file supplies
    def test_a_relative_applications_dir_belongs_to_the_job_root(self) -> None:
        self.mark_job_root()
        self.write_config('applications_dir = "bewerbungen"\n')
        script = text_block("Aus Konfig", "K-1", "https://example.com/2", "Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Applications directory: {self.tmp / 'bewerbungen'}", proc.stdout)
        self.assertEqual(
            (self.tmp / "bewerbungen" / "Aus Konfig_K-1" / "posting.txt").read_text(encoding="utf-8"),
            "Text\n",
        )
        self.assertFalse(self.apps.exists(), "the built-in directory must stay untouched")

    def test_an_absolute_applications_dir_is_used_as_given(self) -> None:
        self.mark_job_root()
        outside = Path(tempfile.mkdtemp(prefix="postings-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        self.write_config(f'applications_dir = "{outside}"\n')
        script = text_block("Absolut", "", "https://example.com/3", "Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((outside / "Absolut" / "posting.txt").read_text(encoding="utf-8"), "Text\n")

    def test_the_config_file_can_be_named_by_the_environment(self) -> None:
        self.mark_job_root()
        elsewhere = self.tmp / "anders.toml"
        elsewhere.write_text('applications_dir = "aus_der_umgebung"\n', encoding="utf-8")
        self.write_config('applications_dir = "ignoriert"\n')
        script = text_block("Umgebung", "", "https://example.com/4", "Text", ".", "y")
        proc = self.run_cli(["--once"], script, env={"POSTING_CONFIG": str(elsewhere)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.tmp / "aus_der_umgebung" / "Umgebung" / "posting.txt").is_file())
        self.assertFalse((self.tmp / "ignoriert").exists())

    def test_unknown_keys_are_ignored(self) -> None:
        self.mark_job_root()
        # The other tools write the same directory with their own keys, and a
        # key this tool does not know must never fail the capture.
        self.write_config('zukunft = [1, 2]\n[section]\nkey = "value"\n')
        script = text_block("Unbekannt", "", "https://example.com/5", "Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.saved_text("Unbekannt"), "Text\n")

    # ------------------------------------------------------------ precedence
    def test_the_root_flag_and_the_job_root_beat_the_config_file(self) -> None:
        self.mark_job_root()
        self.write_config('applications_dir = "bewerbungen"\n')
        with_flag = text_block("Flagge", "", "https://example.com/6", "Text", ".", "y")
        proc = self.run_cli(["--once", "--root", str(self.apps)], with_flag)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.apps / "Flagge" / "posting.txt").is_file())
        with_env = text_block("Umgebungsroot", "", "https://example.com/7", "Text", ".", "y")
        proc = self.run_cli(["--once"], with_env, env={"JOB_ROOT": str(self.tmp)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.apps / "Umgebungsroot" / "posting.txt").is_file())
        self.assertFalse((self.tmp / "bewerbungen").exists())

    # -------------------------------------------- the posting text source
    def test_default_source_from_clipboard_reads_the_clipboard_first(self) -> None:
        self.mark_job_root()
        self.write_config("default_source_from_clipboard = true\n")
        bindir = self.make_clipboard_tool("Clipboard Text\n")
        # No paste lines: the clipboard supplies the text, as with --clipboard.
        script = text_block("Clip Firma", "", "https://example.com/8", "y")
        proc = self.run_cli(["--once"], script, env={"PATH": str(bindir)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Read 15 characters from the clipboard (wl-paste).", proc.stdout)
        self.assertEqual(self.saved_text("Clip Firma"), "Clipboard Text\n")

    def test_the_clipboard_config_falls_back_to_pasting_with_the_warning(self) -> None:
        self.mark_job_root()
        self.write_config("default_source_from_clipboard = true\n")
        script = text_block("Clip Firma", "", "https://example.com/9", "Manuell eingefügt 🚀", ".", "y")
        proc = self.run_cli(["--once"], script, env={"PATH": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clipboard not read", proc.stderr)
        self.assertEqual(self.saved_text("Clip Firma"), "Manuell eingefügt 🚀\n")

    def test_a_text_file_still_wins_over_the_clipboard_config(self) -> None:
        self.mark_job_root()
        self.write_config("default_source_from_clipboard = true\n")
        bindir = self.make_clipboard_tool("Clipboard Text\n")
        text_file = self.tmp / "posting.txt"
        text_file.write_text("Aus der Datei\n", encoding="utf-8")
        script = text_block("Datei Firma", "", "https://example.com/10", "y")
        proc = self.run_cli(["--once", "--text-file", str(text_file)], script,
                            env={"PATH": str(bindir)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("from the clipboard", proc.stdout)
        self.assertEqual(self.saved_text("Datei Firma"), "Aus der Datei\n")

    def test_a_false_config_never_reads_the_clipboard(self) -> None:
        self.mark_job_root()
        self.write_config("default_source_from_clipboard = false\n")
        bindir = self.make_clipboard_tool("Clipboard Text\n")
        script = text_block("Kein Clip", "", "https://example.com/11", "Gepastet", ".", "y")
        proc = self.run_cli(["--once"], script, env={"PATH": str(bindir)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.saved_text("Kein Clip"), "Gepastet\n")

    # --------------------------------------------------------- broken files
    def test_an_invalid_config_file_exits_2_naming_the_file(self) -> None:
        self.mark_job_root()
        config = self.write_config("this is not toml\n")
        script = text_block("Nichts", "", "https://example.com/12", "Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("error:", proc.stderr)
        self.assertIn(str(config), proc.stderr)
        self.assertIn("TOML", proc.stderr)
        # A usage error stops before anything is asked or written.
        self.assertNotIn("Job posting capture", proc.stdout)
        self.assertNotIn("Company name", proc.stdout)
        self.assertFalse(self.apps.exists())

    def test_a_wrong_value_type_exits_2_naming_the_key(self) -> None:
        for line, key in (('offer_cv = "yes"\n', "offer_cv"),
                          ("offer_cv = 1\n", "offer_cv"),
                          ("default_source_from_clipboard = 0\n", "default_source_from_clipboard"),
                          ("applications_dir = 5\n", "applications_dir"),
                          ('applications_dir = ""\n', "applications_dir")):
            with self.subTest(line=line):
                self.mark_job_root()
                config = self.write_config(line)
                script = text_block("Nichts", "", "https://example.com/13", "Text", ".", "y")
                proc = self.run_cli(["--once"], script)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("error:", proc.stderr)
                self.assertIn(str(config), proc.stderr)
                self.assertIn(key, proc.stderr)
                self.assertFalse(self.apps.exists())
                config.unlink()

    def test_importing_the_config_module_does_not_import_prompt_toolkit(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys, posting_entry.config; print('prompt_toolkit' in sys.modules)"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")

    def test_an_unreadable_config_path_exits_2(self) -> None:
        self.mark_job_root()
        config = self.tmp / "config" / "posting.toml"
        config.mkdir(parents=True)  # a directory where the file is expected
        script = text_block("Nichts", "", "https://example.com/14", "Text", ".", "y")
        proc = self.run_cli(["--once"], script)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("error:", proc.stderr)
        self.assertIn(str(config), proc.stderr)


class WrapperTests(CliTest):
    def test_wrapper_works_from_other_cwd_and_keeps_real_data(self) -> None:
        real_apps = REPO / "applications"
        before = sorted(p.name for p in real_apps.iterdir()) if real_apps.exists() else []
        other_cwd = Path(tempfile.mkdtemp(prefix="postings-cwd-"))
        self.addCleanup(shutil.rmtree, other_cwd, True)
        script = text_block("Wrapper GmbH", "W-1", "https://example.com/w", "Text aus Wrapper", ".", "y")
        proc = self.run_cli(["--root", str(self.apps), "--once"], script, cwd=other_cwd, wrapper=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.apps / "Wrapper GmbH_W-1" / "posting.txt").read_text(encoding="utf-8"), "Text aus Wrapper\n")
        self.assert_real_applications_untouched(before)

    def test_existing_intake_folders_are_preserved(self) -> None:
        for name in ("Aperture", "Globex_155461", "Globex_155331"):
            entry = self.apps / name
            entry.mkdir(parents=True, exist_ok=True)
            (entry / "posting.txt").write_text(f"ALT {name}\n", encoding="utf-8")
            (entry / "source.txt").write_text("https://www.linkedin.com/jobs/view/1/\n", encoding="utf-8")
        script = text_block("Neu GmbH", "N-1", "https://example.com/n", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries(), ["Aperture", "Globex_155331", "Globex_155461", "Neu GmbH_N-1"])
        for name in ("Aperture", "Globex_155461", "Globex_155331"):
            self.assertEqual((self.apps / name / "posting.txt").read_text(encoding="utf-8"), f"ALT {name}\n")


class StoreUnitTests(unittest.TestCase):
    def test_normalize_component(self) -> None:
        self.assertEqual(normalize_component("  Acme   Corp  "), "Acme Corp")
        self.assertEqual(normalize_component("Ünïcödé GmbH"), "Ünïcödé GmbH")
        self.assertEqual(normalize_component("a/b\\c"), "a_b_c")
        self.assertEqual(normalize_component("Acme\nCorp"), "Acme Corp")
        for bad in ("", "   ", "..", ".", ".hidden"):
            with self.assertRaises(InvalidInput):
                normalize_component(bad)
        for bad in ("Ac\u0001me", "Acme\u007f", "a" * 201):
            with self.assertRaises(InvalidInput):
                normalize_component(bad)

    def test_entry_name(self) -> None:
        self.assertEqual(entry_name("Acme", ""), "Acme")
        self.assertEqual(entry_name("Acme", None), "Acme")
        self.assertEqual(entry_name("Acme", "1 2"), "Acme_1 2")
        self.assertEqual(entry_name("A/B", "1/2"), "A_B_1_2")
        with self.assertRaises(InvalidInput):
            entry_name("Acme", "../x")

    def test_normalize_source_url(self) -> None:
        self.assertEqual(normalize_source_url("  https://x.example.com/job/1  "), "https://x.example.com/job/1")
        self.assertEqual(normalize_source_url("http://192.168.0.1:8080/job/1"), "http://192.168.0.1:8080/job/1")
        for bad in ("", "   ", "notaurl", "ftp://example.com/x", "https://", "https:///x", "mailto:a@b.com"):
            with self.assertRaises(InvalidInput):
                normalize_source_url(bad)

    def test_failed_promotion_leaves_nothing_behind(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "applications"
            real_rename = os.rename

            def flaky(src, dst):
                raise OSError(5, "simulated promotion failure")

            with mock.patch("posting_entry.store.os.rename", side_effect=flaky) as patched:
                with self.assertRaises(OSError):
                    save_entry(root, "Kaputt", "Text\n", "https://example.com/x")
                self.assertTrue(patched.called)
            self.assertEqual(sorted(p.name for p in root.iterdir()), [])
            self.assertEqual(sorted(p.name for p in root.parent.iterdir()), ["applications"])

    def test_save_entry_writes_utf8_and_no_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "applications"
            dest = save_entry(root, "Firma Ü_1", "Zeile\n\n🚀\n", "https://example.com/a")
            self.assertEqual(Path(dest).name, "Firma Ü_1")
            self.assertEqual((Path(dest) / "posting.txt").read_text(encoding="utf-8"), "Zeile\n\n🚀\n")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["Firma Ü_1"])

    def test_save_entry_requires_and_always_writes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "applications"
            for blank in ("", "   "):
                with self.assertRaises(InvalidInput):
                    save_entry(root, "Ohne Quelle", "Text\n", blank)
            self.assertFalse(root.exists(), "blank source must fail before anything is created")
            dest = save_entry(root, "Mit Quelle", "Text\n", "https://example.com/s")
            self.assertEqual((Path(dest) / "source.txt").read_text(encoding="utf-8"), "https://example.com/s\n")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["Mit Quelle"])


# ------------------------------------------------- user-input colour (orange)
class InputColourTests(unittest.TestCase):
    """The typed text is orange+bold, exactly like in the ``job`` CLI.

    These tests never need a TTY: the injected ``Ui`` streams keep the plain
    reader, and the coloured path is exercised by patching the one function
    that talks to prompt_toolkit.
    """

    def make_ui(self, stdin_text: str = ""):
        ui = Ui(stdin=io.StringIO(stdin_text), stdout=io.StringIO(), stderr=io.StringIO())
        return ui, ui.stdin, ui.stdout, ui.stderr

    def test_answer_style_is_the_shared_cross_tool_contract(self) -> None:
        from posting_entry import ui as ui_module

        # questionary's default ``answer`` class: 256-colour 214 (#FF9D00), bold.
        self.assertEqual(ui_module.ANSWER_STYLE, "fg:#FF9D00 bold")
        # The question text itself must stay in the terminal's regular font.
        self.assertEqual(ui_module.PROMPT_STYLE, "")

    def test_input_style_maps_the_answer_class(self) -> None:
        from posting_entry import ui as ui_module

        self.assertEqual(ui_module.input_style().style_rules, [("answer", "fg:#FF9D00 bold"), ("prompt", "")])

    def test_answer_processor_handles_tuple_and_object_fragments(self) -> None:
        from posting_entry import ui as ui_module

        class Fragment:
            def __init__(self, text: str) -> None:
                self.text = text

        class TransformationInput:
            fragments = [("class:default", "Aa"), Fragment("Bb")]

        transformed = ui_module.answer_processor().apply_transformation(TransformationInput())
        self.assertEqual(transformed.fragments, [("class:answer", "Aa"), ("class:answer", "Bb")])

    def test_interactive_session_is_the_same_strict_gate(self) -> None:
        # The post-save CV question reuses this gate, so injected streams and
        # pipes can never be asked anything.
        ui, _, _, _ = self.make_ui("")
        self.assertFalse(ui.interactive_session())
        self.assertEqual(ui.interactive_session(), ui.coloured_input_allowed())

    def test_injected_streams_never_use_the_coloured_editor(self) -> None:
        ui, _, stdout, _ = self.make_ui("Acme\n")
        self.assertFalse(ui.coloured_input_allowed())
        with mock.patch("posting_entry.ui.read_coloured_line") as reader:
            self.assertEqual(ui.ask_optional("Company name: "), "Acme")
        reader.assert_not_called()
        self.assertEqual(stdout.getvalue(), "Company name: ")

    def test_coloured_path_draws_the_prompt_itself(self) -> None:
        ui, _, stdout, _ = self.make_ui("Acme\n")
        with mock.patch.object(ui, "coloured_input_allowed", return_value=True), mock.patch(
            "posting_entry.ui.read_coloured_line", return_value="Acme"
        ) as reader:
            self.assertEqual(ui.ask_optional("Company name: "), "Acme")
        reader.assert_called_once_with("Company name: ")
        # The editor renders the question, so it must not be written twice.
        self.assertEqual(stdout.getvalue(), "")

    def test_coloured_eof_maps_to_none_and_to_cancelled(self) -> None:
        ui, _, stdout, _ = self.make_ui("")
        with mock.patch.object(ui, "coloured_input_allowed", return_value=True), mock.patch(
            "posting_entry.ui.read_coloured_line", side_effect=EOFError
        ):
            # prompt_toolkit raises EOFError instead of returning an empty line.
            self.assertIsNone(ui.ask_optional("Company name: "))
            self.assertEqual(stdout.getvalue(), "\n")
            with self.assertRaises(Cancelled):
                ui.ask("Company name: ")

    def test_coloured_keyboard_interrupt_cancels(self) -> None:
        ui, _, _, _ = self.make_ui("")
        with mock.patch.object(ui, "coloured_input_allowed", return_value=True), mock.patch(
            "posting_entry.ui.read_coloured_line", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(Cancelled):
                ui.ask_optional("Company name: ")

    def test_coloured_failure_falls_back_to_the_plain_reader(self) -> None:
        for exc in (ImportError("no prompt_toolkit"), RuntimeError("broken terminal")):
            with self.subTest(exc=exc):
                ui, _, stdout, _ = self.make_ui("Acme\n")
                with mock.patch.object(ui, "coloured_input_allowed", return_value=True), mock.patch(
                    "posting_entry.ui.read_coloured_line", side_effect=exc
                ):
                    self.assertEqual(ui.ask_optional("Company name: "), "Acme")
                # The question is re-asked through the plain reader, once.
                self.assertEqual(stdout.getvalue(), "Company name: ")

    def test_multiline_paste_is_never_coloured(self) -> None:
        ui, _, _, _ = self.make_ui("Zeile eins\n\nZeile zwei\n.\n")
        with mock.patch.object(ui, "coloured_input_allowed", return_value=True), mock.patch(
            "posting_entry.ui.read_coloured_line", side_effect=AssertionError("pasted text must stay plain")
        ):
            text, terminated = ui.ask_multiline()
        self.assertEqual(text, "Zeile eins\n\nZeile zwei\n")
        self.assertTrue(terminated)

    def test_importing_the_module_does_not_import_prompt_toolkit(self) -> None:
        # The tool must keep working when prompt_toolkit is missing entirely.
        proc = subprocess.run(
            [sys.executable, "-c", "import sys, posting_entry.ui; print('prompt_toolkit' in sys.modules)"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")

    def test_the_asked_question_texts_are_unchanged(self) -> None:
        from posting_entry import cli as cli_module

        class RecordingUi(Ui):
            def __init__(self, stdin, stdout, stderr):
                super().__init__(stdin=stdin, stdout=stdout, stderr=stderr)
                self.prompts: list[str] = []

            def ask_optional(self, prompt, *, default=""):
                self.prompts.append(prompt)
                return super().ask_optional(prompt, default=default)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "applications"
            args = cli_module.build_parser().parse_args(["--root", str(root)])
            ui = RecordingUi(io.StringIO("Acme\n\nhttps://example.com/x\nText\n.\ny\n"), io.StringIO(), io.StringIO())
            self.assertEqual(cli_module.capture_one(ui, root, args), "saved")
            self.assertEqual(
                ui.prompts,
                [
                    "Company name: ",
                    "Posting id (optional, blank = none): ",
                    "Source URL (LinkedIn, Instaffo, ...; required): ",
                    "Save this posting? [Y/n] ",
                ],
            )
            # The first capture already created applications/Acme, so the same
            # company collides and the menu question below is asked.
            collision = RecordingUi(io.StringIO("Acme\n\nhttps://example.com/y\ns\n"), io.StringIO(), io.StringIO())
            self.assertEqual(cli_module.capture_one(collision, root, args), "skipped")
            self.assertIn("[o]ther posting id / [s]kip this posting / [c]ancel: ", collision.prompts)


class PtyRunMixin(CliTest):
    """Drive the CLI on a real pty, answering questions as they appear.

    Shared by the colour tests and the post-save CV tests: the pty is the only
    honest interactive terminal, and both groups must reach the gate that keeps
    the extra questions away from piped or injected streams.
    """

    PTY_TIMEOUT = 60.0

    def run_in_pty(self, args, answers):
        """Run the CLI on a real pty. ``answers`` is a list of (question, bytes)."""
        env = dict(os.environ)
        # This machine exports NO_COLOR; prompt_toolkit would then skip colour
        # entirely and the check below would be about the test, not the tool.
        env.pop("NO_COLOR", None)
        env.pop("JOB_ROOT", None)
        env["TERM"] = "xterm-256color"
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            os.chdir(str(self.tmp))
            os.execve(sys.executable, [sys.executable, "-m", "posting_entry", *args], env)
        output = b""
        pending = list(answers)
        started = time.monotonic()
        status = None
        try:
            while time.monotonic() - started < self.PTY_TIMEOUT:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:  # the child closed the pty
                        chunk = b""
                    if not chunk:
                        break
                    output += chunk
                    # prompt_toolkit drops the trailing space of a question
                    # until something is typed, so match the text without it.
                    if pending and pending[0][0].rstrip().encode() in output:
                        os.write(fd, pending.pop(0)[1])
        finally:
            os.close(fd)
            _, status = os.waitpid(pid, 0)
        self.assertFalse(pending, f"questions never appeared: {pending!r} in {output!r}")
        return output, os.waitstatus_to_exitcode(status)


@unittest.skipUnless(pty is not None, "requires a POSIX pty")
class PtyColourTests(PtyRunMixin):
    """The real terminal path: the typed text is orange+bold (256-colour 214).

    The pty is the only honest way to check the colour: without a TTY the tool
    deliberately stays on the plain reader.  Each answer is written as soon as
    its question shows up in the terminal output, so the run stays fast.
    """

    def test_typed_text_is_orange_and_questions_stay_uncoloured(self) -> None:
        script = [
            ("Company name: ", b"Acme\r"),
            ("Posting id (optional, blank = none): ", b"A-1\r"),
            ("Source URL (LinkedIn, Instaffo, ...; required): ", b"https://example.com/x\r"),
            ("Posting text: paste it now.", b"Line one of the posting\rLine two\r.\r"),
            ("Save this posting? [Y/n] ", b"y\r"),
        ]
        # ``--no-cv`` keeps this test about the colour: it must never reach the
        # post-save CV question, so its answer script stays byte-identical to
        # the one written before that step existed.
        output, code = self.run_in_pty(["--once", "--no-cv", "--root", str(self.apps)], script)
        self.assertEqual(code, 0, output)
        # Orange (#FF9D00 = 256-colour 214) and bold, exactly like the jobs CLI.
        for typed in (b"Acme", b"A-1", b"https://example.com/x", b"y"):
            self.assertIn(b"38;5;214;1m" + typed, output)
        # The question text, the review block and the pasted text stay plain.
        for plain in (b"Company name", b"Posting id (optional", b"Save this posting?", b"--- review ---", b"Line one"):
            self.assertNotIn(b"38;5;214;1m" + plain, output)
        entry = self.apps / "Acme_A-1"
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), "Line one of the posting\nLine two\n")
        self.assertEqual((entry / "source.txt").read_text(encoding="utf-8"), "https://example.com/x\n")

    def test_ctrl_d_on_the_coloured_prompt_still_cancels(self) -> None:
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], [("Company name: ", b"\x04")])
        self.assertEqual(code, 130, output)
        self.assertFalse(self.apps.exists(), "nothing may be written on EOF")


@unittest.skipUnless(pty is not None, "requires a POSIX pty")
class CvOfferTests(PtyRunMixin):
    """After a saved posting the tool offers the DE and EN tailored CVs once.

    Only a real terminal ever sees that question; the CV work itself is the
    sibling ``cv`` launcher's job, so every test here replaces it with an
    executable shell script that records the arguments it was called with.
    """

    CV_QUESTION = "Generate the tailored CVs now (DE and EN)? [Y/n] "

    def save_answers(self) -> list[tuple[str, bytes]]:
        """The four answers plus 'y' at 'Save this posting?' - one saved entry."""
        return [
            ("Company name: ", b"Acme\r"),
            ("Posting id (optional, blank = none): ", b"A-1\r"),
            ("Source URL (LinkedIn, Instaffo, ...; required): ", b"https://example.com/x\r"),
            ("Posting text: paste it now.", b"Text of the posting\r.\r"),
            ("Save this posting? [Y/n] ", b"y\r"),
        ]

    def calls_file(self) -> Path:
        return self.tmp / "cv-calls.txt"

    def recorded_calls(self) -> list[str]:
        """One line per launcher run: its arguments joined by spaces."""
        path = self.calls_file()
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def make_fake_launcher(self, exit_code: int = 0) -> Path:
        """The executable ``bin/cv`` of the job root (never the real one)."""
        launcher = self.tmp / "bin" / "cv"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.calls_file()}"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        return launcher

    def assert_entry_kept(self) -> None:
        entry = self.apps / "Acme_A-1"
        self.assertEqual((entry / "posting.txt").read_text(encoding="utf-8"), "Text of the posting\n")
        self.assertEqual((entry / "source.txt").read_text(encoding="utf-8"), "https://example.com/x\n")
        self.assert_no_staging_leftovers()

    # ------------------------------------------------------------------ tests
    def test_yes_runs_both_languages_after_the_entry_is_saved(self) -> None:
        self.make_fake_launcher()
        answers = self.save_answers() + [(self.CV_QUESTION, b"y\r")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        # The offer comes after the save, never before it.
        self.assertLess(output.index(b"Saved"), output.index(b"Generate the tailored CVs now"))
        self.assertIn(b"Generating the DE CV for Acme_A-1", output)
        self.assertIn(b"Generating the EN CV for Acme_A-1", output)
        self.assertEqual(
            self.recorded_calls(),
            [
                "--folder Acme_A-1 --language de --all --yes",
                "--folder Acme_A-1 --language en --all --yes",
            ],
        )
        self.assert_entry_kept()

    def test_no_never_runs_the_cv_tool(self) -> None:
        self.make_fake_launcher()
        answers = self.save_answers() + [(self.CV_QUESTION, b"n\r")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"Generating", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_eof_at_the_cv_question_is_a_normal_stop(self) -> None:
        # The entry is already saved, so Ctrl-D here must not cancel the session
        # (and must certainly not report the saved entry as not saved).
        self.make_fake_launcher()
        answers = self.save_answers() + [(self.CV_QUESTION, b"\x04")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"cancelled", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_missing_launcher_warns_and_keeps_the_entry(self) -> None:
        # No fake launcher at all: only a warning plus one retry command per
        # language may happen, and the saved entry stays untouched.
        answers = self.save_answers() + [(self.CV_QUESTION, b"y\r")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertIn(b"missing or not executable", output)
        self.assertIn(b"--folder Acme_A-1 --language de --all --yes", output)
        self.assertIn(b"--folder Acme_A-1 --language en --all --yes", output)
        self.assert_entry_kept()

    def test_failing_launcher_warns_and_does_not_change_the_exit_code(self) -> None:
        self.make_fake_launcher(exit_code=1)
        answers = self.save_answers() + [(self.CV_QUESTION, b"y\r")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertIn(b"the DE CV generation failed (exit 1)", output)
        self.assertIn(b"the EN CV generation failed (exit 1)", output)
        self.assertIn(b"retry with:", output)
        self.assertEqual(len(self.recorded_calls()), 2, "both languages are attempted anyway")
        self.assert_entry_kept()

    def test_offer_cv_false_in_the_config_never_asks(self) -> None:
        """``offer_cv = false`` behaves exactly like ``--no-cv``."""
        self.make_fake_launcher()
        config = self.tmp / "config" / "posting.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("offer_cv = false\n", encoding="utf-8")
        # The answers stop at "Save this posting?": the CV question must not come.
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], self.save_answers())
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"Generate the tailored CVs now", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_no_cv_still_wins_over_an_offering_config_file(self) -> None:
        """The flag beats the file, so the offer stays off either way."""
        self.make_fake_launcher()
        config = self.tmp / "config" / "posting.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("offer_cv = true\n", encoding="utf-8")
        output, code = self.run_in_pty(
            ["--once", "--no-cv", "--root", str(self.apps)], self.save_answers()
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"Generate the tailored CVs now", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_an_offering_config_file_still_asks_the_cv_question(self) -> None:
        self.make_fake_launcher()
        config = self.tmp / "config" / "posting.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("offer_cv = true\n", encoding="utf-8")
        answers = self.save_answers() + [(self.CV_QUESTION, b"y\r")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.recorded_calls()), 2)
        self.assert_entry_kept()

    def test_ctrl_c_at_the_cv_question_skips_without_cancelling(self) -> None:
        # The entry is on disk already: Ctrl-C here may not end the session with
        # "the current entry was not saved".
        self.make_fake_launcher()
        answers = self.save_answers() + [(self.CV_QUESTION, b"\x03")]
        output, code = self.run_in_pty(["--once", "--root", str(self.apps)], answers)
        self.assertEqual(code, 0, output)
        self.assertIn(b"CV generation skipped.", output)
        self.assertNotIn(b"cancelled", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_no_cv_flag_never_asks(self) -> None:
        self.make_fake_launcher()
        output, code = self.run_in_pty(["--once", "--no-cv", "--root", str(self.apps)], self.save_answers())
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"Generate the tailored CVs", output)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()

    def test_piped_run_never_asks_even_with_a_launcher_present(self) -> None:
        # A scripted capture has nobody to answer, so the question must not be
        # printed and the launcher below the job root must stay unused.
        self.make_fake_launcher()
        script = text_block("Acme", "A-1", "https://example.com/x", "Text of the posting", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Generate the tailored CVs", proc.stdout)
        self.assertEqual(self.recorded_calls(), [])
        self.assert_entry_kept()


class CvModuleTests(unittest.TestCase):
    """Unit tests for the stdlib-only wrapper around the repository's ``cv`` tool."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cv-module-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.apps = self.tmp / "applications"

    def make_launcher(self, mode: int = 0o755) -> Path:
        """The ``bin/cv`` launcher of the job root (the post-refactor form)."""
        launcher = self.tmp / "bin" / "cv"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(mode)
        return launcher

    def make_ui(self):
        return Ui(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())

    def test_argv_shape_is_the_documented_command_line(self) -> None:
        self.assertEqual(cv_module.CV_LANGUAGES, ("de", "en"))
        self.assertEqual(
            cv_module.cv_argv(Path("/jobs/bin/cv"), "Acme_A-1", "de"),
            ["/jobs/bin/cv", "--folder", "Acme_A-1", "--language", "de", "--all", "--yes"],
        )
        self.assertEqual(
            cv_module.retry_command(Path("/jobs/bin/cv"), "Acme_A-1", "en"),
            "/jobs/bin/cv --folder Acme_A-1 --language en --all --yes",
        )

    def test_launcher_discovery_needs_an_executable_file(self) -> None:
        self.assertIsNone(cv_module.launcher_for(self.apps), "no launcher below the job root")
        launcher = self.make_launcher(0o644)
        self.assertIsNone(cv_module.launcher_for(self.apps), "a plain file is not runnable")
        launcher.chmod(0o755)
        self.assertEqual(cv_module.launcher_for(self.apps), launcher)
        launcher.unlink()
        launcher.mkdir()
        self.assertIsNone(cv_module.launcher_for(self.apps), "a directory is not a launcher")

    def test_only_bin_cv_is_accepted_as_the_launcher(self) -> None:
        """``<job root>/bin/cv`` is the one launcher; a root-level ``cv`` is not."""
        stray = self.tmp / "cv"
        stray.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stray.chmod(0o755)
        self.assertIsNone(
            cv_module.launcher_for(self.apps),
            "a root-level cv entry is not a launcher",
        )
        current = self.make_launcher()
        self.assertEqual(cv_module.launcher_for(self.apps), current)

    def test_generate_runs_both_languages_from_the_job_root(self) -> None:
        launcher = self.make_launcher()
        ui = self.make_ui()
        seen: list[tuple[list[str], str]] = []

        def runner(argv, cwd=None):
            seen.append((argv, cwd))
            return subprocess.CompletedProcess(argv, 0)

        cv_module.generate(ui, self.apps, "Acme_A-1", runner=runner)
        self.assertEqual(
            [argv for argv, _ in seen],
            [
                cv_module.cv_argv(launcher, "Acme_A-1", "de"),
                cv_module.cv_argv(launcher, "Acme_A-1", "en"),
            ],
        )
        # The job root, not the applications directory: the launcher resolves
        # its own project from there.
        self.assertEqual([cwd for _, cwd in seen], [str(self.tmp), str(self.tmp)])
        self.assertEqual(ui.stderr.getvalue(), "", "a clean run says nothing")

    def test_nonzero_exit_is_recorded_as_a_warning_only(self) -> None:
        launcher = self.make_launcher()
        ui = self.make_ui()

        def runner(argv, cwd=None):
            code = 1 if cv_module.cv_argv(launcher, "Acme_A-1", "de") == argv else 0
            return subprocess.CompletedProcess(argv, code)

        cv_module.generate(ui, self.apps, "Acme_A-1", runner=runner)
        stderr = ui.stderr.getvalue()
        self.assertIn("the DE CV generation failed (exit 1)", stderr)
        self.assertIn(f"retry with: {launcher} --folder Acme_A-1 --language de --all --yes", stderr)
        self.assertNotIn("EN CV generation failed", stderr)

    def test_missing_launcher_warns_once_plus_one_retry_per_language(self) -> None:
        ui = self.make_ui()

        def runner(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("nothing may be run without a launcher")

        cv_module.generate(ui, self.apps, "Acme_A-1", runner=runner)
        lines = ui.stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("missing or not executable", lines[0])
        self.assertIn("--folder Acme_A-1 --language de --all --yes", lines[1])
        self.assertIn("--folder Acme_A-1 --language en --all --yes", lines[2])
        self.assertEqual(ui.stdout.getvalue(), "")

    def test_unusable_launcher_is_reported_like_a_failure(self) -> None:
        self.make_launcher()
        ui = self.make_ui()

        def runner(argv, cwd=None):
            raise OSError(13, "Permission denied")

        cv_module.generate(ui, self.apps, "Acme_A-1", runner=runner)
        self.assertIn("the DE CV generation could not start", ui.stderr.getvalue())

    def test_interrupted_run_warns_and_stops_instead_of_cancelling(self) -> None:
        launcher = self.make_launcher()
        ui = self.make_ui()
        seen: list[list[str]] = []

        def runner(argv, cwd=None):
            seen.append(argv)
            raise KeyboardInterrupt

        cv_module.generate(ui, self.apps, "Acme_A-1", runner=runner)
        self.assertIn("the DE CV generation was interrupted", ui.stderr.getvalue())
        self.assertEqual(seen, [cv_module.cv_argv(launcher, "Acme_A-1", "de")])

    def test_importing_the_module_does_not_import_prompt_toolkit(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", "import sys, posting_entry.cv; print('prompt_toolkit' in sys.modules)"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")


class PipedInputTests(CliTest):
    """Without a TTY nothing is coloured: the plain reader is byte-identical."""

    def test_piped_run_emits_no_escape_sequences(self) -> None:
        script = text_block("Pipe GmbH", "P-1", "https://example.com/p", "Text", ".", "y", "n")
        proc = self.run_cli(["--root", str(self.apps)], script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("\x1b", proc.stdout)
        self.assertNotIn("\x1b", proc.stderr)
        self.assertEqual((self.apps / "Pipe GmbH_P-1" / "posting.txt").read_text(encoding="utf-8"), "Text\n")


if __name__ == "__main__":
    unittest.main()
