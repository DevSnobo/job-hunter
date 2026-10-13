# Development

This page is for working on the toolchain itself: the uv workspace, the four
test suites, the migration workflow, the shared user-interface contract, and how
to add a fifth tool.

## The uv workspace

The repository root is a uv workspace, not an installable package.

```toml
# pyproject.toml
[project]
name = "job-hunter"
version = "0.1.0"
requires-python = ">=3.12"

# The root is the uv workspace, not an installable distribution.
[tool.uv]
package = false

[tool.uv.workspace]
members = ["tools/*"]

[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "pypdf>=4,<7",
]
```

Facts that follow from it:

* `members = ["tools/*"]` is a glob. A new directory under `tools/` with a
  `pyproject.toml` becomes a workspace member with no other change.
* `package = false` means the root itself installs nothing. It only defines the
  workspace and the shared dev group.
* There is **one** virtual environment, the root `.venv`. No tool has its own.
* `.python-version` pins `3.14`, and uv installs that interpreter when it is
  missing. The declared floor stays `>=3.12`.
* `uv.lock` is tracked, so every clone resolves to the same versions.

Set the workspace up once:

```bash
uv sync --all-packages
```

`uv sync` alone syncs the workspace root. `--all-packages` also installs every
member, which is what puts the four console scripts into `.venv/bin`:

```text
$ ls .venv/bin | grep -E '^(job|cv|cvgen|posting)$'
cv
cvgen
job
posting
```

The dev group of the root carries `pytest` and `pypdf`:

```text
$ ./.venv/bin/python -c "import pytest, pypdf, sys; print(sys.version.split()[0])"
3.14.7
$ ./.venv/bin/python -c "import pytest, pypdf; print(pytest.__version__, pypdf.__version__)"
9.1.1 6.20.0
```

Each member also declares its own tests: `tools/job_cli` and `tools/cv_batch`
have a `dev` group with `pytest`, and `tools/cv_generator` has a `test` extra
with `pytest` and `pypdf`.

## Running the suites

One project per pytest invocation. Each project keeps its own `testpaths`,
`addopts` and markers in its `pyproject.toml`.

```bash
cd /path/to/job-hunter

timeout 500 ./.venv/bin/python -m pytest tools/job_cli/tests       -o faulthandler_timeout=20 -p no:cacheprovider
timeout 500 ./.venv/bin/python -m pytest tools/posting_entry/tests -o faulthandler_timeout=20 -p no:cacheprovider
timeout 500 ./.venv/bin/python -m pytest tools/cv_batch/tests      -o faulthandler_timeout=20 -p no:cacheprovider
timeout 500 ./.venv/bin/python -m pytest tools/cv_generator/tests  -o faulthandler_timeout=20 -p no:cacheprovider
```

| suite | tests | time |
|---|---|---|
| `tools/job_cli/tests` | 323 passed | 1 min 51 s |
| `tools/posting_entry/tests` | 90 passed | 1 min 3 s |
| `tools/cv_batch/tests` | 311 passed | 33 s |
| `tools/cv_generator/tests` | 101 passed, 2 warnings | 10 s |

Why the command looks like that:

| part | why |
|---|---|
| `./.venv/bin/python -m pytest` | Runs the workspace environment directly. It does not take the uv environment lock, so it cannot block behind another uv process. |
| `timeout 500` | Turns a hang into a failure instead of a stalled shell. |
| `-o faulthandler_timeout=20` | Prints the stack of a stuck test after 20 seconds, which names the test that hangs. |
| `-p no:cacheprovider` | Writes no `.pytest_cache` into the tree. |

**Never run two suites in one pytest process.** Each suite patches
module-level state and expects to own the process; a single invocation over
several `tools/*/tests` directories collides. One project per invocation.

To run less, name a file or a test:

```bash
timeout 200 ./.venv/bin/python -m pytest tools/job_cli/tests/test_prompt_cursor.py -o faulthandler_timeout=20
timeout 200 ./.venv/bin/python -m pytest "tools/job_cli/tests/test_interactive.py::test_name" -o faulthandler_timeout=20
```

If a test seems to hang, this form prints the stuck stack and then fails:

```bash
timeout 120 ./.venv/bin/python -m pytest <path>::<test> -x -q -o faulthandler_timeout=15 -p no:cacheprovider
```

Never pipe a suite into `tail` when you need to know where it stopped: the pipe
hides the `faulthandler` dump.

Two things to remember: `tools/cv_batch` marks the tests that run the real
generator (and the real launcher) on temporary paths, and `-m integration`
selects those nine tests. `tools/posting_entry/tests/test_posting_entry.py` is a
`unittest` file that pytest collects without any extra flag.

## Adding a migration

The schema lives in `sql/migrations/`. Adding a table, a column, a view or a
lookup row is a new file there, never a change in Python.

```bash
cp sql/migration-template.sql.example sql/migrations/004_<descriptive_name>.sql
```

The next number is the highest applied version plus one. `./job db status` shows
both. The template states the rules:

```sql
-- COPY to migrations/003_descriptive_name.sql; replace all placeholders.
-- Never edit a migration after it has been applied to a real database.
-- Back up first. Each migration owns its transaction; do not wrap again in tooling.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- Add your DDL/DML here. Simple example:
-- ALTER TABLE applications ADD COLUMN employment_type TEXT;
-- DROP VIEW ...; CREATE VIEW ...; when changing a view.
-- SQLite supports only limited ALTER TABLE operations. Table rebuilds require
-- a reviewed migration, explicit column lists, preserved indexes/triggers/FKs,
-- and PRAGMA foreign_key_check before committing. Do not improvise them here.

INSERT INTO schema_migrations(version, name) VALUES (3, 'descriptive_name');
PRAGMA user_version = 3;
COMMIT;
```

Replace the number in the file name, and the version, the name and the
`PRAGMA` in the body. Then apply it:

```bash
./job db migrate --dry-run     # lists what would be applied, changes nothing
./job db migrate               # applies it, and takes a backup first
./job db status                # shows the new user_version and no pending files
```

`./job db migrate` writes a backup into `data/backups/` before it touches an
existing database. The command is idempotent: with nothing pending it does
nothing. A migration file that changes after it was applied is refused, because
the runner compares the recorded name with the file name.

### Test the flow on a copy, never on the real database

Do not run a new migration against `data/job.sqlite3`. Copy the schema tree and
the database into a scratch directory and use `--root` and `--db` there:

```bash
rm -rf /tmp/mig-test
mkdir -p /tmp/mig-test
cp -r sql /tmp/mig-test/sql
cp data/job.sqlite3 /tmp/mig-test/db.sqlite3

./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db status
./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db migrate --dry-run
./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db migrate
./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db status
```

This is the real output of that flow with a new `004_example_note.sql`
(the table borders of `db status` are removed):

```text
$ ./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db status
1 pending migration(s): 004_example_note
user_version: 3
applied_migrations: {'001': 'initial_schema', '002': 'reporting_views', '003': 'intake_folder'}
pending_migrations: ['004_example_note']

$ ./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db migrate --dry-run
1 pending migration(s): 004_example_note
would apply: 004_example_note

$ ./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db migrate
1 pending migration(s): 004_example_note
applied 004_example_note
backup: /tmp/mig-test/data/backups/job-20261010T020934.sqlite3
```

`sql/tests/schema_smoke.sql` is the other safety net. It inserts a full history
into a disposable database, asserts the views in eight checks, prints the count,
and rolls everything back:

```bash
cp /tmp/mig-test/db.sqlite3 /tmp/smoke.sqlite3
sqlite3 -bail /tmp/smoke.sqlite3 < sql/tests/schema_smoke.sql
# PASS: 8 schema/view assertions
```

## The "no DDL in Python" contract

Schema changes live only in `sql/migrations`. Python applies them and never
builds a `CREATE TABLE`. `tools/job_cli/tests/test_cli_surface.py` walks the
`job_cli` sources and fails on `CREATE TABLE`, `CREATE VIEW`, `CREATE TRIGGER`
and on any personal absolute path.

Before a script runs, `job_cli/migrations.py` checks it. After it runs, the
runner re-reads the committed state. The checks are:

| check | rule |
|---|---|
| File name | `<version>_<name>.sql`, and the numbers must run 1..N with no gap. |
| Transaction | Exactly one `BEGIN IMMEDIATE`, and the single `COMMIT` must be the last executable statement. |
| Ledger | The script must insert its own `schema_migrations` row and set `PRAGMA user_version`. |
| After the run | The committed `user_version` and the ledger row are compared with the file. |
| History | A database with a version the files do not have is refused ("created by newer tooling"); a recorded name that differs from the file name is refused. |

There is no checksum column. The version, the name and `PRAGMA user_version` are
the guards.

Two refusals, as you really see them:

```text
$ ./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db migrate
error: 005_bad has a statement after its COMMIT ('CREATE INDEX oops_index ON oops(id);'); the transaction COMMIT must be the final executable statement. Move that work before the COMMIT or split it into the next migration.
```

```text
$ ./job --root /tmp/mig-test --db /tmp/mig-test/db.sqlite3 db status
error: migration files must be numbered 1..N without gaps; found 1, 2, 3, 4, 6
```

Both exit with code 2. [architecture](architecture.md) describes the tables,
views and triggers that these files create.

## The shared CLI user-interface contract

Four rules hold in all four tools.

| rule | where it is implemented |
|---|---|
| The text you type is orange and bold. | `fg:#FF9D00 bold` - questionary's `answer` class - in `job_cli/ui.py` (`PROMPT_STYLE`), `posting_entry/ui.py` (`ANSWER_STYLE`) and `cv_batch/tty_input.py` (`ANSWER_STYLE`). |
| No reverse video. | `job_cli/ui.py` sets `("selected", "noreverse")` and `("highlighted", "noreverse")`. The pointer alone shows where the cursor is; a reversed line would stay on the pre-selected entry after the cursor moved away from it. |
| A fresh list opens with the cursor on its first entry. | `job_cli/ui.py` `first_default()` rotates the recommended entry to the front, and `choice()` documents that `default` must be `None` or the first entry. |
| Piped or redirected input stays plain. | `job_cli/ui.py` `interactive_available()` (stdin is a TTY); `posting_entry/ui.py` `coloured_input_allowed()` (both streams must be the real `sys` streams and TTYs); `cv_batch/cli.py` `_ask()` falls back to `input()`. `./cv --plain` and `--quiet` force the plain path. |

The exact style string is a cross-tool contract, written out literally in each
of the three modules on purpose. It does not inherit from questionary's own
default, so a questionary change cannot make the tools diverge.

The `prompt` class is set to an empty rule in the two prompt_toolkit tools, so
only the input line is coloured and the question text keeps the terminal font.

The tests that pin the contract:

```bash
./.venv/bin/python -m pytest tools/job_cli/tests/test_prompt_cursor.py
# 5 passed
./.venv/bin/python -m pytest tools/job_cli/tests/test_interactive.py
# part of 27 passed when run together with test_prompt_cursor.py
```

`test_prompt_cursor.py` drives every prompt site with a recording stand-in for
`ui.choice` and asserts that no list pre-points at a later entry. It also caps
unscripted prompts, so a wrong prompt key fails instead of looping.
`tools/posting_entry/tests/test_posting_entry.py` and
`tools/cv_batch/tests/test_tty_input.py` cover the same rules in the other two
tools.

## Adding a fifth tool and a launcher

1. Create the project. Copy the shape of an existing member:

```text
tools/<name>/
    pyproject.toml
    src/<package>/__init__.py
    src/<package>/__main__.py
    src/<package>/cli.py
    tests/
```

```toml
[project]
name = "<dist-name>"
version = "0.1.0"
description = "One line about the tool."
requires-python = ">=3.12"
dependencies = []

[project.scripts]
<name> = "<package>.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/<package>"]
```

`__main__.py` (as in `cv_batch` and `posting_entry`) keeps
`python -m <package>` working, and `tools/*` in `[tool.uv.workspace] members`
picks the new project up with no other change. Run `uv sync --all-packages` once
to install it.

2. Add the launcher. `bin/<name>` is a small POSIX shell script. It resolves its
   own location - also through the root symlink - exports the job root, and
   `exec`s the workspace member. This is the real `bin/job`:

```sh
self=$0
while [ -h "$self" ]; do
    link=$(readlink "$self")
    case $link in
        /*) self=$link ;;
        *) self=$(dirname "$self")/$link ;;
    esac
done
root=$(cd -- "$(dirname -- "$self")/.." && pwd)

if ! command -v uv >/dev/null 2>&1; then
    echo "job: uv is not installed or not on PATH (see https://docs.astral.sh/uv/)" >&2
    exit 127
fi

JOB_ROOT=$root
export JOB_ROOT
exec uv run --quiet --project "$root" --package job-cli job "$@"
```

The important lines:

| line | what it does |
|---|---|
| the `while readlink` loop | Follows the root symlink, so `./job` and `bin/job` both work. |
| `root=$(cd -- "$(dirname -- "$self")/.." && pwd)` | The repository root, from the script location. |
| `exit 127` | A missing `uv` is reported with the exit code of "command not found". |
| `JOB_ROOT=$root; export JOB_ROOT` | The root, for the tool. `bin/cv` exports `CV_JOB_ROOT` instead. |
| `exec uv run --quiet --project "$root" --package job-cli job "$@"` | Runs the member with every argument and its exit code. |

`bin/cv` exports `CV_JOB_ROOT` instead of `JOB_ROOT`. `bin/cvgen` differs in
one more line: it sets the profile default only when it is empty, so a value you
set yourself wins:

```sh
if [ -z "${CVGEN_PROFILE_DIR:-}" ]; then
    CVGEN_PROFILE_DIR=$root/profile
fi
export CVGEN_PROFILE_DIR
exec uv run --quiet --project "$root" --package cv-generator cvgen "$@"
```

3. Add the root symlink:

```bash
ln -s bin/<name> <name>
chmod +x bin/<name>
```

The launchers are tested.
`tools/job_cli/tests/test_cli_surface.py` asserts that the root entry is a
symlink to `bin/job`, that a missing `uv` exits 127, and that arguments and exit
codes are forwarded:

```bash
./.venv/bin/python -m pytest tools/job_cli/tests/test_cli_surface.py
# 11 passed
```

## The examples

`examples/` is tracked and synthetic: `examples/profile/` is a complete profile
for Ada Lovelace (`ada@example.com`), and `examples/sample-application/` is one
captured posting. Both use the same persona as the generator fixtures, so the
repository tells one story and contains no personal data.

`examples/profile/README.md` is the guide: how to copy it, what to replace, and
how to run the chain without copying anything.

Run the renderer against the example profile, with the output kept outside the
tree:

```bash
CVGEN_PROFILE_DIR=examples/profile CVGEN_OUTPUT_DIR=/tmp/cv-out ./cvgen render --language en
# wrote /tmp/cv-out/current_en.pdf
```

Because `$CVGEN_OUTPUT_DIR` is not set in a normal shell, the same command
without it writes `exports/generator/current_en.pdf` instead.

Run the whole CV plan against the example, without a model call, using a
throwaway config file (with no `[agent]` a real run is refused, so the dry run
is the way to exercise the chain):

```toml
# /tmp/cv-example.toml
[paths]
profile_dir = "examples/profile"
applications_dir = "examples"
state_dir = "/tmp/cv-example-state"
```

```bash
./cv --config /tmp/cv-example.toml --dry-run --all --language en
```

```text
Job root       : /path/to/job-hunter
Applications   : /path/to/job-hunter/examples
Profile        : /path/to/job-hunter/examples/profile
Generator      : /path/to/job-hunter/tools/cv_generator
State (private): /tmp/cv-example-state
Language       : EN
Agent          : (none configured)
Parallel jobs  : 1
Layout         : classic (default)   (renderer --layout)
Posting folders: 1 usable, 1 ignored, 0 unusable
Missing CVs    : 1

Missing tailored CVs:
    1. sample-application

dry run: 1 job(s) would run; nothing was written
  sample-application -> CV_AdaLovelace_EN_sample-application.pdf
catalog: 3 entries, 11 candidates from current_en.json
warn: en: no backlog section matched exp/analytical-engines (Software Developer Backend); using the baseline bullets
warn: en: backlog item exp/analytical-engines-2018-07/el4 has no bullet form; the long form is never used
```

The generator input stays in `examples/profile/inputs/current_en.json`; only the
run data moves. Set `state_dir` in the throwaway config, as above, or the `State
(private)` line points at the real `state/` directory. A dry run writes nothing
either way.
