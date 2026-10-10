import os
from pathlib import Path

import pytest

from conftest import NAME_SLUG, target_name
from cv_batch.apps import (
    INVALID,
    MISSING,
    VALID,
    PostingReadError,
    classify_target,
    open_directory,
    read_posting,
    scan,
)
from cv_batch.config import DEFAULT_TARGET_TEMPLATE, ConfigError, TargetNaming, name_slug


def test_target_name_is_exact(tmp_path):
    """The name is built from the template and the identity slug (no literal)."""
    assert target_name("Vertex GmbH", "de") == "CV_AdaLovelace_DE_Vertex GmbH.pdf"
    assert (
        target_name("Initech Bank_2024-0091", "en")
        == "CV_AdaLovelace_EN_Initech Bank_2024-0091.pdf"
    )
    assert target_name("Umbrella-Corp", "de") == "CV_AdaLovelace_DE_Umbrella-Corp.pdf"
    assert target_name("Ünïcode Ø AG", "en") == "CV_AdaLovelace_EN_Ünïcode Ø AG.pdf"
    assert NAME_SLUG == "AdaLovelace"
    assert DEFAULT_TARGET_TEMPLATE == "CV_{name}_{language}_{folder}.pdf"


def test_name_slug_removes_whitespace_only():
    assert name_slug("Ada", "Lovelace") == "AdaLovelace"        # C7: today's names
    assert name_slug("Ada  Maria", "Lovelace\t") == "AdaMariaLovelace"
    assert name_slug("Ada Maria", "von Lovelace") == "AdaMariavonLovelace"
    assert name_slug("Ünïcode", "Ø AG") == "ÜnïcodeØAG"          # Unicode kept
    assert name_slug("Jean-Luc", "Picard") == "Jean-LucPicard"   # hyphens kept


def test_target_naming_renders_template_and_language(tmp_path):
    naming = TargetNaming(name="AdaLovelace", template=DEFAULT_TARGET_TEMPLATE)
    folder = tmp_path / "Ünïcode Ø AG"
    folder.mkdir()
    assert naming.filename(folder.name, "en") == "CV_AdaLovelace_EN_Ünïcode Ø AG.pdf"
    assert naming.path(folder, "de") == folder / "CV_AdaLovelace_DE_Ünïcode Ø AG.pdf"
    other = TargetNaming(name="AdaLovelace", template="cv-{language}-{folder}.pdf")
    assert other.filename("Alpha", "de") == "cv-DE-Alpha.pdf"


def test_target_naming_rejects_an_unknown_field(tmp_path):
    naming = TargetNaming(name="AdaLovelace", template="CV_{surname}_{folder}.pdf")
    with pytest.raises(ConfigError, match="cannot be filled in"):
        naming.filename("Alpha", "de")


def test_scan_ignores_non_postings_and_symlinks(tmp_path):
    apps = tmp_path / "applications"
    (apps / "Alpha").mkdir(parents=True)
    (apps / "Alpha" / "posting.txt").write_text("Java", encoding="utf-8")
    (apps / "Beta").mkdir()
    (apps / "Beta" / "posting.txt").write_text("Java", encoding="utf-8")
    (apps / "empty").mkdir()
    (apps / "empty" / "posting.txt").write_text("", encoding="utf-8")
    (apps / "no-posting-uuid-1234").mkdir()
    (apps / ".staging").mkdir()
    (apps / ".staging" / "posting.txt").write_text("Java", encoding="utf-8")
    (apps / "stray-file.txt").write_text("x", encoding="utf-8")
    os.symlink(apps / "Alpha", apps / "linked")
    (apps / "linked-posting").mkdir()
    (apps / "linked-posting" / "real.txt").write_text("Java", encoding="utf-8")
    os.symlink(apps / "linked-posting" / "real.txt", apps / "linked-posting" / "posting.txt")

    result = scan(apps)
    assert [app.folder for app in result.applications] == ["Alpha", "Beta"]
    reasons = {skip.folder: skip.reason for skip in result.skipped}
    assert reasons["linked"] == "symlinked directory"
    assert reasons["no-posting-uuid-1234"] == "no posting.txt"
    assert reasons["linked-posting"] == "posting.txt is a symlink"
    assert [skip.folder for skip in result.unusable] == ["empty"]


def test_scan_is_sorted_deterministically_with_unicode(tmp_path):
    apps = tmp_path / "applications"
    for name in ["beta", "Alpha", "Ärger GmbH", "zeta", "a-b", "A b"]:
        (apps / name).mkdir(parents=True)
        (apps / name / "posting.txt").write_text("x", encoding="utf-8")
    names = [app.folder for app in scan(apps).applications]
    assert names == sorted(names, key=lambda n: (n.casefold(), n))
    assert names == ["A b", "a-b", "Alpha", "beta", "zeta", "Ärger GmbH"]
    assert names == [app.folder for app in scan(apps).applications]  # stable across runs


def test_classify_target_states(tmp_path):
    missing = tmp_path / "CV_de_missing.pdf"
    assert classify_target(missing).state == MISSING

    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.7 body")
    assert classify_target(good).state == VALID

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    state = classify_target(empty)
    assert state.state == INVALID and "empty" in state.reason

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    assert classify_target(broken).state == INVALID

    directory = tmp_path / "dir.pdf"
    directory.mkdir()
    assert classify_target(directory).state == INVALID

    link = tmp_path / "link.pdf"
    os.symlink(good, link)
    state = classify_target(link)
    assert state.state == INVALID and "symlink" in state.reason


def test_target_path_keeps_folder_basename(tmp_path):
    folder = tmp_path / "Initech Bank_2024-0091"
    folder.mkdir()
    assert TargetNaming(name=NAME_SLUG).path(folder, "de").name == target_name(folder.name, "de")


def test_read_posting_is_bounded(tmp_path):
    posting = tmp_path / "posting.txt"
    posting.write_text("a" * 100, encoding="utf-8")
    assert read_posting(posting, 10).startswith("aaaaaaaaaa")
    assert "truncated" in read_posting(posting, 10)
    assert read_posting(posting, 1000) == "a" * 100


def test_read_posting_refuses_a_swapped_symlink(tmp_path):
    """A posting swapped for a symlink must fail safely (F8)."""
    private = tmp_path / "private.txt"
    private.write_text("PRIVATE", encoding="utf-8")
    folder = tmp_path / "app"
    folder.mkdir()
    posting = folder / "posting.txt"
    posting.write_text("Java", encoding="utf-8")
    assert read_posting(posting, 100) == "Java"

    posting.unlink()
    os.symlink(private, posting)
    with pytest.raises(PostingReadError):
        read_posting(posting, 100)


def test_read_posting_rechecks_the_scanned_inode(tmp_path):
    """The inode recorded at scan time is enforced on the opened file (F8)."""
    folder = tmp_path / "app"
    folder.mkdir()
    posting = folder / "posting.txt"
    posting.write_text("Java", encoding="utf-8")
    info = posting.stat()
    assert read_posting(posting, 100, expected_dev=info.st_dev, expected_ino=info.st_ino) == "Java"

    posting.unlink()
    posting.write_text("swapped", encoding="utf-8")
    with pytest.raises(PostingReadError) as excinfo:
        read_posting(posting, 100, expected_dev=info.st_dev, expected_ino=info.st_ino)
    assert "changed since the scan" in str(excinfo.value)


def test_read_posting_prefers_the_validated_directory_descriptor(tmp_path):
    """A retained folder descriptor keeps the read on the scanned folder (F8)."""
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "posting.txt").write_text("Java", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "posting.txt").write_text("PRIVATE-SECRET", encoding="utf-8")

    fd = open_directory(folder)
    try:
        assert read_posting(folder / "posting.txt", 100, dir_fd=fd) == "Java"
        # the folder path is swapped for a symlink to another folder ...
        folder.rename(tmp_path / "app-real")
        os.symlink(other, folder)
        # ... but the already validated descriptor still reads the real folder
        assert read_posting(folder / "posting.txt", 100, dir_fd=fd) == "Java"
        with pytest.raises(PostingReadError):
            read_posting(folder / "posting.txt", 100)  # no descriptor: follows nothing
    finally:
        os.close(fd)


def test_open_directory_refuses_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(PostingReadError):
        open_directory(link)
