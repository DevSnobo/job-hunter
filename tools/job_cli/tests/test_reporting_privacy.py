"""Privacy regression tests for the shareable aggregate Sankey export.

Two confirmed leaks are pinned here:

1. ``export_id`` used to be the output directory basename, so a private folder name
   (for example "Retest Systems 2026-08-01 private") leaked into the JSON that the
   tool calls shareable.
2. ``frozen.rows`` stored one tuple per application (exact interview count, exact
   status), so the "aggregate only" file still carried row-level shapes, order, and
   precision the published chart never shows.

The aggregate must stay a counted set of collapsed chart shapes: duplicate shapes
become one group with a count, rounds collapse to "3+", and ``started`` folds into
``accepted``. Replay stays non-executable and self-contained, and the current-data
comparison keeps its explicit ``matches``/``differs``/``unavailable`` semantics.
"""
from __future__ import annotations

import hashlib
import json
import re

from helpers import payload, read_edges, rows
from job_cli import reporting

PRIVATE_DIR_NAME = "Retest Systems 2026-08-01 private"
PRIVATE_TOKENS = ("Retest", "Systems", "2026-08-01", "private", "Confidential Corp")


def _metadata(out) -> dict:
    return json.loads((out / "sankey-metadata.json").read_text(encoding="utf-8"))


def _groups_by_shape(meta: dict) -> dict:
    """Shape -> count: the gates, the interview bucket, the offer, the booked phase."""
    return {(group["screens"], group["assessments"], group["interviews"],
             group["received_offer"], group["pending"], group["endpoint"]): group["count"]
            for group in meta["frozen"]["groups"]}


def _quiet_apps(new_app, count: int) -> None:
    """Cohort filler: identical shape (no events, 0 interviews, no offer)."""
    for index in range(count):
        new_app(f"Quiet {index}", "Dev", "2026-01-05")


def _started_app(new_app, log_ok, company: str, interviews: int) -> None:
    """An application with ``interviews`` completed rounds that ends as ``started``."""
    new_app(company, "Dev", "2026-01-05")
    for index in range(interviews):
        log_ok(company, "--event", "completed", "--date", f"2026-01-{10 + index:02d}",
               "--round", "new", "--phase", "interview", "--round-type", "general")
    log_ok(company, "--event", "offer_received", "--date", "2026-02-01")
    log_ok(company, "--event", "offer_accepted", "--date", "2026-02-03")
    log_ok(company, "--event", "job_started", "--date", "2026-02-10")


# --------------------------------------------------------------------------
# leak 1: the output directory name
# --------------------------------------------------------------------------
def test_output_directory_name_never_leaks_into_the_shareable_pair(run, new_app, tmp_path):
    new_app("Confidential Corp", "Secret Role", "2026-08-01")
    out = tmp_path / PRIVATE_DIR_NAME
    assert run("export", "sankey", "--output", str(out)).ok
    meta_text = (out / "sankey-metadata.json").read_text(encoding="utf-8")
    edges_text = (out / "sankey-edges.csv").read_text(encoding="utf-8")
    meta = json.loads(meta_text)

    for text in (meta_text, edges_text):
        assert PRIVATE_DIR_NAME not in text
        for token in PRIVATE_TOKENS:
            assert token not in text

    export_id = meta["export_id"]
    assert isinstance(export_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", export_id)  # opaque random hex
    assert export_id != out.name
    # not a digest of the directory name either
    assert export_id != hashlib.sha256(out.name.encode("utf-8")).hexdigest()


def test_export_id_is_random_and_independent_of_the_directory(run, new_app, tmp_path):
    new_app()
    first = tmp_path / PRIVATE_DIR_NAME
    second = tmp_path / "harmless"
    assert run("export", "sankey", "--output", str(first)).ok
    assert run("export", "sankey", "--output", str(second)).ok
    first_id = _metadata(first)["export_id"]
    second_id = _metadata(second)["export_id"]
    assert first_id != second_id
    for export_id, directory in ((first_id, first), (second_id, second)):
        assert export_id not in directory.name
        assert directory.name not in export_id


# --------------------------------------------------------------------------
# leak 2: per-application frozen rows
# --------------------------------------------------------------------------
def test_duplicate_shapes_collapse_to_counted_groups_not_records(
        run, new_app, log_ok, tmp_path):
    _quiet_apps(new_app, 5)
    _started_app(new_app, log_ok, "Started Four", 4)
    _started_app(new_app, log_ok, "Started Seven", 7)
    statuses = {row["company"]: row["current_status"] for row in rows(run, "list", "--json")}
    assert statuses["Started Four"] == statuses["Started Seven"] == "started"

    out = tmp_path / "aggregate"
    assert run("export", "sankey", "--output", str(out)).ok
    meta = _metadata(out)
    groups = meta["frozen"]["groups"]

    assert meta["cohort_size"] == 7
    assert meta["frozen"]["application_count"] == 7
    assert len(groups) == meta["frozen"]["group_count"] == 2  # not 7 records
    assert _groups_by_shape(meta) == {
        (0, 0, 0, 0, None, "in_progress"): 5,
        (0, 0, 3, 1, None, "accepted"): 2,  # 4 and 7 interviews collapse to the 3+ bucket
    }
    assert sum(group["count"] for group in groups) == meta["cohort_size"]
    assert meta["total_flow"] == sum(read_edges(out / "sankey-edges.csv").values())
    assert all(set(group) == set(reporting.GROUP_FIELDS) for group in groups)
    assert all(group["interviews"] in reporting.INTERVIEW_BUCKETS for group in groups)
    assert all(group["screens"] in reporting.GATE_BUCKETS for group in groups)
    assert all(group["assessments"] in reporting.GATE_BUCKETS for group in groups)
    assert all(group["pending"] in (None, *reporting.SANKEY_PHASES) for group in groups)
    assert "started" not in {group["endpoint"] for group in groups}  # folded to accepted
    # deterministic shape order: two exports of the same data list the same groups
    other = tmp_path / "aggregate-again"
    assert run("export", "sankey", "--output", str(other)).ok
    assert _metadata(other)["frozen"]["groups"] == groups
    assert _metadata(other)["export_id"] != meta["export_id"]

    # no identifier, name, or date survives; the edge CSV is reproduced exactly
    group_text = json.dumps(groups)
    for secret in ("Started Four", "Started Seven", "Quiet", "Confidential",
                   "2026-01-05", "2026-02-10"):
        assert secret not in group_text
    assert reporting.build_sankey_from_groups(groups) == read_edges(
        out / "sankey-edges.csv")

    replayed = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert replayed["executed_metadata_sql"] is False
    assert replayed["frozen_format"] == reporting.FROZEN_FORMAT
    assert replayed["frozen_group_count"] == 2
    assert replayed["frozen_cohort_size"] == 7
    assert replayed["frozen_reproduced"] is True
    assert replayed["pair_consistent"] is True
    assert replayed["current_comparison"] == "matches"
    assert replayed["reproduced"] is True


# --------------------------------------------------------------------------
# strict group validation
# --------------------------------------------------------------------------
def _tamper(out, mutate) -> None:
    meta = _metadata(out)
    mutate(meta)
    (out / "sankey-metadata.json").write_text(json.dumps(meta, indent=2) + "\n",
                                               encoding="utf-8")


def _replay_error(run, out) -> str:
    result = run("export", "replay", str(out / "sankey-metadata.json"))
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    return result.output


def test_replay_rejects_groups_with_extra_precision_or_bad_counts(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "strict"
    assert run("export", "sankey", "--output", str(out)).ok
    pristine = (out / "sankey-metadata.json").read_text(encoding="utf-8")
    assert len(json.loads(pristine)["frozen"]["groups"]) == 1

    def tamper(mutate):
        """Start from the pristine pair every time, so cases cannot mask each other."""
        meta = json.loads(pristine)
        mutate(meta)
        (out / "sankey-metadata.json").write_text(json.dumps(meta, indent=2) + "\n",
                                                  encoding="utf-8")

    def field(name, value):
        def mutate(meta):
            meta["frozen"]["groups"][0][name] = value
        return mutate

    def extra_application_field(meta):
        meta["frozen"]["groups"][0]["application_id"] = "abc"

    cases = [
        ("zero count", field("count", 0)),
        ("negative count", field("count", -1)),
        ("boolean count", field("count", True)),
        ("exact 4 interviews", field("interviews", 4)),
        ("two screenings", field("screens", 2)),
        ("two assessments", field("assessments", 2)),
        ("boolean screening flag", field("screens", True)),
        ("unknown booked phase", field("pending", "chat")),
        ("booked phase that is not a phase", field("pending", "invited")),
        ("unfolded started endpoint", field("endpoint", "started")),
        ("unknown endpoint", field("endpoint", "end_elsewhere")),
        ("extra per-application field", extra_application_field),
    ]
    for label, mutate in cases:
        tamper(mutate)
        assert "malformed frozen group" in _replay_error(run, out), label
    tamper(lambda meta: None)  # pristine pair still replays cleanly
    data = payload(run, "export", "replay", str(out / "sankey-metadata.json"), "--json")
    assert data["frozen_reproduced"] is True


def test_replay_rejects_declared_counts_that_disagree_with_the_groups(
        run, new_app, log_ok, tmp_path):
    new_app()
    log_ok("Acme GmbH", "--event", "rejected", "--date", "2026-01-20")
    out = tmp_path / "sum"
    assert run("export", "sankey", "--output", str(out)).ok

    _tamper(out, lambda meta: meta["frozen"].__setitem__("application_count", 9))
    assert "frozen groups sum" in _replay_error(run, out)

    groups_out = tmp_path / "sum-groups"
    assert run("export", "sankey", "--output", str(groups_out)).ok
    _tamper(groups_out, lambda meta: meta["frozen"].__setitem__("group_count", 9))
    assert "group_count" in _replay_error(run, groups_out)

    cohort_out = tmp_path / "sum-cohort"
    assert run("export", "sankey", "--output", str(cohort_out)).ok
    _tamper(cohort_out, lambda meta: meta.__setitem__("cohort_size", 9))
    assert "cohort_size" in _replay_error(run, cohort_out)


def test_replay_rejects_unknown_frozen_formats_but_reads_legacy_v1(run, new_app, tmp_path):
    new_app()
    out = tmp_path / "formats"
    assert run("export", "sankey", "--output", str(out)).ok
    _tamper(out, lambda meta: meta["frozen"].__setitem__("format", "sankey-frozen-v9"))
    message = _replay_error(run, out)
    assert "frozen" in message and "refusing to replay" in message

    # a v1 file (per-application tuples) is still read, folded, and never re-emitted
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    edges_text = (out / "sankey-edges.csv").read_text(encoding="utf-8")
    (legacy / "sankey-edges.csv").write_text(edges_text, encoding="utf-8")
    (legacy / "sankey-metadata.json").write_text(json.dumps({
        "kind": "sankey_aggregate",
        "filters": {"as_of": None, "cohort_from": None, "cohort_to": None},
        "cohort_size": 1,
        "query_sql": "DELETE FROM applications",
        "edges_file": "sankey-edges.csv",
        "frozen": {
            "format": reporting.LEGACY_FROZEN_FORMAT,
            "canonical_fields": list(reporting.LEGACY_ROW_FIELDS),
            "row_count": 1,
            "rows": [[0, 0, "in_progress"]],
            "edges_sha256": hashlib.sha256(edges_text.encode("utf-8")).hexdigest(),
        },
    }, indent=2) + "\n", encoding="utf-8")
    data = payload(run, "export", "replay", str(legacy / "sankey-metadata.json"), "--json")
    assert data["executed_metadata_sql"] is False
    assert data["frozen_format"] == reporting.LEGACY_FROZEN_FORMAT
    assert data["frozen_reproduced"] is True
    assert data["frozen_group_count"] == 1


def test_replay_still_reads_a_v2_bundle_that_has_no_gates_and_no_booking(run, new_app, tmp_path):
    """A bundle written before the chart showed screens and bookings still replays."""
    new_app()
    out = tmp_path / "v2"
    assert run("export", "sankey", "--output", str(out)).ok
    edges_text = (out / "sankey-edges.csv").read_text(encoding="utf-8")
    meta = json.loads((out / "sankey-metadata.json").read_text(encoding="utf-8"))
    # rewrite the pair as a v2 file: the same aggregate, without the newer shape fields
    v2_groups = [{"interviews": 0, "received_offer": 0, "endpoint": "in_progress", "count": 1}]
    (out / "sankey-edges.csv").write_text(
        "source,target,value\nall_applications,end_in_progress,1\n", encoding="utf-8")
    meta["frozen"] = {
        "format": reporting.PREVIOUS_FROZEN_FORMAT,
        "group_fields": list(reporting.PREVIOUS_GROUP_FIELDS),
        "group_count": 1,
        "application_count": 1,
        "groups": v2_groups,
        "edges_sha256": hashlib.sha256(
            (out / "sankey-edges.csv").read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
    }
    (out / "sankey-metadata.json").write_text(json.dumps(meta, indent=2) + "\n",
                                             encoding="utf-8")
    data = json.loads(run("export", "replay", str(out / "sankey-metadata.json"), "--json").output)
    assert data["frozen_format"] == reporting.PREVIOUS_FROZEN_FORMAT
    assert data["frozen_group_count"] == 1
    assert data["frozen_reproduced"] is True
    assert data["pair_consistent"] is True
    assert data["executed_metadata_sql"] is False
