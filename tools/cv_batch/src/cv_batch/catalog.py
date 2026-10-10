"""Candidate catalog built from the reviewed baseline CV and the career backlogs.

Design rules (kept deliberately conservative, see the report):

* The reviewed baseline inputs (``profile/inputs/current_<lang>.json``)
  supply every immutable fact: identity, contacts, photo, sidebar skills,
  languages, education and certificates, plus the role titles and date lines.
* Bullet candidates come from the baseline for every entry, because those
  bullets are already curated, role-mapped and free of the "use instead of, not
  alongside" alternatives that the backlogs keep side by side.
* When a backlog section contains *structured* items (``#### N. Title`` with a
  detailed and a short form, as in the nine Analytical Engines entries), those items
  replace the baseline bullets as candidates for that entry.
* The sidebar *technical skills* come from the backlog section "Technical
  skills — CV sidebar" / "Technische Skills — Seitenleiste des Lebenslaufs",
  because that is where those values are maintained.  ``**bold**`` inside a
  backlog skill line becomes a bold segment; a line without markup stays plain.
  When the backlog has no readable skills section, the baseline skills are used.
* The headline comes from the profile baseline (``baseline["headline"]``); the
  model never chooses it and no posting can change it.  ``cv.toml`` ``headline``
  overrides it, and a baseline without one is a clear error.
* HARD RULE: only the marked *bullet* form becomes a candidate
  (``Lebenslaufpunkt`` in German, ``Resume bullet`` in English).  The long
  ``Ausführliche Version`` / ``Detailed version`` text is background prose and
  is never offered to the selector, so it can never reach a generated CV.
* Everything is read-only.  The backlogs and the baseline inputs are never
  rewritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RANGE = re.compile(r"(\d{2}\.\d{4})\s*[-\u2013\u2014]\s*(\d{2}\.\d{4})")
DATE = re.compile(r"\d{2}\.\d{4}")
H3 = re.compile(r"^###\s+(.*\S)\s*$")
H2 = re.compile(r"^##\s+")
ITEM = re.compile(r"^####\s+(\d+)\.\s*(.*\S)\s*$")
LABEL = re.compile(r"^\*\*(.+?)\*\*\s*:?\s*$")
BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
DETAILED_HINT = re.compile(r"ausf|detail|long", re.IGNORECASE)
SHORT_HINT = re.compile(r"bullet|point|punkt|resume|kurz", re.IGNORECASE)
H2_TITLE = re.compile(r"^##\s+(.*\S)\s*$")
SIDEBAR_SKILLS_H2 = re.compile(r"technische\s+skills|technical\s+skills", re.IGNORECASE)
BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")

#: Suffix of the long-form backlog text.  That text is background prose, never a
#: CV bullet: it is not offered as a candidate and the validator rejects the id
#: outright, so a long form can never reach a generated CV.
LONG_FORM_SUFFIX = "-detailed"

#: Suffix of the marked bullet form, the only backlog text that may be used.
BULLET_FORM_SUFFIX = "-short"


class CatalogError(RuntimeError):
    """The catalog could not be built from the configured inputs."""


@dataclass(frozen=True)
class Candidate:
    id: str
    entry_id: str
    group: str | None
    text: str
    source: str  # "baseline" | "backlog"
    # Backlog candidates always end with ``BULLET_FORM_SUFFIX``; the long-form
    # text never becomes a candidate.


@dataclass(frozen=True)
class Entry:
    id: str
    role: str
    meta: str
    candidates: tuple[Candidate, ...]
    baseline_bullets: tuple[str, ...] = ()


@dataclass
class Catalog:
    language: str
    baseline_path: Path
    headline: str
    entries: list[Entry]
    fixed_context: dict
    notes: list[str] = field(default_factory=list)

    def entry(self, entry_id: str) -> Entry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def candidate(self, candidate_id: str) -> Candidate | None:
        for entry in self.entries:
            for cand in entry.candidates:
                if cand.id == candidate_id:
                    return cand
        return None

    def candidate_count(self) -> int:
        return sum(len(entry.candidates) for entry in self.entries)


def _fold_company(meta: str) -> str:
    head = meta.split("|", 1)[0]
    return head.split(",", 1)[0].strip().casefold()


def _slugify(text: str) -> str:
    lowered = text.casefold()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return cleaned or "entry"


def _entry_slug(role: str, meta: str) -> str:
    company = _fold_company(meta)
    match = RANGE.search(meta)
    suffix = ""
    if match:
        month, year = match.group(1).split(".")
        suffix = f"-{year}-{month}"
    return f"{_slugify(company)}{suffix}"


def _load_baseline(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"baseline input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"baseline input is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"baseline input must be a JSON object: {path}")
    if not isinstance(data.get("experience"), list):
        raise CatalogError(f"baseline input has no experience list: {path}")
    return data


def _blocks(text: str) -> list[tuple[str, str]]:
    """Split the backlog into ``(heading, body)`` blocks introduced by ``### ``."""
    lines = text.splitlines()
    out: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    heading = ""
    for line in lines:
        h3 = H3.match(line)
        if h3:
            if current is not None:
                out.append((heading, current))
            heading = h3.group(1)
            current = []
            continue
        if H2.match(line) and current is not None:
            out.append((heading, current))
            current = None
            heading = ""
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        out.append((heading, current))
    return [(h, "\n".join(b)) for h, b in out]


def _match_block(heading: str, meta: str) -> bool:
    folded = heading.casefold()
    company = _fold_company(meta)
    if company and company not in folded:
        return False
    match = RANGE.search(meta)
    if not match:
        return False
    heading_dates = set(DATE.findall(heading))
    return {match.group(1), match.group(2)} <= heading_dates


def _structured_items(body: str) -> list[tuple[int, str, str, str]]:
    """Return ``(number, title, detailed_text, short_text)`` per ``####`` item.

    Both forms are parsed so the split stays explicit, but callers only ever use
    ``short_text``: the detailed text is background prose, never a CV bullet.
    """
    items: list[tuple[int, str, str, str]] = []
    current: tuple[int, str] | None = None
    form: str | None = None
    detailed: list[str] = []
    short: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph and form is not None:
            text = " ".join(part.strip() for part in paragraph).strip()
            if text:
                paragraph.clear()
                if form == "detailed":
                    detailed.append(text)
                else:
                    short.append(text)
        paragraph.clear()

    def flush() -> None:
        nonlocal current, form, detailed, short
        flush_paragraph()
        if current is not None:
            number, title = current
            det = " ".join(detailed).strip()
            sho = " ".join(short).strip()
            items.append((number, title, det, sho))
        current, form, detailed, short = None, None, [], []

    for line in body.splitlines():
        item = ITEM.match(line)
        if item:
            flush()
            current = (int(item.group(1)), item.group(2))
            continue
        if current is None:
            continue
        if not line.strip():
            flush_paragraph()
            continue
        label = LABEL.match(line)
        if label:
            flush_paragraph()
            name = label.group(1)
            if DETAILED_HINT.search(name) and not SHORT_HINT.search(name):
                form = "detailed"
            elif SHORT_HINT.search(name):
                form = "short"
            else:
                form = None
            continue
        bullet = BULLET.match(line)
        if bullet:
            flush_paragraph()
            text = bullet.group(1).strip()
            if form == "detailed":
                detailed.append(text)
            else:
                short.append(text)
            continue
        if line.startswith(("**", "#")):
            flush_paragraph()
            continue
        paragraph.append(line)
    flush()
    return items


def _segments(text: str) -> list[dict]:
    """Split a backlog skill line into generator segments (``**bold**`` runs)."""
    segments: list[dict] = []
    position = 0
    for match in BOLD_SPAN.finditer(text):
        if match.start() > position:
            segments.append({"text": text[position:match.start()], "bold": False})
        segments.append({"text": match.group(1), "bold": True})
        position = match.end()
    if position < len(text):
        segments.append({"text": text[position:], "bold": False})
    if not segments:
        segments = [{"text": text, "bold": False}]
    return segments


def _sidebar_skills(text: str) -> list[dict]:
    """Parse the backlog's CV-sidebar skills section into generator groups.

    The section is introduced by a level-2 heading that names the technical
    skills, each group by a level-3 heading, and the values are its bullets.
    Returns an empty list when the section is absent or has no readable group.
    """
    groups: list[dict] = []
    label: str | None = None
    values: list[str] = []
    in_section = False

    def flush() -> None:
        nonlocal label, values
        if label and values:
            name = label if label.endswith(":") else f"{label}:"
            groups.append({"label": name, "segments": _segments(", ".join(values))})
        label, values = None, []

    for line in text.splitlines():
        heading = H2_TITLE.match(line)
        if heading:
            flush()
            in_section = bool(SIDEBAR_SKILLS_H2.search(heading.group(1)))
            continue
        if not in_section:
            continue
        group = H3.match(line)
        if group:
            flush()
            label = group.group(1)
            continue
        bullet = BULLET.match(line)
        if bullet and label is not None:
            values.append(bullet.group(1).strip())
    flush()
    return groups


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass(frozen=True)
class Identity:
    """The applicant identity for one language, read from the profile baseline."""

    first_name: str
    last_name: str
    headline: str


def load_identity(
    *,
    language: str,
    baseline_path: Path,
    headline_override: str | None = None,
) -> Identity:
    """Read ``first_name``/``last_name``/``headline`` from the profile baseline.

    This runs once at start-up, before the folder scan: the scan names the CV
    files, and their ``{name}`` part comes from exactly these values.  A missing
    headline is a clear error unless ``cv.toml`` overrides it.
    """
    baseline = _load_baseline(baseline_path)
    _check_language(baseline, baseline_path, language)
    first_name = str(baseline.get("first_name") or "").strip()
    last_name = str(baseline.get("last_name") or "").strip()
    if not first_name or not last_name:
        raise CatalogError(
            f"baseline {baseline_path} must carry a first_name and a last_name; "
            f"the CV file name is built from them"
        )
    headline = (headline_override or "").strip() or str(baseline.get("headline") or "").strip()
    if not headline:
        raise CatalogError(
            f"baseline {baseline_path} has no 'headline' and cv.toml sets none; "
            f"add one of them (the headline is never taken from a posting)"
        )
    if headline_override:
        headline = str(headline_override).strip()
    return Identity(first_name=first_name, last_name=last_name, headline=headline)


def _check_language(baseline: dict, baseline_path: Path, language: str) -> None:
    if baseline.get("language") not in (None, language):
        raise CatalogError(
            f"baseline {baseline_path} is marked {baseline.get('language')!r} but {language!r} was requested"
        )


def build_catalog(
    *,
    language: str,
    baseline_path: Path,
    backlog_path: Path,
    headline_override: str | None = None,
) -> Catalog:
    baseline = _load_baseline(baseline_path)
    _check_language(baseline, baseline_path, language)
    # The headline is a fixed statement about the applicant: it comes from the
    # profile baseline (or the ``cv.toml`` override) and is identical for every
    # CV of a run, so no posting can change or overflow it.
    headline = (headline_override or "").strip() or str(baseline.get("headline") or "").strip()
    if not headline:
        raise CatalogError(
            f"baseline {baseline_path} has no 'headline' and cv.toml sets none; "
            f"add one of them (the headline is never taken from a posting)"
        )

    backlog_text = ""
    notes: list[str] = []
    if backlog_path.is_file():
        backlog_text = backlog_path.read_text(encoding="utf-8")
    else:
        notes.append(f"backlog not found: {backlog_path}")

    blocks = _blocks(backlog_text)
    entries: list[Entry] = []
    used_headings: set[str] = set()

    for index, raw in enumerate(baseline["experience"], start=1):
        if not isinstance(raw, dict):
            raise CatalogError(f"experience[{index}] in {baseline_path} is not an object")
        role = str(raw.get("role", "")).strip()
        meta = str(raw.get("meta", "")).strip()
        bullets = tuple(str(b).strip() for b in raw.get("bullets", []) if str(b).strip())
        entry_id = f"exp/{_entry_slug(role, meta)}"
        candidates: list[Candidate] = []
        baseline_candidates = [
            Candidate(
                id=f"{entry_id}/b{i}",
                entry_id=entry_id,
                group=None,
                text=text,
                source="baseline",
            )
            for i, text in enumerate(bullets, start=1)
        ]

        matched = None
        for heading, body in blocks:
            if heading in used_headings:
                continue
            if _match_block(heading, meta):
                matched = (heading, body)
                break

        structured: list[Candidate] = []
        if matched is not None:
            heading, body = matched
            used_headings.add(heading)
            seen_text: set[str] = set()
            for number, _title, detailed, short in _structured_items(body):
                group = f"{entry_id}/el{number}"
                # HARD RULE: only the marked bullet form is a CV bullet.  The
                # long form is background prose and is never offered, so the
                # selector cannot pick it and no long text can reach the CV.
                if not short:
                    if detailed:
                        notes.append(
                            f"{language}: backlog item {group} has no bullet form; "
                            f"the long form is never used"
                        )
                    continue
                if _normalize(short) in seen_text:
                    continue
                seen_text.add(_normalize(short))
                structured.append(
                    Candidate(
                        id=f"{group}{BULLET_FORM_SUFFIX}",
                        entry_id=entry_id,
                        group=group,
                        text=short,
                        source="backlog",
                    )
                )
            if not structured:
                notes.append(
                    f"{language}: backlog section {heading!r} has no bullet-form items; "
                    f"using the baseline bullets for {entry_id}"
                )
        else:
            notes.append(
                f"{language}: no backlog section matched {entry_id} ({role}); "
                f"using the baseline bullets"
            )

        candidates = structured if structured else baseline_candidates
        entries.append(
            Entry(
                id=entry_id,
                role=role,
                meta=meta,
                candidates=tuple(candidates),
                baseline_bullets=bullets,
            )
        )

    baseline_skills = baseline.get("skills", [])
    backlog_skills = _sidebar_skills(backlog_text)
    if backlog_skills:
        skills = backlog_skills
    else:
        skills = baseline_skills
        notes.append(
            f"{language}: backlog has no readable CV-sidebar skills section; "
            f"using the baseline skills"
        )

    fixed_context = {
        "headline": headline,
        "skills": skills,
        "languages": baseline.get("languages", []),
        "education": baseline.get("education", []),
        "certificates": baseline.get("certificates", []),
    }
    return Catalog(
        language=language,
        baseline_path=baseline_path,
        headline=headline,
        entries=entries,
        fixed_context=fixed_context,
        notes=notes,
    )


def baseline_for(language: str, profile_dir: Path) -> Path:
    """The reviewed baseline input of one language, inside the private profile."""
    return Path(profile_dir) / "inputs" / f"current_{language}.json"


def backlog_for(language: str, backlog_de: Path, backlog_en: Path) -> Path:
    return backlog_de if language == "de" else backlog_en


def sidebar_summary(catalog: Catalog) -> list[str]:
    """Compact, read-only context lines shown to the model."""
    lines: list[str] = []
    for group in catalog.fixed_context.get("skills", []):
        label = str(group.get("label", "")).rstrip(":")
        text = "".join(str(seg.get("text", "")) for seg in group.get("segments", []))
        if text.strip():
            lines.append(f"{label}: {text.strip()}")
    return lines


__all__ = [
    "BULLET_FORM_SUFFIX",
    "LONG_FORM_SUFFIX",
    "Catalog",
    "CatalogError",
    "Candidate",
    "Entry",
    "Identity",
    "backlog_for",
    "baseline_for",
    "build_catalog",
    "load_identity",
    "sidebar_summary",
]
