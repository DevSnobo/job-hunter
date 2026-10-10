# Career knowledge backlog — Ada Lovelace

Synthetic example, in the style of a real private backlog. Every company, date
and bullet in this file is invented, so the public repository never carries real
personal data. Copy `examples/profile/` to `profile/` and replace all of it with
your own material.

The English file is read for `--language en`; the German file is read for
`--language de`. See [German version](./career-knowledge-backlog-DE.md).

## How this file is read

- A `###` heading that names a company and both dates of a CV entry matches
  that entry of `inputs/current_en.json`.
- Inside a matched section, every `#### N. Title` item offers its
  **Resume bullet** text as one candidate for the model to pick. The
  **Detailed version** text is background for you: it is never offered and can
  never reach a CV.
- The sidebar skills come from section 2 below. Without that section the
  baseline values in `inputs/current_en.json` are used.

## 1. Work timeline

### 07.2018 – 02.2022 — Analytical Engines, Berlin

**Working student — Software Developer Backend / Quality Assurance**

Date line on the CV: `07.2018 - 02.2022 / 06.2017 – 06.2018`.

Four approved entries. Use the short form in a CV.

#### 1. Test automation of the web console

**Detailed version**

Built a Selenium suite that drove the internal web console through its main
workflows and reported failures before the team started work. The suite replaced
a manual checklist that took half a day before every release.

**Resume bullet**

- Test automation of an internal web console with Selenium, run nightly

#### 2. Integration of a third-party reporting service

**Detailed version**

Connected a third-party reporting service to the platform: mapped its payload to
the internal model and handled its rate limits and retries.

**Resume bullet**

- Integration of a third-party reporting service into the existing API

#### 3. Manual test plans for releases

**Detailed version**

Wrote and executed the manual test plan of every release and kept the checklist
current while features changed.

**Resume bullet**

- Manual release test plans, written and executed

#### 4. Migration of the build to a newer JDK

**Detailed version**

Moved the build from an old JDK to the next long-term release and fixed the
resulting deprecation warnings.

*(This item has no bullet form on purpose. It stays background: the selector
never sees it, because only a marked **Resume bullet** becomes a candidate.)*

### 03.2022 – 08.2022 — Analytical Engines, Berlin

**Bachelor Thesis — Software Developer Backend**

One approved entry.

#### 1. Hexagonal architecture in a monolith

**Detailed version**

Evaluated how a hexagonal architecture inside a monolithic application allows
decoupled plugins, and described the migration steps in the thesis.

**Resume bullet**

- Evaluation of a hexagonal architecture for decoupled plugins in a monolith

### 09.2022 – present — Analytical Engines, Berlin

This entry has no `#### N.` items: the bullets of `inputs/current_en.json` are
the candidates. A date line that says `present` matches no dated section here,
which is why the baseline bullets are used unchanged.

## 2. Technical skills — CV sidebar

The values below replace the sidebar skills of the baseline input. `**bold**`
marks the bold run on the CV.

### Languages & Frameworks

- **Java**, JakartaEE, Guice

### Build & Tools

- **Git, Maven**, IntelliJ, Shell, Linux

### Testing & Quality

- **JUnit 4/5**, SonarQube, Selenium

### DevOps & Deployment

- **Docker**, Docker Compose, Bitbucket Pipelines

### Methods

- **Scrum**, Agile, Jira

### Soft Skills

- **Mentoring, analytical & objectoriented thinking**

## 3. Education and certificates

- University of Example — Bachelor of Science, Computer Science (2017 – 2022)
- Certified Professional for Software Architecture, Foundation Level

## 4. Notes

- Keep every claim true. The tool checks structure, not truth.
- Facts that must never change belong in `inputs/current_en.json`, not here.
