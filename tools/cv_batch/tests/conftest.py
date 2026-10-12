"""Shared test fixtures: temporary project trees and fake subprocesses.

The profile data here is synthetic (Ada Lovelace): the repository ships no real
personal data, and the CV file names in these tests are built from the profile
identity and the ``target_template`` constant instead of a hard-coded name.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cv_batch.config import DEFAULT_TARGET_TEMPLATE, TargetNaming, name_slug
from cv_batch.model import AgentAdapter

TESTS_DIR = Path(__file__).resolve().parent
FAKES = TESTS_DIR / "fakes"

BASELINE_DE = {
    "schema_version": 1,
    "language": "de",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "headline": "Software Developer",
    "photo": "photo.jpg",
    "contacts": [{"kind": "email", "lines": ["test@example.invalid"]}],
    "languages": [{"name": "Deutsch", "level": "Muttersprache"}],
    "skills": [
        {
            "label": "Sprachen & Frameworks:",
            "segments": [{"text": "Java", "bold": True}, {"text": ", Guice", "bold": False}],
        }
    ],
    "experience": [
        {
            "role": "Software Developer",
            "meta": "Analytical Engines Ltd, Berlin | 11.2025 - 12.2026 | verfügbar ab 01.2027",
            "bullets": ["Basis-Punkt A", "Basis-Punkt B"],
        },
        {
            "role": "Software Developer Backend",
            "meta": "Numerical Machines GmbH, Leipzig | 09.2022 - 10.2025",
            "bullets": ["Maven Builds", "WildFly Deployments"],
        },
    ],
    "education": [{"title": "University of Example | Bachelor of Science - Informatik", "dates": "2017 – 2022"}],
    "certificates": [{"title": "Certified Professional for Software Architecture"}],
    "signature": None,
}

BASELINE_EN = {
    "schema_version": 1,
    "language": "en",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "headline": "Software Developer",
    "contacts": [{"kind": "email", "lines": ["test@example.invalid"]}],
    "languages": [{"name": "German", "level": "Native"}],
    "skills": [{"label": "Languages:", "segments": [{"text": "Java", "bold": True}]}],
    "experience": [
        {
            "role": "Software Developer",
            "meta": "Analytical Engines Ltd, Berlin | 11.2025 - 12.2026 | available from 01.2027",
            "bullets": ["Baseline bullet A", "Baseline bullet B"],
        },
        {
            "role": "Software Developer Backend",
            "meta": "Numerical Machines GmbH, Leipzig | 09.2022 - 10.2025",
            "bullets": ["Maven builds", "WildFly deployments"],
        },
    ],
    "education": [{"title": "University of Example | Bachelor of Science - Computer Science", "dates": "2017 – 2022"}],
    "certificates": [{"title": "Certified Professional for Software Architecture"}],
    "signature": None,
}

#: The identity slug the tool builds from the profile above (``Ada`` + ``Lovelace``).
NAME_SLUG = name_slug(BASELINE_DE["first_name"], BASELINE_DE["last_name"])


def target_name(folder: str, language: str, name: str = NAME_SLUG) -> str:
    """The CV file name the tool must predict, built from the template constant."""
    return DEFAULT_TARGET_TEMPLATE.format(name=name, language=language.upper(), folder=folder)


def target_path(directory: Path, language: str, name: str = NAME_SLUG) -> Path:
    """The expected CV path inside ``directory``."""
    return directory / target_name(directory.name, language, name)

BACKLOG_DE = """# Karriere-Backlog

## 1. Beruflicher Werdegang

### 11.2025 – 12.2026 — Analytical Engines Ltd, Berlin

Deutsche Uebersetzungen der abgestimmten Eintraege.

#### 1. Mock-Implementierung

**Ausführliche Version**

Ausfuehrliche Beschreibung der Mock-Implementierung fuer andere Teams.

**Lebenslaufpunkt**

- Mock-Implementierung einer internen API fuer Tests anderer Teams.

#### 2. API-Design

**Ausführliche Version**

Ausfuehrliche Beschreibung des API-Designs.

**Lebenslaufpunkt**

- API-Design auf Basis der Anforderungen der Aufrufer.

#### 3. Builds

**Ausführliche Version**

Ausfuehrliche Beschreibung der Builds.

**Lebenslaufpunkt**

- Umstrukturierung von Gradle Builds.

### 09.2022 – 10.2025 — Numerical Machines GmbH, Leipzig

- Wartung der EAI Software
- Anpassung der Maven Builds

### 03.2022 – 08.2022 — Numerical Machines GmbH, Leipzig

- Evaluation Hexagonale Architektur

## 2. Technische Skills — Seitenleiste des Lebenslaufs

### Sprachen & Frameworks

- **Java, Spring Boot**, JakartaEE

### Build & Tools

- Git, Maven, Gradle
"""

BACKLOG_EN = """# Career knowledge backlog

## 1. Work timeline

### 11.2025 – 12.2026 — Analytical Engines Ltd, Berlin

Nine jointly approved entries.

#### 1. Mock implementation

**Detailed version**

Detailed description of the mock implementation for other teams.

**Resume bullet**

- Mock implementation of an internal API for other teams.

#### 2. API design

**Detailed version**

Detailed description of the API design work.

**Resume bullet**

- API design based on caller needs.

#### 3. Builds

**Detailed version**

Detailed description of the build restructuring.

**Resume bullet**

- Gradle build restructuring.

### 09.2022 – 10.2025 — Numerical Machines GmbH, Leipzig

- Maintenance of the EAI software
- Maintenance of the Maven builds

### 03.2022 – 08.2022 — Numerical Machines GmbH, Leipzig

- Evaluation of hexagonal architecture

## 2. Technical skills — CV sidebar

### Languages & Frameworks

- **Java, Spring Boot**, JakartaEE

### Build & Tools

- Git, Maven, Gradle
"""


@dataclass
class Project:
    root: Path
    applications: Path
    profile: Path
    generator: Path
    state: Path
    backlog_de: Path
    backlog_en: Path

    @property
    def inputs(self) -> Path:
        return self.profile / "inputs"

    def target(self, folder: str, language: str, name: str = NAME_SLUG) -> Path:
        """The CV file the tool is expected to write for ``folder``."""
        return self.applications / folder / target_name(folder, language, name)

    def naming(self, language: str = "de", template: str = DEFAULT_TARGET_TEMPLATE) -> TargetNaming:
        """The naming the CLI builds for this project: slug from the profile."""
        baseline = json.loads(
            (self.inputs / f"current_{language}.json").read_text(encoding="utf-8")
        )
        return TargetNaming(
            name=name_slug(baseline["first_name"], baseline["last_name"]), template=template
        )

    def add_app(self, folder: str, posting: str | None = None, source: str | None = "https://example.invalid/job") -> Path:
        directory = self.applications / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "posting.txt").write_text(
            posting if posting is not None else f"Stellenanzeige fuer {folder}: Java Backend Developer gesucht.\n",
            encoding="utf-8",
        )
        if source is not None:
            (directory / "source.txt").write_text(source + "\n", encoding="utf-8")
        return directory

    def kwargs(self) -> dict:
        return {
            "job_root": self.root,
            "applications": self.applications,
            "profile_dir": self.profile,
            "backlog_de": self.backlog_de,
            "backlog_en": self.backlog_en,
            "generator_project": self.generator,
            "state_dir": self.state,
        }


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A complete temporary project tree in the post-refactor layout.

    ``profile/`` holds the private inputs, the generator lives below
    ``tools/cv_generator`` and the tool state is the top-level ``state/``.
    """
    root = tmp_path / "jobs"
    applications = root / "applications"
    profile = root / "profile"
    generator = root / "tools" / "cv_generator"
    state = root / "state"
    applications.mkdir(parents=True)
    (profile / "inputs").mkdir(parents=True)
    (profile / "inputs" / "current_de.json").write_text(json.dumps(BASELINE_DE, ensure_ascii=False), encoding="utf-8")
    (profile / "inputs" / "current_en.json").write_text(json.dumps(BASELINE_EN, ensure_ascii=False), encoding="utf-8")
    generator.mkdir(parents=True)
    backlog_de = profile / "backlogs" / "career-knowledge-backlog-DE.md"
    backlog_en = profile / "backlogs" / "career-knowledge-backlog-EN.md"
    backlog_de.parent.mkdir(parents=True, exist_ok=True)
    backlog_de.write_text(BACKLOG_DE, encoding="utf-8")
    backlog_en.write_text(BACKLOG_EN, encoding="utf-8")
    return Project(root, applications, profile, generator, state, backlog_de, backlog_en)


class FakeAgent:
    """Controls the fake prime-agent subprocess through a scenario directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.exe = str(FAKES / "fake_agent.py")

    def steps(self, steps: list[dict]) -> None:
        (self.directory / "steps.json").write_text(json.dumps(steps), encoding="utf-8")
        (self.directory / "counter.txt").write_text("0", encoding="utf-8")
        barrier = self.directory / "barrier.txt"
        if barrier.exists():
            barrier.unlink()

    def calls(self) -> list[dict]:
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def env(self) -> dict:
        return {"FAKE_AGENT_SCENARIO": str(self.directory)}


@pytest.fixture
def fake_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    agent = FakeAgent(tmp_path / "agent-scenario")
    agent.steps([])
    monkeypatch.setenv("FAKE_AGENT_SCENARIO", str(agent.directory))
    return agent


class FakeRenderer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.exe = str(FAKES / "fake_render.py")
        self.configure()

    def configure(
        self,
        *,
        overflow_times: int = 0,
        fail: bool = False,
        junk: bool = False,
        empty: bool = False,
        sleep: float = 0.0,
        descendant: str | None = None,
        descendant_sleep: float = 2.5,
        layouts: tuple[str, ...] = ("classic",),
    ) -> None:
        (self.directory / "config.json").write_text(
            json.dumps(
                {
                    "overflow_times": overflow_times,
                    "fail": fail,
                    "junk": junk,
                    "empty": empty,
                    "sleep": sleep,
                    "descendant": descendant,
                    "descendant_sleep": descendant_sleep,
                    # layout names the fake generator knows; cv_batch forwards
                    # the name verbatim, the fake mirrors the real validation
                    "layouts": list(layouts),
                }
            ),
            encoding="utf-8",
        )
        (self.directory / "render_calls.txt").write_text("0", encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.directory / "render_calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def fake_renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRenderer:
    renderer = FakeRenderer(tmp_path / "render-scenario")
    monkeypatch.setenv("FAKE_RENDER_SCENARIO", str(renderer.directory))
    return renderer


def prime_agent_command(exe) -> str:
    """A shell-style command for a fake prime-agent-style executable.

    The tool runs it verbatim (shlex-split); the fake reads its scenario from the
    environment and ignores its argv, so only the shape has to be plausible.
    """
    parts = [str(exe), "-p", "--mode", "json", "--system-prompt", "{system_prompt}", "--", "{prompt}"]
    return " ".join(part if part.startswith("{") else shlex.quote(part) for part in parts)


def prime_agent_adapter(exe) -> AgentAdapter:
    """The same command as an argument list, for tests that build a Runner directly."""
    return AgentAdapter(
        [str(exe), "-p", "--mode", "json", "--system-prompt", "{system_prompt}", "--", "{prompt}"],
        format="prime-agent-json",
    )


def selection_answer(entry_ids: dict[str, list[str]], headline: str | None = None) -> str:
    """Helper that builds a valid strict-JSON model answer.

    The headline is fixed by the tool, so it is normally absent; ``headline`` is
    kept to prove that a model-supplied value is ignored.
    """
    payload: dict = {
        "experience": [{"entry": entry, "bullets": bullets} for entry, bullets in entry_ids.items()],
    }
    if headline is not None:
        payload["headline"] = headline
    return json.dumps(payload)


def el_entry_id() -> str:
    """Entry id of the first baseline entry (``meta`` -> ``exp/<slug>``)."""
    return "exp/analytical-engines-ltd-2025-11"


def sp_entry_id() -> str:
    """Entry id of the second baseline entry (``meta`` -> ``exp/<slug>``)."""
    return "exp/numerical-machines-gmbh-2022-09"


def tree_snapshot(path: Path) -> dict[str, tuple[int, int, bytes]]:
    """Folder snapshot: name -> (size, mtime_ns, content) for change detection."""
    out: dict[str, tuple[int, int, bytes]] = {}
    for item in sorted(path.rglob("*")):
        if item.is_file():
            stat = item.stat()
            out[str(item.relative_to(path))] = (stat.st_size, stat.st_mtime_ns, item.read_bytes())
    return out


def python_exe() -> str:
    return sys.executable
