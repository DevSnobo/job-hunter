"""The aggregate chart bundle: generator-ready data, a deterministic SVG, a PNG.

The chart is drawn from the counted aggregate only, so these tests pin two things:
the data really can be plugged into a chart tool, and no part of the bundle (JSON,
SVG, or rasterized PNG) can carry identifying text or a local path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import time
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from helpers import read_edges

from job_cli import reporting, sankey_chart

SVG_NS = "http://www.w3.org/2000/svg"
PRIVATE_DIR_NAME = "Retest Systems 2026-08-01 private"
PRIVATE_TOKENS = ("Acme", "Beta", "Backend", "Platform", "Retest", "Systems",
                  "2026-08-01", "private")


def make_story(run, log_ok, new_round):
    """One rejected application after two interviews, one accepted after four."""
    run("add", "--company", "Acme GmbH", "--role", "Backend", "--submitted-on", "2026-01-05")
    run("add", "--company", "Beta AG", "--role", "Platform", "--submitted-on", "2026-01-09")
    new_round("Acme GmbH", "2026-01-06", "screening", "recruiter_screen", "phone")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    new_round("Acme GmbH", "2026-01-08", "interview", "general", "video")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-10", "--round", "2")
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    for day in ("2026-01-15", "2026-01-20", "2026-01-25", "2026-01-30"):
        log_ok("Beta AG", "--event", "completed", "--date", day, "--round", "new",
               "--phase", "interview", "--round-type", "general", "--format", "video")
    log_ok("Beta AG", "--event", "offer_received", "--date", "2026-02-01")
    log_ok("Beta AG", "--event", "offer_accepted", "--date", "2026-02-03")


def bundle(run, out, *extra: str):
    """Run one export and return the parsed metadata."""
    result = run("export", "sankey", "--output", str(out), *extra)
    assert result.ok, result.output
    return json.loads((out / "sankey-metadata.json").read_text(encoding="utf-8"))


def edges_of(out: Path) -> dict[tuple[str, str], int]:
    with (out / "sankey-edges.csv").open(encoding="utf-8", newline="") as handle:
        return {(row["source"], row["target"]): int(row["value"])
                for row in csv.DictReader(handle)}


def png_strings(path: Path) -> str:
    """Printable ASCII runs inside the PNG, where a leaked path or name would live."""
    return " ".join(chunk.decode("ascii")
                    for chunk in re.findall(rb"[ -~]{4,}", path.read_bytes()))


# --------------------------------------------------------------------------
# the bundle
# --------------------------------------------------------------------------
def test_one_export_writes_data_and_image_into_one_timestamped_directory(
        run, new_app, log_ok, new_round, tmp_path):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Beta AG", "Platform", "2026-01-09")
    make_story(run, log_ok, new_round)
    out = tmp_path / "bundle"
    meta = bundle(run, out)

    for name in ("sankey-edges.csv", "sankey-metadata.json", "sankey-node-link.json",
                 "sankey.svg", "sankey.png"):
        assert (out / name).is_file(), name
    assert meta["artifacts"] == ["sankey-edges.csv", "sankey-metadata.json",
                                 "sankey-node-link.json", "sankey.svg", "sankey.png"]

    chart = meta["chart"]
    assert chart["node_link_file"] == "sankey-node-link.json"
    assert chart["png_file"] == "sankey.png"
    assert chart["png_tool"]
    assert chart["svg_sha256"] == hashlib.sha256((out / "sankey.svg").read_bytes()).hexdigest()
    assert chart["canvas"]["width"] >= sankey_chart.CANVAS_WIDTH
    assert chart["canvas"]["height"] >= sankey_chart.MIN_CANVAS_HEIGHT
    assert (out / "sankey.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    # the frozen replay contract is untouched by the chart files
    replayed = json.loads(run("export", "replay", str(out / "sankey-metadata.json"),
                              "--json").output)
    assert replayed["frozen_reproduced"] is True
    assert replayed["pair_consistent"] is True
    assert replayed["executed_metadata_sql"] is False


def test_no_chart_keeps_the_original_pair_only(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "edges-only"
    meta = bundle(run, out, "--no-chart")
    assert sorted(path.name for path in out.iterdir()) == ["sankey-edges.csv",
                                                           "sankey-metadata.json"]
    assert meta["artifacts"] == ["sankey-edges.csv", "sankey-metadata.json"]
    assert meta["chart"] is None


def test_no_png_still_writes_the_node_link_data_and_the_svg(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "no-png"
    meta = bundle(run, out, "--no-png")
    assert (out / "sankey.svg").is_file()
    assert not (out / "sankey.png").exists()
    assert meta["chart"]["png_file"] is None
    assert "--no-png" in meta["chart"]["png_note"]
    assert meta["chart"]["svg_sha256"]


def test_refuses_to_overwrite_a_bundle_whose_only_file_is_the_image(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "occupied"
    out.mkdir()
    (out / "sankey.svg").write_text("<svg/>", encoding="utf-8")
    result = run("export", "sankey", "--output", str(out))
    assert result.exit_code == 2
    assert "refusing" in result.output
    assert (out / "sankey.svg").read_text(encoding="utf-8") == "<svg/>"
    assert not (out / "sankey-edges.csv").exists()


def test_a_missing_converter_costs_the_png_but_never_the_data(
        run, new_app, tmp_path, monkeypatch):
    new_app()
    monkeypatch.setattr(sankey_chart, "RASTERIZERS", ())
    out = tmp_path / "no-converter"
    meta = bundle(run, out)
    assert (out / "sankey.svg").is_file()
    assert not (out / "sankey.png").exists()
    assert meta["chart"]["png_file"] is None
    assert "no SVG converter" in meta["chart"]["png_note"]
    assert (out / "sankey-node-link.json").is_file()


def test_a_failing_converter_leaves_no_temporary_file_behind(
        run, new_app, tmp_path, monkeypatch):
    new_app()
    monkeypatch.setattr(sankey_chart, "RASTERIZERS", (("false", ("{svg}", "{png}")),))
    out = tmp_path / "bad-converter"
    meta = bundle(run, out)
    assert not (out / "sankey.png").exists()
    assert "failed (exit 1)" in meta["chart"]["png_note"]
    assert not list(out.glob("*.tmp-*")) and not list(out.glob("*-stage-*"))
    assert sorted(path.suffix for path in out.iterdir()) == sorted(
        [".csv", ".json", ".json", ".svg"])


def test_a_converter_that_needs_a_real_png_extension_still_works(
        run, new_app, tmp_path, monkeypatch):
    """Inkscape decides the output format from the file name and drops a staged name like
    sankey.png.tmp-1234 (it would even write the final name directly). The staged files
    therefore keep their real extensions."""
    new_app()
    script = ('case "$1" in *.png) printf PNG > "$1" ;;'
              ' *) echo "Ignoring extension of export filename" >&2; exit 0 ;; esac')
    monkeypatch.setattr(sankey_chart, "RASTERIZERS", (("sh", ("-c", script, "sh", "{png}")),))
    out = tmp_path / "extension"
    meta = bundle(run, out)
    assert (out / "sankey.png").is_file()
    assert meta["chart"]["png_tool"] == "sh"
    assert meta["chart"]["png_file"] == "sankey.png"
    assert not list(out.glob("*-stage-*"))


def test_a_non_utf8_metadata_file_is_a_usage_error_not_a_traceback(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "binary"
    bundle(run, out)
    (out / "sankey-metadata.json").write_bytes(b"\xff\xfe{not json at all}")
    result = run("export", "replay", str(out / "sankey-metadata.json"))
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "cannot read" in result.output


def test_a_backslash_path_never_survives_the_converter_note(
        run, new_app, tmp_path, monkeypatch):
    new_app()
    windows = "cannot open C:\\Users\\example\\Job Search 2026 Q4 - CONFIDENTIAL\\bundle\\x.png"
    monkeypatch.setattr(sankey_chart, "RASTERIZERS",
                        (("sh", ("-c", f'echo "{windows}" >&2; exit 2')),))
    out = tmp_path / "Job Search 2026 Q4 - CONFIDENTIAL"
    assert run("export", "sankey", "--output", str(out)).ok
    note = json.loads((out / "sankey-metadata.json").read_text(encoding="utf-8"))
    note = note["chart"]["png_note"]
    assert "exit 2" in note
    for token in ("confidential", "CONFIDENTIAL", "Job", "Search", "example", "Users", "bundle"):
        assert token not in note, (token, note)


def test_an_empty_cohort_bundle_replays_cleanly(run, tmp_path):
    out = tmp_path / "empty-replay"
    bundle(run, out)
    data = json.loads(run("export", "replay", str(out / "sankey-metadata.json"), "--json").output)
    assert data["frozen_cohort_size"] == 0
    assert data["frozen_group_count"] == 0
    assert data["frozen_reproduced"] is True
    assert data["reproduced"] is True


def test_a_single_application_is_not_described_as_plural(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "one"
    bundle(run, out)
    svg = (out / "sankey.svg").read_text(encoding="utf-8")
    assert "1 application" in svg and "1 applications" not in svg


def test_an_empty_cohort_still_produces_a_valid_bundle(run, tmp_path):
    out = tmp_path / "empty"
    meta = bundle(run, out)
    assert meta["cohort_size"] == 0
    assert edges_of(out) == {}
    node_link = json.loads((out / "sankey-node-link.json").read_text(encoding="utf-8"))
    assert node_link["nodes"] == [] and node_link["links"] == []
    assert node_link["total_flow"] == 0
    root = ElementTree.fromstring((out / "sankey.svg").read_text(encoding="utf-8"))
    assert root.tag == f"{{{SVG_NS}}}svg"
    assert "No applications in this cohort" in (out / "sankey.svg").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the data really is generator-ready
# --------------------------------------------------------------------------
def test_node_link_data_maps_onto_a_sankey_generator(run, new_app, log_ok, new_round, tmp_path):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Beta AG", "Platform", "2026-01-09")
    make_story(run, log_ok, new_round)
    out = tmp_path / "generator"
    meta = bundle(run, out)
    data = json.loads((out / "sankey-node-link.json").read_text(encoding="utf-8"))
    edges = edges_of(out)

    assert data["format"] == reporting.NODE_LINK_FORMAT
    assert data["cohort_size"] == meta["cohort_size"] == 2
    assert data["edges_file"] == "sankey-edges.csv"
    assert set(data["generator_hints"]) >= {"plotly", "d3_sankey", "echarts"}

    nodes = data["nodes"]
    assert [node["index"] for node in nodes] == list(range(len(nodes)))
    # plotly wants label lists plus integer link endpoints
    labels = [node["label"] for node in nodes]
    assert labels[0] == "All applications"
    assert len(set(node["id"] for node in nodes)) == len(nodes)

    by_id = {node["id"]: node for node in nodes}
    for link in data["links"]:
        assert nodes[link["source"]]["id"] == link["source_id"]
        assert nodes[link["target"]]["id"] == link["target_id"]
        assert edges[(link["source_id"], link["target_id"])] == link["value"]
        # every drawn flow is a forward step, so a generator can order by stage
        assert by_id[link["source_id"]]["stage"] <= by_id[link["target_id"]]["stage"]

    assert len(data["links"]) == len(edges)
    assert sum(node["total"] for node in nodes if node["column"] == 0) == data["total_flow"] == 2
    assert sum(link["value"] for link in data["links"]) == sum(edges.values())


def test_the_chart_drops_unreachable_nodes_and_keeps_the_declared_order(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "order"
    bundle(run, out)
    data = json.loads((out / "sankey-node-link.json").read_text(encoding="utf-8"))
    assert [node["id"] for node in data["nodes"]] == ["all_applications", "end_in_progress"]
    assert [node["stage"] for node in data["nodes"]] == [0, 7]
    assert [node["column"] for node in data["nodes"]] == [0, 1]


def test_ports_follow_the_partner_positions_so_bands_stay_flat():
    """A link leaves and arrives near the same height, which is what keeps a chart readable."""
    chart = sankey_chart.build_chart(chart_labels(), dict(RICH_EDGES))
    center = {node.id: node.y + node.height / 2.0 for node in chart.nodes}
    for node in chart.nodes:
        outgoing = sorted((link for link in chart.links if link.source == node.id),
                          key=lambda link: link.y0)
        assert [center[link.target] for link in outgoing] == sorted(
            center[link.target] for link in outgoing), node.id
        incoming = sorted((link for link in chart.links if link.target == node.id),
                          key=lambda link: link.y1)
        assert [center[link.source] for link in incoming] == sorted(
            center[link.source] for link in incoming), node.id


def test_build_chart_ignores_impossible_flows():
    labels = [("all_applications", "All applications"), ("end_rejected", "Rejected")]
    chart = sankey_chart.build_chart(labels, {
        ("all_applications", "end_rejected"): 3,
        ("all_applications", "all_applications"): 4,   # a self-edge is never drawn
        ("all_applications", "ghost"): 5,              # an unknown node is dropped
        ("end_rejected", "all_applications"): 0,       # a zero flow carries nothing
    })
    assert [(link.source, link.target, link.value) for link in chart.links] == [
        ("all_applications", "end_rejected", 3)]
    assert [node.total for node in chart.nodes] == [3, 3]
    assert sankey_chart.build_chart(labels, {}).nodes == []


# --------------------------------------------------------------------------
# the image
# --------------------------------------------------------------------------
def test_the_svg_is_self_contained_and_reproducible():
    labels = [("all_applications", "All applications"), ("interview_1", "Interview 1"),
              ("end_rejected", "Rejected"), ("end_in_progress", "Still in progress")]
    edges = {("all_applications", "interview_1"): 7,
             ("all_applications", "end_in_progress"): 2,
             ("interview_1", "end_rejected"): 7}
    first = sankey_chart.render_svg(sankey_chart.build_chart(labels, edges),
                                    title="T", subtitle="S", note="N")
    second = sankey_chart.render_svg(sankey_chart.build_chart(labels, edges),
                                     title="T", subtitle="S", note="N")
    assert first == second  # deterministic bytes for the same aggregate

    root = ElementTree.fromstring(first)
    assert root.tag == f"{{{SVG_NS}}}svg"
    assert root.get("viewBox")
    for element in root.iter():
        assert element.tag.split("}")[-1] not in ("script", "image", "foreignObject")
        for attribute, value in element.attrib.items():
            if attribute.split("}")[-1] == "href" or attribute.endswith("href"):
                pytest.fail(f"external reference in the chart: {attribute}={value}")
    text = first
    assert text.count("http") == 1  # only the SVG namespace
    for label in ("All applications (9)", "Interview 1 (7)", "Rejected (7)",
                  "Still in progress (2)"):
        assert label in text


def test_the_svg_marks_the_outcome_columns_and_edges_with_their_counts(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "svg"
    bundle(run, out)
    text = (out / "sankey.svg").read_text(encoding="utf-8")
    assert "All applications (1)" in text
    assert "Still in progress (1)" in text
    assert "all_applications to end_in_progress: 1" in text   # tooltip on the flow band
    assert text.startswith("<svg xmlns=") and text.rstrip().endswith("</svg>")


def test_a_converter_note_never_repeats_a_local_path(run, new_app, tmp_path, monkeypatch):
    """The rasterizer note lands in the shareable metadata, so it is masked, not trusted.

    Every branch is exercised: a converter that exits non-zero and prints a path that
    contains spaces, one that cannot be executed at all, and one that times out (whose
    exception text carries the whole command line, including the staged PNG path).
    """
    new_app()
    private = "Retest Systems 2026-08-01 private"

    def export(name: str) -> str:
        out = tmp_path / name / private  # the mask has to survive a space in the path
        result = run("export", "sankey", "--output", str(out))
        assert result.ok, result.output
        note = json.loads((out / "sankey-metadata.json").read_text(encoding="utf-8"))
        return note["chart"]["png_note"]

    # 1. non-zero exit, and the converter prints the staged chart path
    monkeypatch.setattr(sankey_chart, "RASTERIZERS",
                        (("sh", ("-c", "echo cannot open {svg} >&2; exit 3")),))
    note = export("prints-a-path")
    assert "exit 3" in note
    for token in ("Retest", "Systems", "private", "2026-08-01", str(tmp_path), "sankey"):
        assert token not in note, (token, note)

    # 2. the converter exists but cannot be executed at all
    broken = tmp_path / "not-really-a-program"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    monkeypatch.setattr(sankey_chart, "RASTERIZERS", (("rsvg-convert", ("{svg}",)),))
    monkeypatch.setattr(sankey_chart.shutil, "which", lambda _name: str(broken))
    note = export("cannot-exec")
    assert "could not run" in note
    for token in ("Retest", "Systems", "private", str(tmp_path), "not-really-a-program"):
        assert token not in note, (token, note)

    # 3. a converter that hangs, with a helper holding the pipes open: the wait has to
    #    stay bounded and the note must not repeat the command line
    monkeypatch.undo()  # case 2 replaced shutil.which; start from the real search again
    monkeypatch.setattr(sankey_chart, "RASTERIZE_TIMEOUT", 0.5)
    monkeypatch.setattr(sankey_chart, "RASTERIZERS",
                        (("sh", ("-c", "(sleep 60 &) ; sleep 60")),))
    started = time.monotonic()
    note = export("timeout")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"the timeout did not bound the wait ({elapsed:.1f}s)"
    assert "did not finish" in note
    # the converter name itself is fine in the note; only the paths are not
    for token in ("Retest", "Systems", "private", str(tmp_path), "sankey"):
        assert token not in note, (token, note)


def test_a_broken_converter_does_not_stop_the_next_one(run, new_app, tmp_path, monkeypatch):
    new_app()
    monkeypatch.setattr(sankey_chart, "RASTERIZERS", (
        ("definitely-broken", ("{svg}", "{png}")),
        ("rsvg-convert", ("--width", "{width}", "--output", "{png}", "{svg}")),
    ))
    out = tmp_path / "second-converter"
    meta = bundle(run, out)
    if sankey_chart.shutil.which("rsvg-convert") is None:  # pragma: no cover
        pytest.skip("rsvg-convert is not installed")
    assert (out / "sankey.png").is_file()
    assert meta["chart"]["png_tool"] == "rsvg-convert"


# --------------------------------------------------------------------------
# side labels stay inside the canvas
# --------------------------------------------------------------------------
RICH_EDGES = {
    ("all_applications", "interview_1"): 28,
    ("all_applications", "end_in_progress"): 8,
    ("all_applications", "end_inactive"): 5,
    ("all_applications", "end_rejected"): 1,
    ("interview_1", "interview_2"): 14,
    ("interview_1", "end_rejected"): 9,
    ("interview_1", "end_in_progress"): 5,
    ("interview_2", "interview_3plus"): 6,
    ("interview_2", "end_rejected"): 5,
    ("interview_2", "end_withdrawn"): 3,
    ("interview_3plus", "offer_received"): 4,
    ("interview_3plus", "end_rejected"): 2,
    ("offer_received", "end_accepted"): 2,
    ("offer_received", "end_offer_declined"): 1,
    ("offer_received", "end_position_closed"): 1,
}


def chart_labels(overrides: dict | None = None) -> list[tuple[str, str]]:
    overrides = overrides or {}
    return [(node_id, overrides.get(node_id, sankey_chart.SHORT_LABELS.get(node_id, label)))
            for node_id, label in reporting.SANKEY_NODES.items()]


def test_a_huge_cohort_is_capped_instead_of_growing_without_end():
    labels = [("all_applications", "All applications"),
              ("end_in_progress", "Still in progress"), ("end_rejected", "Rejected")]
    for total in (200, 1900, 50000):
        chart = sankey_chart.build_chart(labels, {
            ("all_applications", "end_in_progress"): total - 1,
            ("all_applications", "end_rejected"): 1})
        assert chart.height <= sankey_chart.MAX_CANVAS_HEIGHT + 8, total
        assert chart.height == int(chart.height)
        assert chart.width == int(chart.width)


def test_side_labels_never_run_off_the_canvas():
    chart = sankey_chart.build_chart(chart_labels(), dict(RICH_EDGES))
    last_column = max(node.column for node in chart.nodes)
    checked = 0
    for node in chart.nodes:
        text = f"{node.label} ({node.total})"
        if node.column == 0:
            # a deliberately conservative 6 px per character at 13 px
            assert node.x - sankey_chart.SIDE_GAP >= len(text) * 6
            checked += 1
        elif node.column == last_column:
            room = chart.width - (node.x + sankey_chart.NODE_WIDTH + sankey_chart.SIDE_GAP)
            assert room >= len(text) * 6, (node.label, room)
            checked += 1
    assert checked >= 2


def test_a_very_long_label_grows_the_canvas_instead_of_being_clipped():
    long_label = "No response / presumed inactive" + " after several follow-ups" * 3
    chart = sankey_chart.build_chart(
        chart_labels({"end_inactive": long_label}),
        {("all_applications", "end_inactive"): 5})
    assert chart.width > sankey_chart.CANVAS_WIDTH
    node = chart.nodes[-1]
    room = chart.width - (node.x + sankey_chart.NODE_WIDTH + sankey_chart.SIDE_GAP)
    assert room >= len(f"{long_label} (5)") * 6


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------
def test_no_artifact_in_the_bundle_leaks_a_name_a_path_or_the_directory(
        run, new_app, log_ok, new_round, tmp_path):
    new_app("Acme GmbH", "Backend", "2026-01-05")
    new_app("Beta AG", "Platform", "2026-01-09")
    make_story(run, log_ok, new_round)
    out = tmp_path / PRIVATE_DIR_NAME
    bundle(run, out)

    texts = {name: (out / name).read_text(encoding="utf-8")
             for name in ("sankey-edges.csv", "sankey-metadata.json",
                          "sankey-node-link.json", "sankey.svg")}
    png = out / "sankey.png"
    if png.exists():
        texts["sankey.png"] = png_strings(png)
    for name, text in texts.items():
        for token in PRIVATE_TOKENS + (PRIVATE_DIR_NAME, str(out), str(tmp_path)):
            assert token not in text, (name, token)
        assert "/home/" not in text and "file://" not in text


def test_the_shareable_chart_text_is_the_fixed_wording_only(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "wording"
    bundle(run, out)
    svg = (out / "sankey.svg").read_text(encoding="utf-8")
    assert reporting.CHART_TITLE in svg
    assert reporting.CHART_NOTE in svg
    node_link = (out / "sankey-node-link.json").read_text(encoding="utf-8")
    assert "shareable" in node_link


# --------------------------------------------------------------------------
# the pipeline the chart shows: completed gates, and work that is only booked
# --------------------------------------------------------------------------
def test_a_completed_screening_appears_on_the_chart(run, new_app, log_ok, new_round):
    new_app()
    new_round("Acme GmbH", "2026-01-06", "screening", "recruiter_screen", "phone")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-07", "--round", "1")
    out = Path(run.tmp) / "screen-done"
    assert run("export", "sankey", "--output", str(out)).ok
    edges = read_edges(out / "sankey-edges.csv")
    assert edges == {("all_applications", "screen_done"): 1,
                     ("screen_done", "end_in_progress"): 1}
    data = json.loads((out / "sankey-node-link.json").read_text(encoding="utf-8"))
    labels = {node["id"]: node["label"] for node in data["nodes"]}
    assert labels["screen_done"] == "Screening"
    assert all(node["pending"] is False for node in data["nodes"])


def test_a_screening_counts_once_even_after_several_screens(run, new_app, log_ok, new_round):
    new_app()
    for index, day in enumerate(("2026-01-06", "2026-01-08")):
        new_round("Acme GmbH", day, "screening", "recruiter_screen", "phone")
        log_ok("Acme GmbH", "--event", "completed", "--date", day, "--round", str(index + 1))
    out = Path(run.tmp) / "two-screens"
    assert run("export", "sankey", "--output", str(out)).ok
    assert read_edges(out / "sankey-edges.csv") == {
        ("all_applications", "screen_done"): 1,
        ("screen_done", "end_in_progress"): 1,
    }


def _book(log_ok, company, phase="screening", round_type="recruiter_screen",
          invited_on="2026-01-08", appointment_on="2026-01-20", fmt="phone"):
    """The realistic booking flow: an invitation, then the appointment."""
    log_ok(company, "--event", "invited", "--date", invited_on, "--round", "new",
           "--phase", phase, "--round-type", round_type, "--format", fmt)
    log_ok(company, "--event", "scheduled", "--date", invited_on, "--round", "1",
           "--scheduled-for", appointment_on)


def test_a_booked_round_is_drawn_before_the_outcome_and_marked_pending(
        run, new_app, log_ok, tmp_path):
    """The booked step is the newest thing that happened, so it sits before the outcome."""
    new_app("Booked AG", "Backend", "2026-01-05")
    _book(log_ok, "Booked AG", phase="interview", round_type="general")
    out = tmp_path / "booked"
    meta = bundle(run, out)
    assert edges_of(out) == {("all_applications", "booked_interview"): 1,
                             ("booked_interview", "end_in_progress"): 1}
    data = json.loads((out / "sankey-node-link.json").read_text(encoding="utf-8"))
    booked = [node for node in data["nodes"] if node["pending"]]
    assert [node["id"] for node in booked] == ["booked_interview"]
    svg = (out / "sankey.svg").read_text(encoding="utf-8")
    assert "stroke-dasharray" in svg          # booked nodes are drawn dashed
    assert "Interview booked (1)" in svg
    assert meta["frozen"]["groups"] == [
        {"screens": 0, "assessments": 0, "interviews": 0, "received_offer": 0,
         "pending": "interview", "endpoint": "in_progress", "count": 1}]
    # and the pair still verifies
    replayed = json.loads(run("export", "replay", str(out / "sankey-metadata.json"),
                              "--json").output)
    assert replayed["frozen_reproduced"] is True
    assert replayed["frozen_format"] == reporting.FROZEN_FORMAT


def test_the_newest_round_event_decides_what_is_booked(run, new_app, log_ok):
    """A completed round after the invitation means nothing is booked any more."""
    new_app()
    _book(log_ok, "Acme GmbH", phase="interview", round_type="general")
    log_ok("Acme GmbH", "--event", "completed", "--date", "2026-01-20", "--round", "1")
    out = Path(run.tmp) / "not-booked"
    assert run("export", "sankey", "--output", str(out)).ok
    assert read_edges(out / "sankey-edges.csv") == {
        ("all_applications", "interview_1"): 1,
        ("interview_1", "end_in_progress"): 1,
    }


def test_a_cancelled_booking_is_not_booked_any_more(run, new_app, log_ok):
    new_app()
    _book(log_ok, "Acme GmbH")
    log_ok("Acme GmbH", "--event", "cancelled", "--date", "2026-01-15", "--round", "1")
    out = Path(run.tmp) / "cancelled"
    assert run("export", "sankey", "--output", str(out)).ok
    assert read_edges(out / "sankey-edges.csv") == {
        ("all_applications", "end_in_progress"): 1}


def test_a_booked_round_is_dropped_at_a_cutoff_before_the_invitation(run, new_app, log_ok):
    new_app()
    _book(log_ok, "Acme GmbH")
    out = Path(run.tmp) / "cutoff"
    assert run("export", "sankey", "--as-of", "2026-01-07", "--output", str(out)).ok
    assert read_edges(out / "sankey-edges.csv") == {
        ("all_applications", "end_in_progress"): 1}
    # the invitation itself is inside the cohort at a later cutoff
    later = Path(run.tmp) / "cutoff-later"
    assert run("export", "sankey", "--as-of", "2026-01-09", "--output", str(later)).ok
    assert read_edges(later / "sankey-edges.csv") == {
        ("all_applications", "booked_screening"): 1,
        ("booked_screening", "end_in_progress"): 1}


def test_the_real_shape_of_the_owners_cohort_is_drawn(run, new_app, log_ok):
    """14 quiet, one booked screening, one rejected: the chart must show the booking."""
    for index in range(13):
        new_app(f"Quiet {index}", "Dev", "2026-01-05")
    new_app("Booked AG", "Backend", "2026-01-05")
    new_app("Rejected AG", "Backend", "2026-01-05")
    log_ok("Rejected AG", "--event", "rejected", "--date", "2026-01-20")
    _book(log_ok, "Booked AG")
    out = Path(run.tmp) / "owner-shape"
    assert run("export", "sankey", "--output", str(out)).ok
    assert read_edges(out / "sankey-edges.csv") == {
        ("all_applications", "booked_screening"): 1,
        ("booked_screening", "end_in_progress"): 1,
        ("all_applications", "end_in_progress"): 13,
        ("all_applications", "end_rejected"): 1,
    }
