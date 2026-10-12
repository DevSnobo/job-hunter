# job-hunter

A private job-application toolchain that runs entirely on this machine. It does
four things: a SQLite tracker for what you sent and what came back, a guided
capture tool for job postings, a tailoring tool that writes one one-page CV per
posting, and a local CV renderer. No job board, no hosted service and no AI API
is contacted by the tools themselves. The optional CV tailoring step is the only
part that calls a model: it drives a CLI agent on your machine, and that agent
has one narrow job - read your experience backlog and pick the points for the
CV. Which CLI agent that is, is your choice: `./cv` runs the command you configure.

This repository is **public** and contains **no personal data**. All personal
material lives in private, gitignored directories (`applications/`, `profile/`,
`data/`, `exports/`, `state/` and `config/*.toml`).

## The four tools

| launcher | what it does | documentation |
|---|---|---|
| `./job` | Tracks applications: register a submission, log events and rounds, browse, report, export, and keep the SQLite schema migrated. | [docs/job-cli.md](docs/job-cli.md) |
| `./posting` | Captures a pasted job posting into `applications/<company>[_<posting-id>]/` as `posting.txt` plus `source.txt`. | [docs/posting-cli.md](docs/posting-cli.md) |
| `./cv` | Tailors one one-page CV per posting: a CLI agent picks the points from your experience backlog, the answer is validated, and the CV is rendered with `./cvgen`. | [docs/cv-cli.md](docs/cv-cli.md) |
| `./cvgen` | Renders a structured CV JSON file to a one-page A4 PDF. No network, no AI. | [docs/cvgen-cli.md](docs/cvgen-cli.md) |

Each launcher is a small shell script in `bin/` that finds the repository root,
exports it (`JOB_ROOT`, `CV_JOB_ROOT`) and runs the matching uv workspace member
from `tools/`. The root symlinks (`./job`, `./posting`, `./cv`, `./cvgen`) are
only convenience: the tools work from any working directory.

## Requirements

| requirement | why |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Runs every tool and manages the single `.venv` of the workspace. |
| Python 3.12 or newer | The workspace requires `>=3.12` and pins 3.14 in `.python-version`; uv installs it when needed. |
| a CLI agent (optional) | Only `./cv` needs one: it reads your experience backlog and selects the points for the CV. Any CLI agent works - set `[agent] command` in `config/cv.toml` (`./cv --explain-modes`); nothing is built in. `./cv --dry-run` works without any. |
| poppler-utils (optional) | Only `./cvgen render --png` needs `pdftoppm`. The PDF is always written. |
| an SVG rasterizer (optional) | `rsvg-convert` (librsvg), `inkscape` or ImageMagick: `./job export sankey` always writes the SVG, and a PNG only when one of them is installed. |

SQLite is part of Python, so there is no database server to install or run.

## Quickstart

```bash
git clone https://github.com/DevSnobo/job-hunter.git
cd job-hunter
uv sync
```

Create your private configuration from the committed examples:

```bash
cp config/job.toml.example     config/job.toml
cp config/posting.toml.example config/posting.toml
cp config/cv.toml.example      config/cv.toml
cp config/cvgen.toml.example   config/cvgen.toml
```

Create your private profile. The repository ships a synthetic one (Ada
Lovelace) that runs the whole chain; replace every value with your own:

```bash
cp -r examples/profile profile
```

Create the database and the schema, then use the tools:

```bash
./job db migrate     # creates data/job.sqlite3 and applies sql/migrations/
./posting            # paste a job posting, save it into applications/
./job list           # browse what you registered
./cv                 # tailor and render a CV per selected posting
```

`config/*.toml` and `profile/` are optional in the sense that every tool has
built-in defaults, but they are where your own values belong. `examples/` shows
the shape of both: `examples/profile/` for the profile and
`examples/sample-application/` for one captured posting.

## Where things live

The answer to "where is the input, the output, the data, the state and the
configuration" is one line per directory:

| path | tracked | what it is |
|---|---|---|
| `applications/<folder>/` | private | One folder per application: `posting.txt`, `source.txt`, the rendered CVs, attachments. Written by `./posting` and `./cv`; read by `./job`, `./cv`. |
| `profile/inputs/current_de.json`, `current_en.json` | private | Input: the reviewed CV inputs per language (identity, contacts, education, certificates, roles, bullets). |
| `profile/photos/` | private | Input: your photo, named by the `photo` field of the input JSON. |
| `profile/backlogs/*.md` | private | Input: extra CV bullet candidates and the sidebar skills. |
| `data/job.sqlite3` | private | The tracker database. Written by `./job`, read by everything that reports. |
| `data/backups/` | private | Backups written before a migration or a restore. |
| `exports/` | private | Output: `./job export snapshots`, the Sankey bundles, and `exports/generator/` for rendered PDFs. |
| `state/` | private | Output/state: the `./cv` run records, saved model inputs, sessions, and backups of replaced CVs in `state/replaced/`. |
| `config/*.toml` | private | Configuration. Nothing works differently without them; they override the built-in defaults. |
| `config/*.toml.example` | tracked | The documented starting point for each config file. |
| `sql/migrations/` | tracked | The schema, the only place where DDL lives, and the marker that makes a directory the job root. |
| `sql/queries/` | tracked | The read-only SQL that reporting and exports run. |
| `sql/tests/` | tracked | A schema smoke test. |
| `bin/` | tracked | The four launcher scripts. |
| `tools/` | tracked | The four uv workspace projects (`job_cli`, `posting_entry`, `cv_batch`, `cv_generator`). |
| `examples/` | tracked | The synthetic profile and one sample posting. |
| `docs/` | tracked | The documentation linked below. |
| `pyproject.toml`, `uv.lock`, `.python-version` | tracked | The uv workspace definition and the locked dependency set. |

Details, including how the root is found and how a value is resolved, are in
[docs/layout.md](docs/layout.md).

## What is private and never committed

These directories hold personal data and are excluded by `.gitignore`:

- `applications/` - companies, postings, CVs, attachments.
- `profile/` - your identity, photo, backlogs.
- `data/` - the SQLite database and its backups.
- `exports/` - snapshots, exports and rendered PDFs.
- `state/` - run records, saved inputs and replaced-CV backups.
- `config/*.toml` - your configuration; only the `*.toml.example` files are
  committed.

Keep it that way: never commit a file from these directories. The `config/`
rule covers the four `.toml` files only, so the examples stay in the repository.

## Privacy of the aggregate export

`./job stats` reports aggregate numbers, and `./job export sankey` writes one
timestamped bundle under `exports/` whose counts are meant to be shareable.
Every node of that chart is a pipeline stage and every value is a count of
applications: the bundle carries no company name, role, link, path, note or
per-application date. The chart note says exactly that, and the metadata file
repeats it in its `privacy` field.

`./job export snapshot` is the opposite: it writes full private rows, so treat
that file as personal data. Everything else under `exports/` is private too.

## Documentation

- [docs/layout.md](docs/layout.md) - every directory, the frozen constraints, root
  resolution and configuration precedence.
- [docs/architecture.md](docs/architecture.md) - how the four tools compose, the
  SQLite schema, the artifact model and the CV pipeline.
- [docs/job-cli.md](docs/job-cli.md) - the full `job` command reference.
- [docs/posting-cli.md](docs/posting-cli.md) - the guided posting capture.
- [docs/cv-cli.md](docs/cv-cli.md) - selection, retention, models and exit codes of `./cv`.
- [docs/cvgen-cli.md](docs/cvgen-cli.md) - the renderer, its flags and the input schema.
- [docs/templates.md](docs/templates.md) - the template system and how to write your own.
- [docs/development.md](docs/development.md) - the uv workspace, the test suites and
  how to add a migration or a fifth tool.
- [examples/profile/README.md](examples/profile/README.md) - how to copy the example
  profile and replace it with your own.

## Licence

MIT.

This is a personal tool, shared as is. It is built around one person's
workflow, so expect sharp edges and no support. Read the code, fork it, or copy
the parts you want.
