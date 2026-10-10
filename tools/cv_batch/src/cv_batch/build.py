"""Validate the model selection and build the CV generator input.

The model never writes files, never runs the renderer and never invents facts.
It only returns candidate IDs.  This module checks that selection strictly and
turns it into the real ``cvgen`` schema, taking all immutable content from the
reviewed baseline input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from .catalog import BULLET_FORM_SUFFIX, LONG_FORM_SUFFIX, Catalog, CatalogError

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: a headline must contain at least one letter (the prompt forbids digits-only text)
LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


class SelectionValidationError(ValueError):
    """The model answer is not a valid selection (feeds the bounded retry)."""


@dataclass(frozen=True)
class PickedEntry:
    entry_id: str
    bullet_ids: tuple[str, ...]
    trimmed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Selection:
    headline: str
    #: entries in catalog order (most recent job first), never in model order
    entries: list[PickedEntry]
    #: the order the model actually sent; kept for the evidence only
    model_entry_order: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "headline": self.headline,
            "experience": [
                {"entry": entry.entry_id, "bullets": list(entry.bullet_ids)} for entry in self.entries
            ],
        }


def extract_json_object(text: str) -> dict:
    """Parse the model answer strictly; fences are tolerated, prose is not."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SelectionValidationError(f"answer is not valid JSON: {exc.msg} at position {exc.pos}") from exc
    if not isinstance(data, dict):
        raise SelectionValidationError("answer must be a JSON object")
    allowed = {"headline", "experience"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SelectionValidationError(
            f"unknown key(s) {unknown!r}; allowed keys are {sorted(allowed)!r}"
        )
    return data


def validate_selection(
    data: dict,
    catalog: Catalog,
    *,
    max_bullets_per_entry: int,
    max_bullets_total: int,
    max_headline: int,
) -> Selection:
    # The headline is fixed for every CV and never varies with the posting, so
    # the model has no say in it.  A "headline" key in the answer is accepted
    # and ignored instead of failing the run for a field that is not used.
    headline = catalog.headline
    if not headline or len(headline) > max_headline or not LETTER.search(headline):
        raise CatalogError(
            f"the fixed headline {headline!r} is not usable "
            f"(max {max_headline} characters, at least one letter)"
        )

    experience = data.get("experience")
    if not isinstance(experience, list) or not experience:
        raise SelectionValidationError("'experience' must be a non-empty list")
    if len(experience) > len(catalog.entries):
        raise SelectionValidationError(
            f"'experience' has {len(experience)} entries but only {len(catalog.entries)} exist"
        )

    picked: list[PickedEntry] = []
    seen_entries: set[str] = set()
    seen_bullets: set[str] = set()
    seen_groups: dict[str, str] = {}
    total = 0

    for position, item in enumerate(experience, start=1):
        where = f"experience[{position - 1}]"
        if not isinstance(item, dict):
            raise SelectionValidationError(f"{where} must be an object")
        unknown = sorted(set(item) - {"entry", "bullets"})
        if unknown:
            raise SelectionValidationError(f"{where} has unknown key(s) {unknown!r}")
        entry_id = item.get("entry")
        if not isinstance(entry_id, str):
            raise SelectionValidationError(f"{where}.entry must be a string")
        entry = catalog.entry(entry_id)
        if entry is None:
            raise SelectionValidationError(
                f"{where}.entry {entry_id!r} is not a known entry id "
                f"(known: {[e.id for e in catalog.entries]!r})"
            )
        if entry_id in seen_entries:
            raise SelectionValidationError(f"entry {entry_id!r} appears more than once")
        seen_entries.add(entry_id)

        bullets = item.get("bullets")
        if not isinstance(bullets, list) or not bullets:
            raise SelectionValidationError(f"{where}.bullets must be a non-empty list")
        if len(bullets) > max_bullets_per_entry:
            raise SelectionValidationError(
                f"{where} selects {len(bullets)} bullets; the limit is {max_bullets_per_entry}"
            )

        resolved: list[str] = []
        for bullet_id in bullets:
            if not isinstance(bullet_id, str):
                raise SelectionValidationError(f"{where}.bullets must contain strings")
            if bullet_id.endswith(LONG_FORM_SUFFIX):
                raise SelectionValidationError(
                    f"{bullet_id!r} is long-form backlog text; it is never a CV bullet. "
                    f"Select the marked bullet form (id ending in {BULLET_FORM_SUFFIX!r}) instead"
                )
            candidate = catalog.candidate(bullet_id)
            if candidate is None:
                raise SelectionValidationError(
                    f"unknown candidate id {bullet_id!r} (never invent ids)"
                )
            if candidate.entry_id != entry_id:
                raise SelectionValidationError(
                    f"candidate {bullet_id!r} belongs to {candidate.entry_id!r}, not to {entry_id!r}"
                )
            if bullet_id in seen_bullets:
                raise SelectionValidationError(f"candidate {bullet_id!r} is selected more than once")
            seen_bullets.add(bullet_id)
            if candidate.group:
                other = seen_groups.get(candidate.group)
                if other and other != bullet_id:
                    raise SelectionValidationError(
                        f"candidates {other!r} and {bullet_id!r} are alternatives "
                        f"(same group {candidate.group!r}); select only one"
                    )
                seen_groups[candidate.group] = bullet_id
            resolved.append(bullet_id)
            total += 1
        picked.append(PickedEntry(entry_id=entry_id, bullet_ids=tuple(resolved)))

    if total > max_bullets_total:
        raise SelectionValidationError(
            f"the selection contains {total} bullets; the limit is {max_bullets_total}"
        )
    # The model order is recorded for the evidence, then replaced by the catalog
    # order: the CV timeline is newest job first, whatever the model answered.
    selection = Selection(
        headline=headline,
        entries=picked,
        model_entry_order=tuple(entry.entry_id for entry in picked),
    )
    return order_entries(selection, catalog)


def catalog_index(catalog: Catalog, entry_id: str) -> int:
    """Position of an entry in the catalog (``len(entries)`` when unknown)."""
    for index, entry in enumerate(catalog.entries):
        if entry.id == entry_id:
            return index
    return len(catalog.entries)


def order_entries(selection: Selection, catalog: Catalog) -> Selection:
    """Return the selection with its entries in catalog order (newest job first).

    The catalog keeps the order of the reviewed baseline, which lists the past
    jobs from the newest to the oldest.  A tailored CV must always show that
    timeline: the model may answer in any order, but relevance decides only
    *which* entries and bullets are selected, never the order they appear in.
    """
    ordered = sorted(
        selection.entries, key=lambda picked: catalog_index(catalog, picked.entry_id)
    )
    if ordered == selection.entries:
        return selection
    return replace(selection, entries=ordered)


def build_cv_dict(baseline: dict, catalog: Catalog, selection: Selection) -> dict:
    """Rebuild the baseline with the selected experience; nothing else changes.

    The experience list is emitted in catalog order (most recent job first) even
    when the model answered in a different order.
    """
    cv = json.loads(json.dumps(baseline))  # deep copy
    cv["language"] = catalog.language
    cv["headline"] = selection.headline
    # Sidebar skills are maintained in the backlog, so they replace the baseline
    # copy here; every other immutable field stays as the baseline has it.
    skills = catalog.fixed_context.get("skills")
    if skills:
        cv["skills"] = json.loads(json.dumps(skills))
    experience = []
    for picked in order_entries(selection, catalog).entries:
        entry = catalog.entry(picked.entry_id)
        assert entry is not None  # validated earlier
        bullets = []
        for bullet_id in picked.bullet_ids:
            candidate = catalog.candidate(bullet_id)
            assert candidate is not None  # validated earlier
            bullets.append(candidate.text)
        experience.append({"role": entry.role, "meta": entry.meta, "bullets": bullets})
    cv["experience"] = experience
    return cv


def trim_once(selection: Selection, catalog: Catalog) -> tuple[Selection, str | None]:
    """Drop the last (least relevant) bullet of the longest entry.

    On a tie the *oldest* entry loses the bullet, and when whole entries have to
    go the oldest one is dropped, so the most recent jobs stay intact.  Returns
    the new selection and a description of what was removed, or
    ``(selection, None)`` when nothing can be trimmed any more.
    """
    if not selection.entries:
        return selection, None
    # The longest entry loses a bullet first; on a tie the *oldest* job loses it,
    # so the most recent roles stay untouched.  The tie-break uses the catalog
    # order, not the order the model happened to answer in.
    target = max(
        range(len(selection.entries)),
        key=lambda index: (
            len(selection.entries[index].bullet_ids),
            catalog_index(catalog, selection.entries[index].entry_id),
        ),
    )
    best = len(selection.entries[target].bullet_ids)
    if best <= 1 or len(selection.entries) <= 1:
        if len(selection.entries) > 1 and best <= 1:
            # All entries are down to one bullet: drop the *oldest* job (the last
            # one in the timeline), not whatever the model listed last.
            oldest = max(
                range(len(selection.entries)),
                key=lambda index: catalog_index(catalog, selection.entries[index].entry_id),
            )
            dropped = selection.entries[oldest]
            kept = [entry for position, entry in enumerate(selection.entries) if position != oldest]
            note = f"dropped entry {dropped.entry_id} (oldest entry; all entries were down to one bullet)"
            return replace(selection, entries=kept), note
        return selection, None

    picked = selection.entries[target]
    dropped_id = picked.bullet_ids[-1]
    candidate = catalog.candidate(dropped_id)
    text = candidate.text if candidate else dropped_id
    new_entry = PickedEntry(
        entry_id=picked.entry_id,
        bullet_ids=picked.bullet_ids[:-1],
        trimmed=picked.trimmed + (dropped_id,),
    )
    entries = list(selection.entries)
    entries[target] = new_entry
    note = f"dropped {dropped_id} from {picked.entry_id}: {_shorten(text)}"
    return replace(selection, entries=entries), note


def _shorten(text: str, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def selection_metadata(selection: Selection, catalog: Catalog) -> dict:
    rendered_order = [picked.entry_id for picked in selection.entries]
    metadata = {
        "headline": selection.headline,
        "entry_order": "catalog (newest job first)",
        "experience": [
            {
                "entry": picked.entry_id,
                "role": (catalog.entry(picked.entry_id).role if catalog.entry(picked.entry_id) else ""),
                "bullets": [
                    {
                        "id": bullet_id,
                        "group": (catalog.candidate(bullet_id).group if catalog.candidate(bullet_id) else None),
                        "source": (catalog.candidate(bullet_id).source if catalog.candidate(bullet_id) else None),
                        "text": (catalog.candidate(bullet_id).text if catalog.candidate(bullet_id) else ""),
                    }
                    for bullet_id in picked.bullet_ids
                ],
                "trimmed": list(picked.trimmed),
            }
            for picked in selection.entries
        ],
    }
    model_order = list(selection.model_entry_order)
    if model_order and model_order != rendered_order:
        metadata["model_entry_order_normalized"] = model_order
    return metadata


__all__ = [
    "PickedEntry",
    "Selection",
    "SelectionValidationError",
    "build_cv_dict",
    "catalog_index",
    "extract_json_object",
    "order_entries",
    "selection_metadata",
    "trim_once",
    "validate_selection",
]
