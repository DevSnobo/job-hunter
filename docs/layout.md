# Layout

The repository is one uv workspace. Four tools run from it, one SQLite database
holds the tracker data, and every personal file lives in a private directory
that Git ignores.

This page covers the repository root, two constraints that cannot change, how
the tools find their root, how a value is resolved, and how to move data.

## The repository root

| path | tracked | written by | what it is |
|---|---|---|---|
| `applications/` | private | `./posting` (new folders), `./cv` (CVs), `./job attach` (attachments), `./job delete application` (removal) | One folder per application. `./job`, `./cv` and `./cvgen` read it. |
| `bin/` | tracked | you | The four launcher scripts: `bin/cv`, `bin/cvgen`, `bin/job`, `bin/posting`. |
| `config/` | mixed | you | Configuration. The `*.toml.example` files are tracked, the real files are private. |
| `config/job.toml.example` | tracked | you | Example for the `job` tracker. |
| `config/posting.toml.example` | tracked | you | Example for the posting capture. |
| `config/cv.toml.example` | tracked | you | Example for the CV tailoring tool. |
| `config/cvgen.toml.example` | tracked | you | Example for the CV generator. |
| `config/job.toml`, `config/posting.toml`, `config/cv.toml`, `config/cvgen.toml` | private | you | Your real configuration. Copy them from the examples. |
| `cv` | tracked | you | Symlink `cv -> bin/cv`. |
| `cvgen` | tracked | you | Symlink `cvgen -> bin/cvgen`. |
| `job` | tracked | you | Symlink `job -> bin/job`. |
| `posting` | tracked | you | Symlink `posting -> bin/posting`. |
| `data/` | private | `./job` | The tracker database and its backups. |
| `data/job.sqlite3` | private | `./job db migrate` creates it, every write command changes it | The database. Everything else reads it. |
| `data/backups/` | private | `./job db migrate`, `./job db backup`, `./job db restore`, and every command that changes history | Copies named `job-<stamp>.sqlite3`, written with the SQLite backup API. |
| `docs/` | tracked | you | This documentation. |
| `docs/*.md` | tracked | you | The eight pages: `layout.md`, `architecture.md`, `job-cli.md`, `posting-cli.md`, `cv-cli.md`, `cvgen-cli.md`, `templates.md`, `development.md`. |
| `examples/` | tracked | you | The synthetic profile and one sample application. |
| `exports/` | private | `./job export` | Export output: `snapshot-<stamp>.json` and `sankey-<stamp>/`. |
| `exports/generator/` | private | `./cvgen` (also called by `./cv`) | Rendered PDFs (`current_en.pdf`, `CV_*.pdf`). |
| `profile/` | private | you | Your identity and your CV content. The tools only read it. |
| `profile/inputs/` | private | you | `current_de.json` and `current_en.json`: the reviewed CV inputs. |
| `profile/photos/` | private | you | Photos. The input file names one in its `photo` field. |
| `profile/backlogs/` | private | you | `career-knowledge-backlog-DE.md` and `-EN.md`: bullet candidates and sidebar skills. |
| `sql/` | tracked | you | The schema and the read-only SQL. The single source of truth for the database. |
| `sql/migrations/` | tracked | you | `001_initial_schema.sql`, `002_reporting_views.sql`, `003_intake_folder.sql`, and every later migration. |
| `sql/queries/` | tracked | you | The seven read-only queries that reporting and exports run. |
| `sql/tests/` | tracked | you | `schema_smoke.sql`, a schema and view smoke test. |
| `sql/migration-template.sql.example` | tracked | you | The copy source for a new migration. |
| `state/` | private | `./cv` and the CLI agent it drives | Tool state. See the table below. |
| `tools/` | tracked | you | The four uv workspace projects. See the table below. |
| `pyproject.toml` | tracked | you | The uv workspace (`name = "job-hunter"`, `members = ["tools/*"]`). |
| `uv.lock` | tracked | `uv sync` | The locked dependency set of the whole workspace. |
| `.python-version` | tracked | you | `3.14`. The workspace itself requires `>=3.12`. |
| `.gitignore` | tracked | you | The ignore rules for the private paths. They must cover `applications/`, `profile/`, `data/`, `exports/`, `state/` and the four `config/*.toml` files, while the `config/` rule must not match `*.toml.example`. |
| `README.md` | tracked | you | The entry point of the repository. |
| `LICENSE` | tracked | you | MIT. |
| `.venv/` | private | `uv sync` | The one virtual environment of the workspace. |

`applications/` also holds two kinds of entries that the tracker does not treat
as intake folders:

* `applications/<uuid>/` - the private file folder of a registered application.
  `./job attach` writes here.
* `.posting-entry-<pid>-<hex>/` - a hidden staging folder. `./posting` uses it
  while saving and moves the finished files into the real folder.

`./job intake` lists the other entries and ignores both kinds.

### The four workspace projects

| project | launcher | what it does |
|---|---|---|
| `tools/job_cli` | `./job` | Registers applications, logs events and rounds, reports, exports, migrates the schema. |
| `tools/posting_entry` | `./posting` | Captures a job posting into `applications/<company>[_<posting-id>]/`. |
| `tools/cv_batch` | `./cv` | Tailors one CV per posting with your configured CLI agent and renders it. |
| `tools/cv_generator` | `./cvgen` | Renders a structured CV JSON to a one-page A4 PDF. |

## What lands in `state/`

`state/` belongs to `./cv`. `--state-dir` and `cv.toml [paths] state_dir` move
the whole directory. The tool creates the four subdirectories it owns on the
first run.

| directory | written by | contents |
|---|---|---|
| `state/inputs/` | `./cv` | One file per folder and language: `<folder-slug>-<hash10>-<language>.json`. It holds the evidence of the newest successful run, including the exact generator JSON that produced the CV. |
| `state/runs/` | `./cv` | `<stamp>-<folder-slug>-<hash10>-<language>-<hex6>.json`, one file per job run. Runs never overwrite each other. |
| `state/replaced/` | `./cv` | `<CV name>.<hex8>.bak`: the old bytes of a CV that `--regenerate-invalid` or `--overwrite` replaced. |
| `state/staging/` | `./cv` | The render area of one job: `<slug>-<hash6>-<language>-<hex8>/input.json` plus the PDF that `cvgen` wrote. The tool deletes the folder when the job ends. |

Everything under `state/` is written by this repository. Where the *agent* keeps
its own sessions is up to the `[agent] command` you configure (for example
`prime-agent`'s `--session-dir`); `./cv` does not create a session directory.

Safe to delete: `state/runs/`, `state/replaced/` and `state/staging/`. They are
history and scratch space.

Do not delete `state/inputs/` while you still need a reproducible CV: it is the
only record of which candidate ids produced a PDF. The sessions are also the
only copy of what the model was asked.

## Frozen constraint 1: `sql/migrations/` marks the root

`tools/job_cli/src/job_cli/paths.py` accepts a directory as the job root only
when it carries the marker:

```python
def _looks_like_root(path: Path) -> bool:
    """The single root marker: ``<root>/sql/migrations`` is a directory."""
    return (path / "sql" / "migrations").is_dir()
```

Two consequences:

* Never rename or move `sql/migrations/`. Every tool and the database
  bootstrap depend on it.
* A project that only needs the CV generator is still recognized by
  `tools/cv_generator` when it has a `pyproject.toml` with a
  `[tool.uv.workspace]` table. `cv_generator.paths._looks_like_repo` accepts
  either marker:

```python
def _looks_like_repo(path: Path) -> bool:
    if (path / "sql" / "migrations").is_dir():
        return True
    pyproject = path / "pyproject.toml"
    try:
        return "[tool.uv.workspace]" in pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
```

## Frozen constraint 2: `applications/` is pinned in the database

`sql/migrations/001_initial_schema.sql` constrains every stored artifact path:

```sql
    relative_path TEXT NOT NULL UNIQUE CHECK (
        relative_path LIKE 'applications/%' AND instr(relative_path, '../') = 0
        AND instr(relative_path, char(92)) = 0
        AND substr(relative_path, -3) != '/..'
    ),
```

`sql/migrations/003_intake_folder.sql` constrains the folder link in the same
way: it must be one plain name, with no separator, no leading dot, and not `.`
or `..`.

What this means:

* The database stores the path **relative to the job root**, and it must start
  with `applications/`. A path is never absolute and never machine-specific.
* Renaming the `applications` directory on disk does not change a single stored
  row. Register files keep the old path text, and `./job db doctor` then reports
  them under `missing_files`.
* Setting `applications_dir` in `config/job.toml` moves where the tools read and
  write, but not what the database stores. An attachment copied into the new
  directory is therefore recorded as `applications/<uuid>/<file>` and read back
  from `<job root>/applications/<uuid>/<file>`. Keep the directory under its
  default name unless you also move the whole tree and the job root with it.

## Root resolution

The rule is the same in every tool: an explicit flag wins, then the environment,
then a search. The searched start directory and the marker differ.

| tool | flags and variables | resolution |
|---|---|---|
| `./job` | `--root`, `$JOB_ROOT` | `job_cli.paths.find_job_root`: `--root`, then `$JOB_ROOT`, then a walk up from the package file, then a walk up from the current directory (6 levels each). A named root must carry `sql/migrations`. |
| `./posting` | `--root`, `$JOB_ROOT` | `posting_entry.cv.job_root_for`: `$JOB_ROOT` when it is a directory, else a walk up from the parent of the applications directory (marker: a `bin/cv` file or `sql/migrations`), else that parent. |
| `./cv` | `--root`, `$CV_JOB_ROOT` | `cv_batch.config.find_job_root`: `--root`, then `$CV_JOB_ROOT`, else the current directory. There is no upward walk and no marker check. |
| `./cvgen` | `--profile-dir`, `$CVGEN_PROFILE_DIR` | `cv_generator.paths.find_repo_root`: a walk up from `tools/cv_generator` (6 levels) to the first directory with `sql/migrations` or a `pyproject.toml` that contains `[tool.uv.workspace]`; a standalone copy falls back to the project directory. |

The launchers pin the value. `bin/job` and `bin/posting` always export
`JOB_ROOT` with the repository root they live in, and `bin/cv` always exports
`CV_JOB_ROOT`. A value that you set in the shell therefore only takes effect
when you call the workspace member directly:

```bash
JOB_ROOT=/tmp/other ./.venv/bin/job list          # the environment value is used
JOB_ROOT=/tmp/other ./job list                    # bin/job overwrites it
```

Errors are readable and name the path. Both messages come from
`job_cli.paths.find_job_root` and exit with code 2:

```text
$ ./job --root /tmp/p4-noroot list
error: /tmp/p4-noroot does not look like the job project (no sql/migrations directory)
$ echo $?
2
```

```text
$ JOB_ROOT=/tmp/p4-noroot ./.venv/bin/job list
error: $JOB_ROOT points at /tmp/p4-noroot, which does not look like the job project (no sql/migrations directory)
$ echo $?
2
```

When nothing is found, the message is:

```text
cannot locate the job project root; pass --root or set JOB_ROOT
```

## Configuration precedence

Every value in every tool follows one order:

```text
CLI flag  ->  environment variable  ->  config file  ->  built-in default
```

| tool | config file | environment that moves the file | `--config` flag | variables for single values |
|---|---|---|---|---|
| `./job` | `<job root>/config/job.toml` | `$JOB_CONFIG` | no | `$JOB_DB` (database), `$JOB_ROOT` (root) |
| `./posting` | `<job root>/config/posting.toml` | `$POSTING_CONFIG` | no | `$JOB_ROOT` (root) |
| `./cv` | `<job root>/config/cv.toml` | `$CV_CONFIG` | yes | `$CV_JOB_ROOT` (root), `$CV_AGENT_COMMAND` (the agent command), `$CV_AGENT_FORMAT` (the answer format) |
| `./cvgen` | `<repo>/config/cvgen.toml` | `$CVGEN_CONFIG` | yes | `$CVGEN_PROFILE_DIR`, `$CVGEN_OUTPUT_DIR` |

`job` and `posting` have no `--config` flag on purpose: the environment variable
plus the default path keep the option surface small.

The five keys of `config/job.toml`:

| key | built-in default | what it sets |
|---|---|---|
| `db` | `data/job.sqlite3` | The SQLite database. Flag `--db`, environment `$JOB_DB`. |
| `applications_dir` | `applications` | The folder that holds the application and intake folders. |
| `exports_dir` | `exports` | Where `./job export` writes its snapshots and bundles. |
| `backups_dir` | `data/backups` | Where a backup is written before a migration or a restore. |
| `default_language` | `"de"` | **Reserved.** `config/job.toml.example` says it literally: "It is loaded and exposed on the layout, but no command consumes it yet - no output depends on it." |

Every key is optional. Without the file the tool runs with exactly these values.
Relative paths resolve against the job root.

`$JOB_CONFIG` and `$POSTING_CONFIG` may hold a relative path. That path resolves
against the current directory. A path inside a config file resolves against the
job root instead, with one exception: `cvgen.toml` `input` resolves against the
profile directory.

A missing config file is normal and silent. A broken one is a usage error:

```text
$ JOB_CONFIG=/tmp/p4-bad.toml ./job list
error: config file /tmp/p4-bad.toml is not valid TOML: Invalid value (at line 1, column 8)
$ echo $?
2
```

Unknown keys are ignored, because the four tools share one `config/` directory
and each tool reads only its own keys. A key with the wrong type is an error,
never a silent fallback.

One precedence rule, verified end to end:

```text
$ ./job --no-link db status --json | head -3      # no variable set: the config file wins
{
  "database": "/path/to/job-hunter/data/job.sqlite3",
  "project_root": "/path/to/job-hunter",
```

```text
$ JOB_DB=/tmp/other.sqlite3 ./job --no-link db status --json | head -3
{
  "database": "/tmp/other.sqlite3",
  "project_root": "/path/to/job-hunter",
```

`--db` beats `$JOB_DB`, and `$JOB_DB` beats the config file. An empty database
at a new path is created and migrated on the spot, unless you pass
`--no-migrate`:

```text
$ JOB_DB=/tmp/p4-nope.sqlite3 ./job list
applied 001_initial_schema, 002_reporting_views, 003_intake_folder; initialized /tmp/p4-nope.sqlite3
no applications match
```

One more environment variable exists outside this scheme: `$JOB_WIDTH` sets the
output width of the `job` console.

## What you create after cloning

Only two things must be copied before the tools use your own values. Both copies
are private.

```bash
cp config/job.toml.example     config/job.toml
cp config/posting.toml.example config/posting.toml
cp config/cv.toml.example      config/cv.toml
cp config/cvgen.toml.example   config/cvgen.toml
cp -r examples/profile profile
```

Then replace every value in `profile/` with your own. `examples/profile/README.md`
lists the three subdirectories to work through. Without a config file every tool
still runs, with the built-in defaults. Without a profile, `./cv` and `./cvgen`
stop with a message that names the file they looked for.

## Moving data

Every write location is configurable. Nothing is hard-coded.

The simplest move is the whole tree. Every stored path is relative to the job
root, so a repository that moves as a unit keeps working:

```bash
mv ~/job-hunter /mnt/work/job-hunter
cd /mnt/work/job-hunter && ./job list
```

Moving single directories needs care, because `applications/` is pinned in the
database. See "Moving the database" and "Moving the applications directory"
below.

| what | flag | config key | environment |
|---|---|---|---|
| Database | `--db` | `job.toml` `db` | `$JOB_DB` |
| Applications directory | - | `job.toml` `applications_dir`, `cv.toml` `[paths] applications_dir` | - |
| Exports | - | `job.toml` `exports_dir` | - |
| Database backups | - | `job.toml` `backups_dir` | - |
| Profile | `--profile-dir` (`cvgen`) | `cv.toml` `[paths] profile_dir`, `cvgen.toml` `profile_dir` | `$CVGEN_PROFILE_DIR` |
| Tool state of `./cv` | `--state-dir` | `cv.toml` `[paths] state_dir` | - |
| Rendered PDFs | `-o` (`cvgen render`) | `cvgen.toml` `output_dir` | `$CVGEN_OUTPUT_DIR` |
| Backlogs | `--backlog-de`, `--backlog-en` | `cv.toml` `[paths] backlog_de`, `backlog_en` | - |

### Moving the database

The database is fully relocatable. Give an absolute path in `job.toml`, or use
`$JOB_DB` or `--db` for a single run.

```toml
# config/job.toml
db           = "/mnt/work/job-hunter/job.sqlite3"
backups_dir  = "/mnt/work/job-hunter/backups"
exports_dir  = "/mnt/work/job-hunter/exports"
```

```bash
mkdir -p /mnt/work/job-hunter
mv data/job.sqlite3 /mnt/work/job-hunter/
mv data/backups     /mnt/work/job-hunter/backups
./job db status     # shows the new path
```

A relative value in the file resolves against the job root, so use an absolute
path for a location outside the repository.

Point the CV tool at the same locations when it writes there:

```toml
# config/cv.toml
[paths]
state_dir = "/mnt/work/job-hunter/state"
```

### Moving the applications directory

`applications_dir` is configurable in `job.toml`, `posting.toml` and
`cv.toml` `[paths]`, but the database pins the prefix `applications/` in every
row (see frozen constraint 2). Two consequences, both verified:

* A file copied after the move is registered as `applications/<uuid>/<file>`
  and then read back from `<job root>/applications/<uuid>/<file>`, not from the
  new directory. `./job db doctor` reports the file in both `missing_files`
  (the row) and `untracked_folders` (the file on disk).
* A directory outside the job root makes `./job db doctor` fail with a
  `ValueError` traceback and exit code 1, because it computes the path of every
  found file relative to the job root:

```text
ValueError: '/mnt/work/job-hunter/applications/<folder>/posting.txt' is not in the subpath of '<job root>'
```

So the practical rule is: move the whole tree, or move `data/`, `state/` and
`exports/` and leave `applications/` where it is. `./posting`, `./cv` and
`./job` all read and write the same directory, so a relocation that only some of
them know about is worse than no relocation.
