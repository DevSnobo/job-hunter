# Your experience backlog

The backlog is the private Markdown file that holds the CV bullet candidates
`./cv` may select from, plus (optionally) the sidebar skills. Together with
`inputs/current_<language>.json` (the reviewed baseline) it is the only content
the model ever sees of you; the model returns candidate ids, never text.

The baseline owns identity, contacts, photo, role titles and the sidebar unless
the backlog overrides the sidebar. The backlog adds *selectable* bullet text.
The candidate ids are described in
[architecture.md](architecture.md#candidate-ids).

> **The hard rule.** A CV bullet comes only from text under a short-form label
> (like `**Resume bullet**` / `**Lebenslaufpunkt**`) or from a plain `- ` bullet
> line inside a matched section. The long form is background prose: it is not
> offered, and its id is refused if a model returns it anyway.

## Where it lives

| language | file | `cv.toml` | flag |
|---|---|---|---|
| `de` | `<profile>/backlogs/career-knowledge-backlog-DE.md` | `[paths] backlog_de` | `--backlog-de` |
| `en` | `<profile>/backlogs/career-knowledge-backlog-EN.md` | `[paths] backlog_en` | `--backlog-en` |

Markdown, UTF-8. A missing file is not an error: the run warns and uses the
baseline bullets everywhere. The file is private; both it and the whole
`profile/` directory are gitignored.

## Start from an example

| starting point | what it is |
|---|---|
| `examples/profile/` | a complete, runnable synthetic profile (baseline, backlogs, photo): `cp -r examples/profile profile` gives you a working chain to watch end to end |
| `examples/profile/backlogs/career-knowledge-backlog-DE.md.example` (and `-EN.md.example`) | a fill-in skeleton with placeholder entries and the two labels |
| `examples/profile/backlogs/career-knowledge-backlog-DE.md` (and `-EN.md`) | the same skeleton filled in with the synthetic Ada Lovelace material |

```bash
# 1. a private profile and one sample posting (fresh clone: profile/ must not
#    exist yet, and nothing tracked is written)
cp -r examples/profile profile
mkdir -p applications
cp -r examples/sample-application applications/

# 2. see the plan, the candidate count and every backlog warning
./cv --dry-run --all --language de

# 3. a real run needs a CLI agent: fill in [agent] command in config/cv.toml
#    (examples are in the comments of config/cv.toml.example), then:
./cv --all --language de --yes
```

Step 3 writes `applications/sample-application/CV_AdaLovelace_DE_sample-application.pdf`
with the example backlog's bullets. To dry-run the example profile without
copying it, `docs/development.md#the-examples` shows the throwaway config.

`./cv --dry-run` prints the plan, the candidate count and every warning, so it
is the loop for writing a backlog: fill in a section, dry-run, read the
warnings, repeat.

## The format

### The dated section (`###`)

One `###` heading per job, and the heading must name **the company and both
`mm.yyyy` dates of one baseline entry's `meta` line**. The rule, exactly:

1. The company is the part of the baseline entry's `meta` before the first `|`,
   cut again at the first `,`, compared case-insensitively.
2. The heading must contain that company and both dates of the `meta` range.
3. The first matching heading wins, and a heading is used for at most one entry.

```markdown
### 01.2020 – 12.2023 — Example GmbH, Musterstadt
```

That heading matches the entry with
`"meta": "Example GmbH, Musterstadt | 01.2020 - 12.2023"`. A typo in a date or a
company name is not fatal: the entry falls back to the baseline bullets with a
warning, which is why an unmatched section is visible instead of silent.

A `meta` line that names a *present* job (`present`, German `heute`) has no
range, so **no** section can match it: its bullets always come from the input
JSON. Keep those in `inputs/current_<language>.json`.

### The items (`#### N. Title`)

Inside a matched section, every `#### N. Title` item is one candidate group:

- `N` builds the group `exp/<slug>/el<N>`; candidates of one section are the
  alternatives of that entry, and the model may pick at most one per group.
- The item's short form becomes `exp/<slug>/el<N>-short`, the id the model
  returns.
- Keep `N` unique inside a section. Two items with the same number produce the
  *same* candidate id (`…/el<N>-short`), and the model's answer then always
  resolves to the first of them.

### The labels decide the form

A bold label line on its own (`**...**`) switches the form for everything that
follows it:

| form | how the label is recognised | example labels |
|---|---|---|
| long | contains `ausf`, `detail` or `long` (and no short hint) | `Ausführliche Version`, `Detailed version`, `Long form` |
| short | contains `bullet`, `point`, `punkt`, `resume` or `kurz` | `Lebenslaufpunkt`, `Resume bullet`, `Bullet point` |

Matching is case-insensitive and by word fragment, so the label is not a fixed
string. What each form does:

- **short** — paragraphs and `- ` bullets under it become selectable candidates.
- **long** — never selectable; the text is background for you and is skipped.
- **any other bold label** — paragraphs under it are dropped, but `- ` bullets
  under it still count as the short form.
- **no label yet** — a `- ` bullet still counts as the short form, so an
  unlabelled bullet list in a matched section *is* allowed into a CV. If you
  want every CV line to be explicitly reviewed, put it under a short-form label.

Identical short texts inside one item are offered once. An item with no short
text is skipped with a warning.

```markdown
#### 1. Test automation of the web console

**Detailed version**

Built a Selenium suite that drove the internal web console through its main
workflows. Background only - this never reaches a CV.

**Resume bullet**

- Test automation of an internal web console with Selenium, run nightly
```

### The sidebar skills (`##`)

The sidebar is not selected by the model - it is fixed content. It comes from
one `##` heading whose text contains `technical skills` or `technische skills`:

- every `###` heading below it is a skill group (`label`),
- the `- ` bullets below that heading are the values, joined with `", "`,
- `**bold**` marks the bold part of a value.

```markdown
## 2. Technische Skills — Seitenleiste des Lebenslaufs

### Sprachen & Frameworks

- **Java, Spring Boot**, JakartaEE
```

Without that section the baseline `skills` from the input JSON are used, with a
warning.

## When something does not match

Every fallback keeps the run working and says so. A real run prints these as
`warn: ...` lines, and `--dry-run` prints the same text:

| situation | what happens |
|---|---|
| the backlog file is missing | `warn: <language>: backlog not found: <path>`; baseline bullets everywhere |
| no section matches an entry | `warn: <language>: no backlog section matched <entry id> (<role>); using the baseline bullets` |
| a matched section has no short form at all | `warn: <language>: backlog section '<heading>' has no bullet-form items; using the baseline bullets for <entry id>` |
| an item has only a long form | `warn: <language>: backlog item <group> has no bullet form; the long form is never used` |
| no sidebar section | `warn: <language>: backlog has no readable CV-sidebar skills section; using the baseline skills` |

## Common mistakes

- A heading date that does not match the baseline `meta` (including a `present`
  job, which has no range) - the section is ignored, with a warning.
- A `- ` bullet list placed outside a `####` item: `####` items are the only
  candidate source in a matched section.
- Assuming an unmarked bullet is safe: it is offered (see the table above).
- Putting `####` items in an unmatched section; nothing there is read.
- Forgetting the `##` heading with `Technical skills` / `Technische Skills`; the
  sidebar falls back to the input JSON, with a warning.
