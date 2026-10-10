import json
from pathlib import Path

import pytest

from cv_batch.build import (
    Selection,
    PickedEntry,
    SelectionValidationError,
    build_cv_dict,
    extract_json_object,
    selection_metadata,
    trim_once,
    validate_selection,
)
from cv_batch.catalog import Candidate, Catalog, CatalogError, Entry, build_catalog

LIMITS = dict(max_bullets_per_entry=5, max_bullets_total=13, max_headline=60)


@pytest.fixture
def catalog(project):
    return build_catalog(
        language="de",
        baseline_path=project.inputs / "current_de.json",
        backlog_path=project.backlog_de,
    )


@pytest.fixture
def baseline(project):
    return json.loads((project.inputs / "current_de.json").read_text(encoding="utf-8"))


def validate(data, catalog, **overrides):
    limits = dict(LIMITS)
    limits.update(overrides)
    return validate_selection(data, catalog, **limits)


EL = "exp/analytical-engines-ltd-2025-11"
SP = "exp/numerical-machines-gmbh-2022-09"


def test_extract_json_object_accepts_fenced_json():
    assert extract_json_object('```json\n{"experience": []}\n```') == {"experience": []}


@pytest.mark.parametrize(
    "text",
    ['no json here', '[1, 2]', '{"experience": [], "extra": 1}', '{"experience": ['],
)
def test_extract_json_object_rejects_prose_and_bad_shapes(text):
    with pytest.raises(SelectionValidationError):
        extract_json_object(text)


def test_valid_selection_keeps_the_entry_order(catalog):
    data = {
        "experience": [
            {"entry": EL, "bullets": [f"{EL}/el2-short", f"{EL}/el3-short"]},
            {"entry": SP, "bullets": [f"{SP}/b1"]},
        ],
    }
    selection = validate(data, catalog)
    assert [entry.entry_id for entry in selection.entries] == [EL, SP]


def test_entries_are_always_returned_in_timeline_order(catalog):
    """A model answer in the wrong order is normalised to newest job first."""
    data = {
        "experience": [
            {"entry": SP, "bullets": [f"{SP}/b1"]},
            {"entry": EL, "bullets": [f"{EL}/el1-short"]},
        ],
    }
    selection = validate(data, catalog)
    assert [entry.entry_id for entry in selection.entries] == [EL, SP]
    # the order the model actually answered in is kept for the evidence
    assert selection.model_entry_order == (SP, EL)


def test_build_cv_dict_orders_the_experience_newest_first(catalog, baseline):
    selection = validate(
        {
            "experience": [
                {"entry": SP, "bullets": [f"{SP}/b1"]},
                {"entry": EL, "bullets": [f"{EL}/el1-short"]},
            ]
        },
        catalog,
    )
    cv = build_cv_dict(baseline, catalog, selection)
    assert [entry["meta"].split("|")[0].strip() for entry in cv["experience"]] == [
        "Analytical Engines Ltd, Berlin",
        "Numerical Machines GmbH, Leipzig",
    ]
    # the same holds for a hand-made selection that is not ordered at all
    manual = Selection(headline="x", entries=[selection.entries[1], selection.entries[0]])
    assert [
        entry["meta"].split("|")[0].strip()
        for entry in build_cv_dict(baseline, catalog, manual)["experience"]
    ] == ["Analytical Engines Ltd, Berlin", "Numerical Machines GmbH, Leipzig"]


def test_selection_metadata_records_the_normalised_order(catalog):
    selection = validate(
        {
            "experience": [
                {"entry": SP, "bullets": [f"{SP}/b1"]},
                {"entry": EL, "bullets": [f"{EL}/el1-short"]},
            ]
        },
        catalog,
    )
    meta = selection_metadata(selection, catalog)
    assert [item["entry"] for item in meta["experience"]] == [EL, SP]
    assert meta["entry_order"] == "catalog (newest job first)"
    assert meta["model_entry_order_normalized"] == [SP, EL]

    ordered = validate({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]}, catalog)
    assert "model_entry_order_normalized" not in selection_metadata(ordered, catalog)


def test_the_headline_comes_from_the_profile_and_a_model_headline_is_ignored(catalog):
    """The headline is a statement about the applicant, not a per-posting choice."""
    assert catalog.headline == "Software Developer"  # the profile baseline value
    without = validate({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]}, catalog)
    assert without.headline == "Software Developer"
    for proposed in ("Java Backend Developer", "x" * 80, "12345", "zwei\nZeilen", 5):
        selection = validate(
            {"headline": proposed, "experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]},
            catalog,
        )
        assert selection.headline == "Software Developer"


def test_the_cv_toml_headline_override_wins_over_the_baseline(project):
    overridden = build_catalog(
        language="de",
        baseline_path=project.inputs / "current_de.json",
        backlog_path=project.backlog_de,
        headline_override="Data Engineer",
    )
    assert overridden.headline == "Data Engineer"


def test_a_baseline_without_a_headline_is_a_clear_error(tmp_path):
    baseline = tmp_path / "current_de.json"
    baseline.write_text(json.dumps({"language": "de", "experience": []}), encoding="utf-8")
    backlog = tmp_path / "backlog.md"
    backlog.write_text("# Backlog\n", encoding="utf-8")
    from cv_batch.catalog import CatalogError, build_catalog

    with pytest.raises(CatalogError, match="has no 'headline'"):
        build_catalog(language="de", baseline_path=baseline, backlog_path=backlog)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"experience": []}, "non-empty"),
        ({"experience": "x"}, "non-empty list"),
        ({"experience": [{"entry": "exp/not-real", "bullets": ["x"]}]}, "not a known entry id"),
        ({"experience": [{"entry": EL, "bullets": []}]}, "non-empty list"),
        ({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-detailed"]}]}, "never a CV bullet"),
        ({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short", f"{EL}/el1-short"]}]}, "more than once"),
        ({"experience": [{"entry": EL, "bullets": [f"{SP}/b1"]}]}, "belongs to"),
        ({"experience": [{"entry": EL, "bullets": ["invented/id"]}]}, "unknown candidate id"),
        (
            {
                "experience": [
                    {"entry": EL, "bullets": [f"{EL}/el1-short"]},
                    {"entry": EL, "bullets": [f"{EL}/el2-short"]},
                ]
            },
            "more than once",
        ),
        ({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"], "x": 1}]}, "unknown key"),
    ],
)
def test_invalid_selections(catalog, data, fragment):
    with pytest.raises(SelectionValidationError) as excinfo:
        validate(data, catalog)
    assert fragment in str(excinfo.value)


def test_same_group_candidates_are_still_alternatives():
    """The group rule stays guarded for any future catalog with real alternatives."""
    entry_id = "exp/synthetic"
    synthetic = Catalog(
        language="de",
        baseline_path=Path("baseline.json"),
        headline="Software Developer",
        entries=[
            Entry(
                id=entry_id,
                role="Software Developer",
                meta="Synthetic GmbH, Ettlingen | 01.2020 - 12.2021",
                candidates=(
                    Candidate(f"{entry_id}/g1-short", entry_id, f"{entry_id}/g1", "Kurz", "backlog"),
                    Candidate(f"{entry_id}/g1-alt", entry_id, f"{entry_id}/g1", "Anders", "backlog"),
                ),
            )
        ],
        fixed_context={},
    )
    with pytest.raises(SelectionValidationError) as excinfo:
        validate(
            {
                "experience": [
                    {"entry": entry_id, "bullets": [f"{entry_id}/g1-short", f"{entry_id}/g1-alt"]}
                ]
            },
            synthetic,
        )
    assert "alternatives" in str(excinfo.value)


def test_caps_are_enforced(catalog):
    bullets = [f"{EL}/el1-short", f"{EL}/el2-short", f"{EL}/el3-short", f"{SP}/b1", f"{SP}/b2"]
    with pytest.raises(SelectionValidationError) as excinfo:
        validate({"experience": [{"entry": EL, "bullets": bullets[:5]}]}, catalog, max_bullets_per_entry=2)
    assert "limit is 2" in str(excinfo.value)

    with pytest.raises(SelectionValidationError) as excinfo:
        validate(
            {"experience": [{"entry": EL, "bullets": bullets[:3]}, {"entry": SP, "bullets": [f"{SP}/b1"]}]},
            catalog,
            max_bullets_total=3,
        )
    assert "limit is 3" in str(excinfo.value)


def test_build_cv_dict_uses_selected_texts_and_keeps_everything_else(catalog, baseline):
    selection = validate(
        {"experience": [{"entry": SP, "bullets": [f"{SP}/b2", f"{SP}/b1"]}]},
        catalog,
    )
    cv = build_cv_dict(baseline, catalog, selection)
    assert cv["headline"] == "Software Developer"
    assert cv["experience"] == [
        {
            "role": "Software Developer Backend",
            "meta": "Numerical Machines GmbH, Leipzig | 09.2022 - 10.2025",
            "bullets": ["WildFly Deployments", "Maven Builds"],
        }
    ]
    # Skills are maintained in the backlog, so they replace the baseline copy.
    assert cv["skills"] == catalog.fixed_context["skills"]
    assert cv["skills"] != baseline["skills"]
    for key in ("contacts", "languages", "education", "certificates", "photo"):
        assert cv[key] == baseline[key]
    assert json.loads(json.dumps(cv)) == cv  # JSON serialisable


def test_build_cv_dict_does_not_mutate_the_baseline(catalog, baseline):
    before = json.dumps(baseline, sort_keys=True)
    selection = validate({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]}, catalog)
    build_cv_dict(baseline, catalog, selection)
    assert json.dumps(baseline, sort_keys=True) == before


def test_trim_once_drops_from_the_longest_entry_then_the_entry(catalog):
    selection = Selection(
        headline="x",
        entries=[
            PickedEntry(EL, (f"{EL}/el1-short", f"{EL}/el2-short", f"{EL}/el3-short")),
            PickedEntry(SP, (f"{SP}/b1",)),
        ],
    )
    trimmed, note = trim_once(selection, catalog)
    assert note and "el3-short" in note
    assert [len(entry.bullet_ids) for entry in trimmed.entries] == [2, 1]

    trimmed2, note2 = trim_once(trimmed, catalog)
    assert [len(entry.bullet_ids) for entry in trimmed2.entries] == [1, 1]
    trimmed3, note3 = trim_once(trimmed2, catalog)
    assert note3 and "dropped entry" in note3
    assert [entry.entry_id for entry in trimmed3.entries] == [EL]
    trimmed4, note4 = trim_once(trimmed3, catalog)
    assert note4 is None and trimmed4 == trimmed3


def test_trim_drops_the_bullet_of_the_oldest_entry_on_a_tie(catalog):
    """Equal-length entries: the older job loses the bullet, not the newest one."""
    selection = Selection(
        headline="x",
        entries=[
            PickedEntry(EL, (f"{EL}/el1-short", f"{EL}/el2-short")),
            PickedEntry(SP, (f"{SP}/b1", f"{SP}/b2")),
        ],
    )
    trimmed, note = trim_once(selection, catalog)
    assert note and SP in note
    assert [entry.entry_id for entry in trimmed.entries] == [EL, SP]
    assert len(trimmed.entries[0].bullet_ids) == 2
    assert len(trimmed.entries[1].bullet_ids) == 1

    # same rule when every entry has a single bullet: the oldest entry goes last
    downsized = Selection(
        headline="x",
        entries=[PickedEntry(SP, (f"{SP}/b1",)), PickedEntry(EL, (f"{EL}/el1-short",))],
    )
    trimmed2, note2 = trim_once(downsized, catalog)
    assert note2 and "oldest entry" in note2
    assert [entry.entry_id for entry in trimmed2.entries] == [EL]


def test_selection_metadata_is_reproducible(catalog):
    selection = validate({"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]}, catalog)
    meta = selection_metadata(selection, catalog)
    assert meta["experience"][0]["bullets"][0]["id"] == f"{EL}/el1-short"
    assert meta["experience"][0]["bullets"][0]["group"] == f"{EL}/el1"
    assert meta["experience"][0]["bullets"][0]["source"] == "backlog"


def test_an_unusable_fixed_headline_is_a_configuration_error(catalog):
    """The limit now guards the fixed headline itself, not a model choice."""
    data = {"experience": [{"entry": EL, "bullets": [f"{EL}/el1-short"]}]}
    with pytest.raises(CatalogError) as excinfo:
        validate(data, catalog, max_headline=5)
    assert "fixed headline" in str(excinfo.value)
