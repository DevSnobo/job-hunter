# Architecture

The toolchain has two halves that meet in one place on disk. The tracker
(`./job`) keeps the history of every application in SQLite. The CV chain
(`./posting`, `./cv`, `./cvgen`) keeps its data as files: one folder per
application under `applications/`, and one private profile under `profile/`.

The application **folder** is the join key between the two halves.

## The four tools

| launcher | project | reads | writes |
|---|---|---|---|
| `./job` | `tools/job_cli` | `data/job.sqlite3`, `sql/queries/*.sql`, `applications/` | The database, `data/backups/`, `exports/` |
| `./posting` | `tools/posting_entry` | your text input, the clipboard | `applications/<company>[_<posting-id>]/posting.txt` and `source.txt` |
| `./cv` | `tools/cv_batch` | `applications/`, `profile/`, `tools/cv_generator` | the tailored PDFs in the folder, `state/` |
| `./cvgen` | `tools/cv_generator` | one CV JSON file, `profile/photos/` | one PDF in `exports/generator/` |

Each launcher resolves the repository root and runs its workspace member. See
[layout](layout.md) for the launcher contents and the root rules.

## The application folder is the join key

`./posting` owns the folder name: `<company>` or `<company>_<posting-id>`, built
from what you type. `./job`, `./cv` and `./cvgen` all use that exact name, so
one folder is one application for every tool.

The tracker stores the link in `applications.intake_folder` (one plain folder
name, or NULL when you entered the application by hand).

### The link step

`./job` links applications to their folders when it starts. The step runs for
every command except `job db ...`, and `--no-link` skips it:

```text
$ ./job --help
  --no-link    Do not link applications to their applications/ folders on
               startup.
```

The link step is `job_cli.cli._reconcile_intake_links`. It:

1. scans `applications/` (`job_cli.intake.scan`) and skips hidden entries,
   symlinks, plain files, folders without `posting.txt` or `source.txt`, and the
   UUID folders of registered applications;
2. matches each unlinked folder to an unlinked application
   (`job_cli.intake.plan_links`) with two conservative passes:
   the folder `source.txt` URL equals the application `job_url`, or the employer
   matches and exactly one application and one folder carry that employer;
3. writes the match with `repo.update_application(..., intake_folder=...)` and
   prints one line per link.

Anything ambiguous stays unlinked on purpose, and `./job intake` shows it.
`./job edit <id> --set intake_folder=<name>` corrects a link by hand.

Note that the link step is **not** in `job_cli/artifacts.py`. That module handles
the other direction: copying a file into an application folder and registering
its path.

### `job list --json` and `job show --json`

`./job list --json` prints one object per application, straight from
`sql/queries/application_overview.sql`:

```json
{
  "id": "0f3c1c0e-...", "company": "Example Analytics GmbH", "role": "Backend Engineer",
  "current_status": "in_progress", "submitted_on": "2026-01-05", "status_since": "2026-01-05",
  "last_activity_on": "2026-01-05", "completed_interviews": 0, "completed_screens": 0,
  "completed_assessments": 0, "next_appointment": null
}
```

`./job show <ident> --json` adds the folder link, the history and the files:

```json
{
  "application": { "...": "...", "intake_folder": "Example Analytics GmbH_4711" },
  "timeline": [ { "event_id": null, "event_type": "submitted", "...": "..." } ],
  "rounds": [ { "id": 1, "participants": ["hiring_manager"], "...": "..." } ],
  "artifacts": [ { "artifact_type": "cv", "relative_path": "applications/<uuid>/cv.pdf" } ],
  "posting_file": "applications/Example Analytics GmbH_4711/posting.txt"
}
```

### `relative_path`

`relative_path` is the path of a stored file **relative to the job root**, with
a forward slash, and it always starts with `applications/`. It is never
absolute, so the database stays portable between machines. See
[layout](layout.md) for the `CHECK` constraint that enforces the shape.

## The profile is the single source of identity and content

`profile/` holds everything personal. `./cv` and `./cvgen` only read it.

| path | content |
|---|---|
| `profile/inputs/current_de.json`, `current_en.json` | The reviewed CV inputs, one per language. |
| `profile/photos/` | The photo. The input names it in the `photo` field. |
| `profile/backlogs/career-knowledge-backlog-DE.md`, `-EN.md` | Candidate entries and bullets, plus the sidebar skills. |

One input file is one complete CV page. Its shape is the generator schema
(`cv_generator.schema.CVData`):

```json
{
  "schema_version": 1, "language": "en",
  "first_name": "Ada", "last_name": "Lovelace", "headline": "Software Developer",
  "photo": "placeholder_photo.jpg",
  "contacts": [ { "kind": "email", "lines": ["ada@example.com"] } ],
  "languages": [ { "name": "German", "level": "Native" } ],
  "skills": [ { "label": "Languages:", "segments": [ { "text": "Java", "bold": true } ] } ],
  "experience": [ { "role": "Software Developer", "meta": "Example GmbH | 09.2022 - present",
                    "bullets": ["..." ] } ],
  "education": [ { "title": "...", "dates": null } ],
  "certificates": [ { "title": "..." } ],
  "signature": { "place_date": "London, 10.05.2025", "name": "Ada Lovelace" }
}
```

The split of responsibility is strict:

* **Identity, contacts, photo, skills, languages, education, certificates and the
  role titles are immutable.** They come from the input file and never from the
  model and never from a posting.
* **The bullet candidates have ids.** The backlog supplies them, and the model
  only returns ids. Python checks every id and renders the text.
* **The headline is fixed.** It comes from the input file (`headline`) or from
  `cv.toml` `headline`. The model may not change it.

### Candidate ids

`cv_batch.catalog` builds the catalog. Every entry and every bullet has a stable
id:

| id | source | meaning |
|---|---|---|
| `exp/<slug>` | `inputs/current_<lang>.json` | One experience entry. |
| `exp/<slug>/b<N>` | the input file | Bullet N of the input file. |
| `exp/<slug>/el<N>-short` | the backlog | The marked bullet form of backlog item N. |
| `exp/<slug>/el<N>-detailed` | the backlog | The long form. Never selectable, and refused if returned. |

Two candidates that share a group (same `el<N>`) are alternatives and only one
of them can be selected. When a dated backlog section matches an entry, its
bullet forms replace the input bullets for that entry. When it does not match,
the input bullets stay, and the run prints a note.

## The CV pipeline

```text
applications/<folder>/posting.txt
        |
        v
catalog.py      build the catalog from profile/inputs/current_<lang>.json + profile/backlogs/
        |
        v
prompt.py       one system prompt + one user message with the candidate ids and the posting
        |
        v
model.py        the CLI agent (default: prime-agent in headless JSON mode), one call per job
        |
        v
build.py        validate the selected ids, rebuild the CV JSON from the fixed baseline
        |
        v
runner.py       write state/staging/<job>/input.json
        |
        v
render.py       run cvgen render --layout <name> in the generator project
        |
        v
publish         place the PDF next to posting.txt; write the evidence into state/
```

| module | job | artifact it writes |
|---|---|---|
| `cv_batch/catalog.py` | Build the catalog and the identity. Read-only. | nothing |
| `cv_batch/prompt.py` | Build the system prompt and the user message. The posting is data, and the system prompt says so. | nothing |
| `cv_batch/model.py` | Build the agent command, run one headless call and read the answer. The shipped adapters: `prime-agent` (`-p --mode json`, tools/skills/context disabled, `--system-prompt`, `--session-dir state/sessions`, JSON event stream) and a generic command template for any other CLI agent. | session files, through the agent (`prime-agent` adapter only) |
| `cv_batch/build.py` | Validate the answer, then build the generator JSON from the input file. | nothing |
| `cv_batch/render.py` | Run the renderer on a staging path and place the result without clobbering. | `state/staging/<job>/input.json` and the staged PDF |
| `cv_batch/runner.py` | Own one job end to end and write the evidence. | `state/inputs/<slug>.json`, `state/runs/<stamp>-<slug>.json` |
| `cv_batch/apps.py` | Scan `applications/` and classify the existing CV file: missing, valid, or invalid. | nothing |
| `cvgen` | Render the JSON to one A4 page. | the PDF |

The agent call is one bounded message. The tool sends the candidate ids, the
fixed headline, the immutable sidebar content and the posting text. It requires
strict JSON back:

```json
{"experience": [{"entry": "exp/software-developer-backend", "bullets": ["exp/software-developer-backend/el1-short"]}]}
```

Five validation rules matter, because they are what makes the model safe:

1. every `entry` id must exist;
2. every bullet id must exist and belong to that entry;
3. an id ending in `-detailed` is refused outright;
4. two candidates with the same group cannot both be selected;
5. the entry order is the catalog order (most recent job first), never the order
   the model sent.

A rejected answer costs one bounded retry, with the validator message appended
to the prompt. `cv.toml [limits] content_retries` sets the count.

An overflowing page is handled the same way: the tool drops one selected bullet
and renders again, up to `[limits] trim_attempts` times. `--no-trim` makes an
overflow fail instead.

After a successful run, the evidence file records the exact generator JSON that
produced the PDF, the model, the token usage and the trims. That is what makes a
finished CV reproducible.

`./cv` details: [cv CLI](cv-cli.md). Renderer details and the flag list:
[cvgen CLI](cvgen-cli.md), [templates](templates.md).

## The SQLite schema

`sql/` is the single source of truth. Python never creates or changes a table.

### Tables

| table | purpose |
|---|---|
| `schema_migrations` | One row per applied migration: version, name, applied_at. |
| `applications` | One row per submitted application, plus `intake_folder`. `submitted_on` *is* the submission event. |
| `rounds` | One row per interview, screening or assessment, numbered inside its application. |
| `round_participants` | Which roles took part in a round (recruiter, hiring manager, team, ...). |
| `events` | The history: invitations, scheduling, completions, offers, rejections, notes. One row per recorded fact. |
| `event_types` | The event vocabulary: label, scope (`application`, `round`, `either`) and the state it sets. |
| `discovery_sources` | How you found the posting (LinkedIn, referral, ...). |
| `submission_channels` | How you applied (portal, email, ...). |
| `recruiter_types` | Recruiter involvement. |
| `round_types` | What kind of round it is (screen, live coding, ...). |
| `interview_formats` | Phone, video, on-site, asynchronous. |
| `participant_roles` | The roles a round can list. |
| `artifact_types` | The file vocabulary: `job_posting`, `cv`, `cover_letter`, `other`. |
| `artifacts` | One row per stored file: type, `relative_path`, label. |

The lookup tables (`discovery_sources` and the six others) hold data, not
schema: a new source or a new event type is an `INSERT`, not a table rebuild.

### Views

| view | purpose |
|---|---|
| `v_application_timeline` | The submission plus every event, as one ordered history: `timeline.sql` reads it. |
| `v_application_status` | The current state of one application, derived from its newest state-setting event, plus the counters `completed_interviews`, `completed_screens`, `completed_assessments`. |
| `v_round_status` | The current state of one round, plus the newest scheduled date, time and timezone. |

A view is a convenience, not history: to look at the past, filter the events
before ranking them, which is what `sql/queries/snapshot.sql` does.

### Triggers

| trigger | what it refuses |
|---|---|
| `events_validate_insert` | An event before the submission date; a round-scoped event without a round (or the reverse); an assessment event on a round that is not an assessment. |
| `events_validate_update` | The same three rules on an edit. |
| `applications_validate_submission_update` | Moving `submitted_on` to after an existing event. |
| `rounds_validate_phase_update` | Changing a phase when the existing events no longer fit it. |

The triggers are the last line of defence. `job_cli/rules.py` checks the scope,
phase, schedule and state rules earlier, so you get a readable message instead
of a `sqlite3.IntegrityError`.

### Read-only queries

`sql/queries/` holds seven files. Every one is parameterized and read-only, and
reporting loads it by name, never as text from a caller.

| file | what it answers |
|---|---|
| `application_overview.sql` | The browse table: status, dates, counters, next appointment. Filtered by `:status` and `:search`. |
| `timeline.sql` | The full history of one application (`:application_id`). |
| `upcoming.sql` | Rounds that still need attention, including past-due ones (`:as_of`). |
| `stale.sql` | Waiting applications with no activity for `:stale_days`. |
| `current_summary.sql` | One row per current outcome with interview counts. Aggregate only. |
| `snapshot.sql` | The cohort as it looked at `:as_of`, with `:cohort_from` and `:cohort_to`. This is corrected truth, not a frozen record. |
| `cohort_events.sql` | The raw cohort history for statistics. |

### The "no DDL in Python" contract

Schema changes live **only** in `sql/migrations/NNN_<name>.sql`. Python applies
them and nothing else. `job_cli/migrations.py` never builds a `CREATE TABLE`, and
the tests that guard the schema read the `.sql` files.

The runner adds safety rules on top:

* a migration file must be named `NNN_<name>.sql`, and the numbers must run
  1..N with no gap;
* the script must open exactly one transaction, and its `COMMIT` must be the
  final executable statement;
* the script must write its own `schema_migrations` row and set
  `PRAGMA user_version`;
* after each script the runner re-reads the committed `user_version` and the
  ledger row and compares them with the file;
* a database that carries a version the files do not have is refused as
  "created by newer tooling";
* a migration whose recorded name differs from the file name is refused, so a
  file cannot be renamed after it was applied. The runner stores no checksum:
  the name, the version and `user_version` are the guards.

Read `./job db status` and `./job db doctor` to see the state. Both are
read-only:

```text
$ ./job db status
database: <job root>/data/job.sqlite3
project_root: <job root>
size_bytes: 196608
sqlite_runtime: 3.53.4
user_version: 3
applied_migrations: {'001': 'initial_schema', '002': 'reporting_views', '003': 'intake_folder'}
pending_migrations: []
row_counts: {'applications': 29, 'rounds': 4, 'events': 14, 'artifacts': 0, 'round_participants': 1}
```

(The numbers are the counts of one example database; the shape is fixed.)

```text
$ ./job db doctor
{
  "integrity": [],
  "missing_files": [],
  "unregistered_files": [],
  "unsafe_paths": [],
  "untracked_folders": ["applications/<intake folder>/posting.txt", "..."]
}
```

`db doctor` groups what it finds:

| key | meaning |
|---|---|
| `integrity` | Problems from the SQLite integrity check and the foreign key check. |
| `missing_files` | A registered artifact whose file is gone. |
| `unregistered_files` | A file inside the folder of a registered application that no row describes. |
| `unsafe_paths` | A registered path that does not start with `applications/` or that contains `../`. |
| `untracked_folders` | Pre-submission intake folders. Normal, never an error. |

## The artifact model

A file becomes an artifact through `./job attach`:

```bash
./job attach <ident> ~/Downloads/posting.pdf --type job_posting
```

`job_cli/artifacts.py` copies the file, stages it in the target directory with a
temporary name, renames it atomically, and only then inserts the row. If the
insert fails, the copy is removed again. The source file is never moved and
never deleted.

| column | content |
|---|---|
| `id` | Row id. |
| `application_id` | The application. `ON DELETE CASCADE`. |
| `artifact_type` | One of `job_posting`, `cv`, `cover_letter`, `other`. |
| `relative_path` | The path from the job root, `applications/<folder>/<file>`. Unique. |
| `label` | Free text, optional. |
| `created_at` | Insert time. |

There is **no** `size` and no `sha256` column. The tracker records where a file
lives, not what it contains. The CV chain does not register its PDFs either:
`./cv` scans the folder and classifies the target file as missing, valid or
invalid by its `%PDF-` header and size.

Two safety rules apply to every file operation: a symlink inside `applications/`
is refused, and the resolved path must stay inside the applications directory.
A file that already exists is versioned (`cv.pdf`, `cv-2.pdf`, ...) unless you
pass `--overwrite`, and the old bytes are kept as
`<name>.replaced-<stamp>` during the swap.

## Where state lives

`state/` belongs to `./cv`, plus the two session directories that the built-in
`prime-agent` adapter has the agent write. [layout](layout.md) lists every file
and what is safe to delete.
In short:

* Safe to delete (history and scratch space): `state/runs/`,
  `state/replaced/`, `state/staging/`, `state/session-artifacts/`.
* Keep for reproducibility: `state/inputs/` (the exact generator JSON of the
  newest accepted run) and `state/sessions/` (what the model was asked).

`--state-dir` or `cv.toml [paths] state_dir` moves the whole directory.
