# job CLI reference

`./job` is the private tracker for submitted applications. It stores every
application, round, and event in one SQLite database and reports from it.
The launcher at the repository root is a symlink to `bin/job`, which runs the
`job-cli` workspace member through `uv`.

Related pages: [posting capture](posting-cli.md), [repository layout](layout.md),
[architecture](architecture.md).

## Synopsis

```bash
./job [OPTIONS] COMMAND [ARGS]...
```

The tool's own description is: "Private tracker for submitted job applications.
Run without arguments for the menu." So `./job` alone opens the interactive
menu when stdin is a terminal.

Without a terminal, the bare command prints the help page and exits with code
2. Use a sub-command with flags in scripts.

## Global options

Global options come **before** the sub-command: `./job --yes db migrate`.

| Option | Metavar | Meaning |
|---|---|---|
| `--db` | `<str>` | Path to the SQLite database (default `data/job.sqlite3`). |
| `--root` | `<str>` | Job project root (default: `JOB_ROOT` or detected). |
| `--json` | | Machine-readable output where supported. |
| `--yes`, `-y` | | Assume yes for confirmations. |
| `--no-migrate` | | Do not apply pending migrations on startup. |
| `--no-link` | | Do not link applications to their `applications/` folders on startup. |
| `--help` | | Show this message and exit. |

Notes:

- Root resolution is `--root` -> `$JOB_ROOT` -> walk up from the current
  directory to the directory that holds `sql/migrations`. A wrong `--root` or
  `$JOB_ROOT` is an error, never a silent fallback.
- The launcher exports `JOB_ROOT` and forwards the exit code. If `uv` is not on
  `PATH`, the launcher prints `job: uv is not installed or not on PATH` and
  exits **127**.
- `--json` exists **where supported**. The sub-commands that support it list it
  in their flag tables below. Human-facing notes and error lines go to stderr,
  so a `--json` run keeps stdout machine-readable.
- Startup work happens for every command except `job db ...`: pending
  migrations are applied (see below) and unambiguous intake folders are linked
  to their application. `--no-migrate` and `--no-link` switch those two steps
  off. A new link is reported on stderr, for example
  `linked e553bc56 Ada Link Test - QA Engineer to applications/Ada Link Test (the only application for Ada Link Test)`.

## Commands at a glance

| Command | Purpose (help text) |
|---|---|
| `add` | Register a submitted application (the only thing that creates a new application). |
| `log` | Log what happened next (the most common action). |
| `list` | Browse and search applications. |
| `show` | Show one application in full: attributes, timeline, rounds, files. |
| `posting` | Read the job posting of an application again. |
| `intake` | List the intake folders under `applications/` and the application each one produced. |
| `upcoming` | Scheduled rounds that still need attention, including past-due ones. |
| `waiting` | Waiting applications; hints about stale ones without closing anything. |
| `stats` | Aggregate statistics for a submission cohort. Contains no personal data. |
| `edit` | Correct application attributes (not history: use `edit-event` for that). |
| `edit-round` | Correct a round: phase, type, format, label, notes, participants. |
| `edit-event` | Correct one recorded event, including its date, after validating the history. |
| `undo` | Review and delete the most recently entered event (recover from a mistake). |
| `attach` | Copy a posting, CV, cover letter, or other file into the application folder. |
| `db` | Database status, migrations, backups, and health checks. |
| `export` | Private snapshots and aggregate exports. |
| `delete` | Delete events, rounds, or applications (with a preview and backup). |

## add

Register a submitted application (the only thing that creates a new application).

```bash
./job add [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--company` | `<str>` | Company name (required). |
| `--role` | `<str>` | Role title (required). |
| `--submitted-on` | `<str>` | Submission date `YYYY-MM-DD`. |
| `--discovery-source` | `<str>` | Lookup code, e.g. `linkedin`, `company_website`, `referral`, `unknown`. |
| `--submission-channel` | `<str>` | Lookup code, e.g. `company_portal`, `linkedin_easy_apply`, `email`. |
| `--recruiter-type` | `<str>` | `none`, `internal`, `external`, `both`, or `unknown`. |
| `--platform` | `<str>` | Platform name (Workday, Greenhouse, ...). |
| `--source-detail` | `<str>` | Free text about where the posting came from. |
| `--url` | `<str>` | Job url. |
| `--location` | `<str>` | Location. |
| `--notes` | `<str>` | Free-text notes. |
| `--from-folder` | `<str>` | Intake folder under `applications/` (`<company>[_<posting-id>]`): the company name comes from the folder identifier, the job url from its `source.txt`. |
| `--force` | | Create even when a similar application exists. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: one row in `applications`. The first timeline row
(`submitted`, label "Application submitted") is derived from `submitted_on` by
the reporting view, so no `events` row is inserted.

Two guards run before the write: a likely duplicate (same company with the same
job url, or, without urls, the same role) needs `--force` or an interactive
confirmation, and in a terminal the tool shows a review table first.

```bash
./job add \
  --company "Ada Lovelace Analytics" \
  --role "Backend Developer" \
  --submitted-on 2026-10-01 \
  --discovery-source linkedin \
  --submission-channel company_portal \
  --url https://example.com/jobs/1
```

```text
saved 5c7a0207  Ada Lovelace Analytics - Backend Developer (submitted 2026-10-01)
```

Start from a folder that [posting capture](posting-cli.md) wrote:

```bash
./job add --from-folder "Ada Demo_77" --role "Platform Engineer" --submitted-on 2026-10-02
```

```text
saved cafaa066  Ada Demo - Platform Engineer (submitted 2026-10-02)

! intake folder applications/Ada Demo_77 -> company 'Ada Demo', posting id 77
! job url from source.txt: https://example.com/jobs/77
! files: posting.txt, source.txt
```

## log

Log what happened next (the most common action).

```bash
./job log [OPTIONS] [ident]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--event` | `<str>` | Event type code, e.g. `completed`. |
| `--date` | `<str>` | When it happened (`YYYY-MM-DD`, `today`/`yesterday`). |
| `--round` | `<str>` | Round id, sequence number, `new`, or `none`. |
| `--round-type` | `<str>` | Round type for a new round. |
| `--phase` | `<str>` | `screening`, `interview`, or `assessment`. |
| `--format` | `<str>` | Interview format code for a new round. |
| `--participants` | `<str>` | Comma-separated participant roles. |
| `--scheduled-for` | `<str>` | Appointment date. |
| `--scheduled-time` | `<str>` | Appointment local time `HH:MM`. |
| `--timezone` | `<str>` | IANA timezone for the appointment time. |
| `--reason` | `<str>` | Optional short reason code. |
| `--notes` | `<str>` | Optional notes. |
| `--before-event` | `<int>` | Insert before this event on the same day. |
| `--after-event` | `<int>` | Insert after this event on the same day. |
| `--strict` | | Treat warnings as errors. |
| `--allow-state-regression` | | Confirm moving an offered, accepted, or started application back to `in_progress`. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: one `events` row, plus `rounds` and `round_participants` rows
when `--round new` creates a round. The command validates the whole history in
the same transaction; a contradiction rolls the write back.

Common event codes: `invited`, `scheduled`, `rescheduled`, `completed`,
`cancelled`, `assessment_received`, `assessment_submitted`, `feedback_received`,
`follow_up_sent`, `note`, `offer_received`, `offer_accepted`, `offer_declined`,
`rejected`, `withdrawn`, `position_closed`, `marked_inactive`, `reopened`,
`job_started`.

```bash
./job log 5c7a0207 --event invited --date 2026-10-03 \
  --round new --round-type recruiter_screen --phase screening --format phone
```

```text
logged Interview/screen invitation on 2026-10-03 (round #1)
Ada Lovelace Analytics - Backend Developer -> in_progress (submitted 2026-10-01)
```

A round-level event needs a round. In a script, pass `--round <id|sequence|new>`:

```bash
./job log 5c7a0207 --event scheduled --date 2026-10-04 --round 1 \
  --scheduled-for 2026-10-10 --scheduled-time 10:00 --timezone Europe/Berlin
```

## list

Browse and search applications.

```bash
./job list [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--status` | `<str>` | Filter by current status. |
| `--search` | `<str>` | Substring of company or role. |
| `--open` | | Only `in_progress` and `offer_pending`. |
| `--limit` | `<int>` | Show at most this many rows. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing. The printed table carries the status legend and the
tracker counters below it.

`--status` is passed to the reporting query as it is; an unknown value is not
an error, the table is just empty:

```bash
./job list --status not_a_status ; echo $?
```

```text
no applications match
0
```

```bash
./job list --open --limit 20
./job list --search "backend" --json
```

## show

Show one application in full: attributes, timeline, rounds, files.

```bash
./job show [OPTIONS] {ident}
```

| Option | Metavar | Meaning |
|---|---|---|
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing.

The `--json` payload has four keys: `application`, `timeline`, `rounds` (each
round with its `participants`), `artifacts`, plus `posting_file`.

```bash
./job show 5c7a0207
./job show "Ada Lovelace Analytics" --json
```

## posting

Read the job posting of an application again.

```bash
./job posting [OPTIONS] [ident]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--json` | | Machine-readable JSON output. |
| `--raw` | | Print only the stored text (no header). |
| `--help` | | Show this message and exit. |

What it writes: nothing. The tool prints the copy that was captured earlier:
`posting.txt` in the intake folder under `applications/` that `./posting`
wrote, or a file attached with `--type job_posting`. Nothing is fetched from
the network.

An application without a stored posting is not an error: the reason goes to
stderr and the exit code stays 0.

```bash
./job posting 5c7a0207 --raw | less
```

## intake

List the intake folders under `applications/` and the application each one produced.

```bash
./job intake [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--pending` | | Only folders with no application registered yet. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing. A folder is named `<company>[_<posting-id>]` by the
posting capture tool. The tracker only reads it: it is never renamed, moved, or
deleted here. Folders that could belong to two applications are listed as
ambiguous hints.

```bash
./job intake --pending
```

```text
                 Intake folders under /tmp/p4-jobdemo/applications
┏━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┓
┃ folder      ┃ company  ┃ posting id ┃ source      ┃ files ┃ application ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━┩
│ Ada Demo_77 │ Ada Demo │ 77         │ example.com │ 2     │ cafaa066    │
└─────────────┴──────────┴────────────┴─────────────┴───────┴─────────────┘
```

## upcoming

Scheduled rounds that still need attention, including past-due ones.

```bash
./job upcoming [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--as-of` | `<str>` | Reference date (default today). |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing.

```bash
./job upcoming --as-of 2026-10-10
```

## waiting

Waiting applications; hints about stale ones without closing anything.

```bash
./job waiting [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--as-of` | `<str>` | Reference date (default today). |
| `--stale-days` | `<int>` | Days without activity before a hint. Default `30`. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing. A stale hint never changes a status.

```bash
./job waiting --stale-days 14
```

## stats

Aggregate statistics for a submission cohort. Contains no personal data.

```bash
./job stats [OPTIONS]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--as-of` | `<str>` | Cutoff date (default today). |
| `--from` | `<str>` | Cohort start (`submitted_on >=`). |
| `--to` | `<str>` | Cohort end (`submitted_on <=`). |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

What it writes: nothing. `--from` after `--to` is a usage error (exit 2).

```bash
./job stats --from 2026-01-01 --to 2026-12-31 --json
```

## edit

Correct application attributes (not history: use `edit-event` for that).

```bash
./job edit [OPTIONS] {ident}
```

| Option | Metavar | Meaning |
|---|---|---|
| `--set` | `<str>` | `field=value` (repeatable). |
| `--help` | | Show this message and exit. |

What it writes: the named columns of one `applications` row. Writable fields:
`company`, `role`, `submitted_on`, `discovery_source`, `submission_channel`,
`recruiter_type`, `platform_name`, `source_detail`, `job_url`, `location`,
`notes`, `intake_folder`. An unknown field is refused:

```bash
./job edit 5c7a0207 --set bogus=1
```

```text
error: cannot update application field(s): bogus
```

Without `--set` the command asks in a terminal. Blank, `none`, `null`, or `-`
clears `intake_folder`; an unknown folder name is refused.

```bash
./job edit 5c7a0207 --set location=Berlin --set notes="first interview booked"
```

## edit-round

Correct a round: phase, type, format, label, notes, participants.

```bash
./job edit-round [OPTIONS] {ident} --round <str>
```

| Option | Metavar | Meaning |
|---|---|---|
| `--round` | `<str>` | Round id or sequence number. Required. |
| `--set` | `<str>` | `field=value` (repeatable). |
| `--participants` | `<str>` | Replace participant roles. |
| `--help` | | Show this message and exit. |

What it writes: the named columns of one `rounds` row, and the
`round_participants` rows when `--participants` is given. Writable round
fields: `phase`, `round_type`, `format`, `label`, `notes`.

```bash
./job edit-round 5c7a0207 --round 1 --set label="Recruiter screen" \
  --participants recruiter,hiring_manager
```

```text
updated round 1
```

Nothing to change is an error: `error: nothing to change: pass --set and/or --participants`.

## edit-event

Correct one recorded event, including its date, after validating the history.

```bash
./job edit-event [OPTIONS] {event_id}
```

| Option | Metavar | Meaning |
|---|---|---|
| `--set` | `<str>` | `field=value` (repeatable). |
| `--allow-state-regression` | | Confirm moving an offered, accepted, or started application back to `in_progress`. |
| `--help` | | Show this message and exit. |

What it writes: the named columns of one `events` row. Writable event fields:
`event_type`, `occurred_on`, `round_id`, `scheduled_for`, `scheduled_time`,
`timezone`, `reason_code`, `notes`. The tool prints the changes first, then
revalidates the whole history; a contradiction is refused.

```bash
./job edit-event 2 --set notes="moved by the recruiter"
```

```text
correcting event 2 (Round scheduled); current application state 'in_progress'
  notes: None -> 'moved by the recruiter'
updated event 2 (Round scheduled)
```

## undo

Review and delete the most recently entered event (recover from a mistake).

```bash
./job undo [OPTIONS] [ident]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--yes`, `-y` | | Confirm the deletion. |
| `--help` | | Show this message and exit. |

What it writes: deletes one `events` row and writes a backup into
`data/backups/` first. When the round was created together with that event and
holds nothing else, the command offers to remove the round too.

```bash
./job undo 5c7a0207 --yes
```

## attach

Copy a posting, CV, cover letter, or other file into the application folder.

```bash
./job attach [OPTIONS] {ident} {file}
```

| Option | Metavar | Meaning |
|---|---|---|
| `--type` | `<str>` | `job_posting`, `cv`, `cover_letter`, `other`. Default `other`. |
| `--name` | `<str>` | Store under this filename. |
| `--label` | `<str>` | Free-text label. |
| `--overwrite` | | Replace an existing file. |
| `--help` | | Show this message and exit. |

What it writes: a copy of `{file}` under `applications/<uuid>/` and one
`artifacts` row. The original file is copied, not moved.

```bash
./job attach 5c7a0207 /tmp/p4-posting/posting.txt --type job_posting --name posting.txt
```

```text
attached applications/5c7a0207-781c-4a5e-a93c-14278826e07a/posting.txt
the original file was copied, not moved
```

## db

Database status, migrations, backups, and health checks.

```bash
./job db COMMAND [ARGS]...
```

| Sub-command | Purpose (help text) |
|---|---|
| `status` | Show database path, version, pending migrations, and row counts. |
| `migrate` | Apply pending numbered migrations (with a backup for an existing database). |
| `backup` | Copy the database into `data/backups/` with the SQLite backup API. |
| `restore` | Replace the database with a backup (taking a safety backup first). |
| `doctor` | Run integrity, foreign key, and artifact checks. |

`job db ...` is the one command group that runs **without** applying pending
migrations first, so `status`, `migrate --dry-run`, and `backup` stay usable.

### db status

| Option | Meaning |
|---|---|
| `--json` | Machine-readable JSON output. |
| `--help` | Show this message and exit. |

Writes nothing. With pending migrations it prints the pending list to stderr.

### db migrate

| Option | Meaning |
|---|---|
| `--dry-run` | Only list what would be applied. |
| `--help` | Show this message and exit. |

Writes: the pending SQL scripts, and a backup in `data/backups/` for an
existing database. There is no `--yes` on this sub-command; use the global
flag before it.

```bash
./job db migrate --dry-run
./job --yes db migrate
```

```text
applied 003_intake_folder
backup: /tmp/p4-jobmig/data/backups/job-20261010T020523.sqlite3
```

### db backup

| Option | Metavar | Meaning |
|---|---|---|
| `--label` | `<str>` | Name prefix of the backup file. Default `job`. |
| `--help` | | Show this message and exit. |

Writes one file `data/backups/<label>-<timestamp>.sqlite3`. An existing file is
never overwritten: a counter is appended.

### db restore

```bash
./job db restore [OPTIONS] {backup_file}
```

| Option | Meaning |
|---|---|
| `--no-backup` | Do not back up the current file. |
| `--yes`, `-y` | Confirm the replacement. |
| `--help` | Show this message and exit. |

Writes: replaces the database with the backup, after writing a
`pre-restore-<timestamp>.sqlite3` safety backup into `data/backups/`.

```bash
./job db restore data/backups/job-20261010T020446.sqlite3 --yes
```

### db doctor

| Option | Meaning |
|---|---|
| `--json` | Machine-readable JSON output. |
| `--help` | Show this message and exit. |

Writes nothing. It runs `integrity_check` and `foreign_key_check`, then lists
artifacts registered but missing on disk, files on disk that are not
registered, and files outside registered applications (intake folders).

```bash
./job db doctor
```

```text
integrity_check and foreign_key_check are clean
Artifacts registered but missing on disk: none
Files on disk that are not registered: none
```

## export

Private snapshots and aggregate exports.

```bash
./job export COMMAND [ARGS]...
```

| Sub-command | Purpose (help text) |
|---|---|
| `snapshot` | Write a private row-level snapshot of the cohort (identified data). |
| `sankey` | Write one timestamped aggregate bundle: edges CSV, metadata, chart data, image. |
| `replay` | Re-run a saved snapshot query and compare with the stored aggregate edges. |

### export snapshot

| Option | Metavar | Meaning |
|---|---|---|
| `--as-of` | `<str>` | Cutoff date. |
| `--from` | `<str>` | Cohort start (`submitted_on >=`). |
| `--to` | `<str>` | Cohort end (`submitted_on <=`). |
| `--output` | `<str>` | File or directory (default `exports/`). |
| `--private` | | Acknowledge that this file is private. |
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

Writes one file with identified data. Without `--private` the command refuses:

```text
error: the snapshot contains company names, roles, urls, and paths; pass --private to confirm you want a private file (use `export sankey` for a shareable aggregate)
```

```bash
./job export snapshot --private --output /tmp/snapshot.csv
```

### export sankey

| Option | Metavar | Meaning |
|---|---|---|
| `--as-of` | `<str>` | Cutoff date. |
| `--from` | `<str>` | Cohort start. |
| `--to` | `<str>` | Cohort end. |
| `--output` | `<str>` | Directory for the whole bundle. |
| `--chart`, `--no-chart` | | Also write the node/link JSON and the rendered chart. Default `--chart`. |
| `--png`, `--no-png` | | Also rasterize the chart to PNG (needs a local converter). Default `--png`. |
| `--help` | | Show this message and exit. |

Writes one timestamped directory: `sankey-edges.csv`, `sankey-metadata.json`,
`sankey-node-link.json`, and `sankey.svg` (+ `sankey.png` when a converter is
installed). The bundle has no identifying fields.

```bash
./job export sankey --output /tmp/sankey --no-png
```

```text
aggregate export written into /tmp/sankey
  sankey-edges.csv  edge list (source, target, value)
  sankey-metadata.json  filters, counting rules, frozen replay data
  sankey-node-link.json  node/link data for a chart tool
  sankey.svg  rendered chart (vector)
cohort 1 applications, 2 edges; no identifying fields
```

### export replay

```bash
./job export replay [OPTIONS] {metadata_file}
```

| Option | Metavar | Meaning |
|---|---|---|
| `--json` | | Machine-readable JSON output. |
| `--help` | | Show this message and exit. |

Writes nothing. Later corrections change an `as-of` result by design, so the
comparison can differ.

```bash
./job export replay /tmp/sankey/sankey-metadata.json
```

```text
reproduced the stored aggregate (1 applications)
```

## delete

Delete events, rounds, or applications (with a preview and backup).

```bash
./job delete COMMAND [ARGS]...
```

| Sub-command | Purpose (help text) |
|---|---|
| `event` | Delete one event (with a preview and a backup). |
| `round` | Delete one round, its events, and its participant links. |
| `application` | Delete one application and its dependent rows. Artifact files stay unless asked. |

Every delete writes a backup into `data/backups/` first, and refuses a deletion
that would leave a contradictory history.

### delete event

```bash
./job delete event [OPTIONS] {event_id}
```

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Confirm the deletion. |
| `--help` | Show this message and exit. |

```bash
./job delete event 1 --yes
```

### delete round

```bash
./job delete round [OPTIONS] {round_id}
```

| Option | Meaning |
|---|---|
| `--yes`, `-y` | Confirm the deletion. |
| `--help` | Show this message and exit. |

```bash
./job delete round 1 --yes
```

### delete application

```bash
./job delete application [OPTIONS] {ident}
```

| Option | Meaning |
|---|---|
| `--remove-files` | Also delete the artifact folder after confirmation. |
| `--yes`, `-y` | Confirm the deletion. |
| `--help` | Show this message and exit. |

Without `--remove-files` the artifact files stay on disk. The intake folder
under `applications/` is always kept. In a non-interactive run, a missing
`--yes` is an error (exit 2), not a hang.

```bash
./job delete application 5c7a0207 --yes --remove-files
```

## Interactive menu

`./job` with no arguments shows a menu when stdin is a terminal:

| Entry | What it runs |
|---|---|
| `1. Prepare application` | run the sibling `./posting` capture, then offer each new folder the `add` flow |
| `2. Add submitted application` | the `add` flow, then the "Next step" sub-menu |
| `3. Log an update` | pick an application, then the `log` flow |
| `4. Browse/search applications` | search box, the applications table, optional open |
| `5. Read a job posting` | pick an application, then print its stored posting |
| `6. Upcoming interviews / waiting applications` | reference date, then `upcoming`, then `waiting` with a stale threshold |
| `7. Statistics and export` | `stats`, Sankey export, private snapshot, or replay |
| `8. Database maintenance` | status, backup, doctor, or migrate |
| `9. Exit` | leave the menu |

The entries carry their own numbers; the cursor starts on the first entry.
The list is an arrow-key list, and the questions inside each flow are typed
answers (dates, text, lookups, yes/no). Ctrl-C or Ctrl-D leaves the current
entry.

Pressing a number confirms that entry at once: `3` picks `3. Log an update`
without the arrow keys or Enter. The number keys cover the first ten entries
(`1`..`9`, then `0` for the tenth); a longer list keeps its remaining entries on
the arrow keys. The same shortcut works in every other list the tool shows
(applications, events, rounds, lookups).

`1. Prepare application` runs the repository's own `./posting` launcher
(`<job root>/bin/posting`) with the inherited terminal, so the capture, the
optional CVs and their questions stay exactly as they are outside the menu.
When it returns, `./job` lists the folders this step created that have no
application yet (newest first) and offers each one through the same add flow as
`job add --from-folder`: the company comes from the folder identifier and the
job URL from its `source.txt`, so preparing a posting and starting to track it
stay in one entry. A missing or unstartable launcher is only a warning. The
posting capture is separate tooling; see [posting capture](posting-cli.md).

The menu is for a person at the keyboard. `--json` and the flags exist so a
script can do the same work without a menu, and a run without a terminal prints
the help page instead of the menu (exit 2).

## Selector rules

Commands that take an `ident` accept "Application id, id prefix, company, or
role". The selector resolves in this order:

| Step | Rule |
|---|---|
| 1 | The ident is the full application id (UUID). |
| 2 | The ident is a prefix of exactly one id. |
| 3 | The ident equals, case-insensitively, one company, one role, or one `company role` text. |
| 4 | The ident is a substring of a company or a role. |

A step that finds several rows is refused. The error lists
up to ten matches and ends with "Use a longer id prefix.":

```bash
./job show S
```

```text
error: 'S' matches several applications:
  841394a1  <company> - <role> (2026-10-09, in_progress)
  ...
Use a longer id prefix.
```

An ident with no match is also exit 2:

```bash
./job show definitely-not-a-real-company ; echo $?
```

```text
error: no application matches 'definitely-not-a-real-company'
2
```

Use the short id that `./job list` and `./job show` print (the first 8
characters of the UUID).

## Migrations

Schema changes live only in SQL. Python applies the files, it never contains
DDL.

- Migration files are `sql/migrations/NNN_name.sql`, numbered from `001` in
  order. Each file owns its `BEGIN IMMEDIATE` and its single final `COMMIT`,
  and it writes its own `schema_migrations` row plus `PRAGMA user_version`.
- The runner checks, before running a script, that the single `COMMIT` is the
  last executable statement, and after it that the committed `user_version` and
  the `schema_migrations` row match the file.
- `job` applies pending migrations on startup, except for `job db ...`.
- A backup is written into `data/backups/` before a migration of an existing
  database, and before a restore.
- In a non-interactive run, a pending migration on an existing database is an
  error, not a silent change:

```text
error: 1 pending migration(s): 003_intake_folder; run `job db migrate` to apply them (a backup is taken first)
```

- In a terminal the tool asks "Apply them now? A backup is taken first.", with
  `yes` as the default answer.
- `--no-migrate` skips the startup step. With it, `job db status` on a database
  with pending migrations still reports the old `user_version` and lists
  `pending_migrations`, and no backup is written. Apply the pending files with
  `job db migrate` before you work with a database whose schema is behind.
- `job db migrate` applies the files explicitly; `job db migrate --dry-run`
  only lists them.
- Never edit or rename an applied migration: the runner compares the stored
  name with the file name and refuses on a difference. Unknown newer versions in
  the database are also refused.

To add a migration:

```bash
cp sql/migration-template.sql.example sql/migrations/004_my_change.sql
```

Then replace the placeholders, keep the transaction and the `schema_migrations`
row, and apply with `./job db migrate`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Also used for "nothing to report", e.g. an empty list or an application without a stored posting. |
| `2` | Usage, configuration, database, or migration error. Also a refused selector, a missing required value, an unknown field, a delete that is not confirmed in a non-interactive run, and a bare `./job` without a terminal. |
| `130` | The flow was cancelled (Ctrl-C, or an interactive confirmation that was declined). |
| `127` | The launcher could not find `uv`. |

Verified examples:

```bash
./job show definitely-not-a-real-company ; echo $?   # -> 2, error on stderr
./job list --status not_a_status ; echo $?            # -> 0, "no applications match"
./job --db /tmp/nope.sqlite3 --no-migrate list        # -> 2, "database not found: ..."
./job --root /tmp/not-a-root list                     # -> 2, "... does not look like the job project"
./job list --limit abc                                # -> 2, "Invalid value for '--limit'"
```

`--json` writes to stdout; error lines and progress notes go to stderr. A
piped `--json` run therefore stays parseable.
