# Tailored CVs with `./cv`

`./cv` writes one tailored, one-page CV per selected job posting. It reads
`posting.txt` of an application folder, builds a catalog of approved CV text
from your own profile, lets a CLI agent pick from that catalog,
validates the answer in Python and renders the PDF with
[`./cvgen`](cvgen-cli.md). The agent only selects candidate IDs; the Python
pipeline owns every file written.

## Prerequisites

| requirement | detail |
|---|---|
| `uv` | The launcher `bin/cv` runs `uv run --quiet --project <root> --package cv-batch cv ...`. Without `uv` on `PATH` it prints an error and exits 127. |
| workspace dependencies | Run `uv sync` once at the repository root. |
| a CLI agent | Only a real run needs one: it reads the candidate catalog and returns the selected IDs (`--dry-run` never calls it). The shipped default adapter drives `prime-agent` (override the executable with `--model-command EXE` or `$CV_PRIME_AGENT`). Any other CLI agent can be used instead - see [Custom CLI agent](#custom-cli-agent). |
| a profile | `profile/inputs/current_de.json` and `current_en.json` (the reviewed baseline) plus the backlogs `profile/backlogs/career-knowledge-backlog-DE.md` and `-EN.md`. The baseline supplies `first_name`, `last_name` and `headline`; the first two build the CV file name. Copy the synthetic example: `cp -r examples/profile profile`. |
| `applications/` | One folder per posting. A folder is usable when it is a real directory with a non-empty regular `posting.txt`. |
| config (optional) | `config/cv.toml`, found through `--config`, then `$CV_CONFIG`, then `<job root>/config/cv.toml`. |

The job root is `--root`, else `$CV_JOB_ROOT` (the `./cv` launcher exports it),
else the current directory. Every relative default follows it: `applications/`,
`profile/`, `state/` and `tools/cv_generator`.

## The pipeline

One run does this, in order: scan `applications/` and classify the expected CV
file of every folder, build the candidate catalog from the profile baseline and
the backlogs, write one prompt per job, run the CLI agent in headless mode,
extract and validate the selection, rebuild the CV JSON from the baseline plus
the selection, render it with `cvgen render` on a private staging path, and
finally place the accepted PDF next to the posting under the target name. Every
job fails on its own: one bad posting never stops the rest of the batch. See
[architecture.md](architecture.md) for how `cv` sits between the other tools.

| step | who does it | result |
|---|---|---|
| scan and classify | `cv_batch` | the numbered candidate list and the plan |
| catalog | `cv_batch` from `profile/` | candidate IDs the model may select |
| generate | the CLI agent (default adapter: `prime-agent -p --mode json`) | one JSON answer per job |
| validate | `cv_batch` | a selection with known IDs, or a bounded retry |
| render | `cvgen render` in the generator's uv project | a one-page A4 PDF on a staging path |
| publish | `cv_batch` | the PDF in the application folder and the run evidence under `state/` |

## Model policy

`--model` chooses the policy for the built-in `prime-agent` adapter. The default
comes from `cv.toml` `model`, else `deepseek`. For another CLI agent, see
[Custom CLI agent](#custom-cli-agent).

| value | behaviour |
|---|---|
| `deepseek` | DeepSeek Flash only (`deepseek/deepseek-v4-flash`), never Pro. No fallback: a recognised quota, rate-limit, availability or authentication failure fails the job. |
| `auto` | Astra (`openai-codex/gpt-6-astra`) first. Switch to DeepSeek Flash only on a recognised quota, rate-limit, availability or authentication failure, then keep Flash for the rest of the batch. Malformed JSON and validation problems never switch the model. |
| `astra` | Astra only. No fallback. |

`./cv --explain-modes` prints the full documentation. The model part is:

```text
Model policy:

  deepseek (default)   DeepSeek Flash only, never Pro
                       (deepseek/deepseek-v4-flash).  No fallback: a recognised
                       quota/rate-limit, availability or authentication failure
                       fails the job.
  auto                 Try Astra (openai-codex/gpt-6-astra) first.  Switch to
                       DeepSeek Flash (deepseek/deepseek-v4-flash) only when
                       the call fails with a recognised quota/rate-limit,
                       availability or authentication problem, then keep using
                       Flash for the rest of the batch.  Malformed JSON and
                       validation problems never trigger a switch; they are
                       retried at most --content-retries times with the
                       validator message.
  astra                Astra only.  No fallback.

  There is no separate quota probe and no paid retry loop.  Token counters are
  reported only when the CLI exposes them in its JSON event stream.
```

Related flags: `--thinking LEVEL` (prime-agent reasoning level) and
`--timeout SECONDS` (model timeout, default `[limits] model_timeout`, else 240).

## Custom CLI agent

`prime-agent` is the default, not a requirement. `./cv` needs only a CLI agent
that reads a system prompt plus the user message and prints its answer - the
selection JSON - on stdout. Point the tool at another agent with any of:

| source | value |
|---|---|
| `cv.toml` | `[agent]` table, key `command` = a list of strings, e.g. `["your-agent", "--print", "{prompt}"]` |
| environment | `$CV_AGENT_COMMAND`, the same command as one shell-style string |
| flag | `--agent-command 'your-agent --print {prompt}'` |

A flag beats the environment, which beats the configuration file; without any of
them the built-in `prime-agent` adapter runs.

The command is a template. `{prompt}` is replaced with the user message and is
required. `{system_prompt}` is replaced with the system prompt when the template
names it; otherwise the system prompt is prepended to the user message, so an
agent without a system-prompt option still receives the instructions. Both
replacements are literal and per argument: no shell runs and the prompt is never
split. Use your agent's headless/print mode and disable its tools the way that
agent documents it.

The command must print the answer JSON on stdout. A non-zero exit fails the job,
and the same recognised `quota`/rate-limit, `availability` and `authentication`
failures are read from its stderr as for `prime-agent`.

The model policy and the session modes above belong to the built-in `prime-agent`
adapter. A custom command runs without them: no `--model`/`--thinking` selection,
no `state/sessions/`, and no token accounting. `--jobs N` still runs N command
processes in parallel.

## Selection

The scan numbers the candidates it is allowed to offer. Only a missing target
file is numbered by default; an existing file enters the list only after an
explicit opt-in.

| flag | effect |
|---|---|
| `--language de\|en` | CV language. The codes it accepts come from `cv.toml` `languages`. Without the flag the tool asks; a blank answer means the first language (DE). |
| `--folder NAME` | Keep only `applications/<NAME>`. Repeatable. `NAME` must be the bare basename of an existing usable folder: an unknown, unusable or path-like name is refused, never widened to every folder. |
| `--select LIST` | Non-interactive selection, for example `1,3-5,12`. `all` or `*` selects everything. |
| `--all` | Select every numbered candidate. |
| `--dry-run` | Scan, select and print the plan. Nothing is written. |

The numbering is the ordered list `1..n` printed under "Missing tailored CVs:",
"Replaceable invalid targets" and "Existing CVs". `--select` uses those numbers.
A bad token is rejected: `abc` is "cannot read 'abc' as a number or range", `99`
is "'99' is out of range: choose between 1 and N", and `5-3` is a reversed
range.

`--dry-run` writes nothing, but it is not independent of the profile: the target
file name of every folder comes from the profile identity, so the tool reads
`profile/inputs/current_<language>.json` before it can print the plan. A minimal
synthetic dry run (throwaway config, unrelated plan lines omitted) looks like
this:

```bash
$ ./cv --dry-run --all --language de --config /tmp/cv-example.toml
```

```text
Profile        : /tmp/cv-example/profile
Language       : DE
Posting folders: 1 usable, 0 ignored, 0 unusable
Missing CVs    : 1

Missing tailored CVs:
    1. Example GmbH

dry run: 1 job(s) would run; nothing was written
  Example GmbH -> CV_AdaLovelace_DE_Example GmbH.pdf
catalog: 2 entries, 5 candidates from current_de.json
```

## Target file name

The expected CV of one folder is built from `target_template` in `cv.toml`
(default `CV_{name}_{language}_{folder}.pdf`):

| field | value |
|---|---|
| `{name}` | `first_name` + `last_name` from the profile baseline, with all whitespace removed (`Ada` + `Lovelace` -> `AdaLovelace`) |
| `{language}` | the upper-case language (`DE`, `EN`) |
| `{folder}` | the exact application folder name, spaces and Unicode kept |

One example: the identity Ada Lovelace and the folder
`applications/Example GmbH` produce
`applications/Example GmbH/CV_AdaLovelace_DE_Example GmbH.pdf`.

## Retention

Nothing is overwritten by chance.

| situation | default | with `--overwrite` | with `--regenerate-invalid` |
|---|---|---|---|
| no file yet | created | created | created |
| a valid PDF exists | job skipped, file untouched | replaced, old file backed up | untouched |
| an invalid file exists (empty, bad header) | reported, untouched | replaced, old file backed up | replaced, old file backed up |
| a symlink or a directory sits at the target | never selectable | never selectable | never selectable |

Details:

- By default an existing valid PDF is skipped and never overwritten, also not by
  a parallel job: the file is placed with a no-clobber link.
- `--overwrite` adds existing valid PDFs to the numbered selection, so they can
  be replaced deliberately. `--regenerate-invalid` adds only the safe invalid
  targets (regular, non-symlink files). Both are explicit opt-ins.
- Every replacement first copies the old file into
  `state/replaced/<target name>.<8 hex chars>.bak`.
- The replacement is atomic, and the target is re-checked immediately before it:
  it must still be the exact file that was inspected and still a PDF. A target
  that changed in between is refused, not overwritten
  (`the target X changed after it was inspected; refusing to replace it`).
- When no candidate is left, the run says so instead of claiming success:
  `nothing to do: every usable posting folder already has a valid DE CV. Pass
  --overwrite to replace an existing CV.` Exit code 0.

## What a run writes

| path | content |
|---|---|
| `applications/<folder>/<target name>.pdf` | the accepted one-page CV |
| `state/inputs/<folder>-<hash>-<lang>.json` | the canonical run record for that folder and language |
| `state/runs/<timestamp>-<folder>-<hash>-<lang>-<suffix>.json` | the same record, one file per run |
| `state/replaced/*.bak` | copies of replaced files |
| `state/sessions/` | agent sessions (built-in `prime-agent` adapter only, mode `fresh` or `reuse`) |
| `state/staging/` | scratch directory, removed with the job |

The record holds `generator_input` (the exact JSON that produced the accepted
PDF, post-trim), the `layout`, the selection metadata, the model and session
details, the used trims and the publish information.

Fitting one page is a loop, not a single try. When the renderer reports an
overflow, the tool drops the last bullet of the longest entry (the oldest job
loses a bullet on a tie; when every entry is down to one bullet, the oldest
entry is dropped) and renders again. It stops at `--trim-attempts` trim steps
(default from `[limits] trim_attempts`, else 8). `--no-trim` disables trimming,
so an overflow fails the job immediately. A rejected model answer is retried at
most `--content-retries` times with the validator message (default from
`[limits] content_retries`, else 1).

`--jobs N` runs N jobs as parallel fresh sessions. It saves wall time; token use
stays about the same. The upper bound is `[limits] max_jobs` in `cv.toml`
(default 4) - an upper bound, not a default: without `--jobs` the tool runs one
job at a time even when `max_jobs` is 4. `--session-mode reuse` cannot be
combined with `--jobs > 1`, because a live session is never shared.

The three session modes come from `./cv --explain-modes`:

```text
  fresh (default)  One new tool-owned session per job, run sequentially.
                   Predictable and isolated; no cross-job history.
  reuse            ONE tool-owned session is created for the first job and
                   resumed (--resume <id> --session-dir <tool dir>) for every
                   later job in the same batch, always sequentially.  Reuse is
                   NOT a token promise: the transcript grows with every job and
                   the provider decides how much of it is cached.  A live
                   session is never shared between parallel jobs.
  none             --no-session: nothing is saved anywhere.
```

## Options

Selection:

| option | meaning |
|---|---|
| `--language de\|en` | CV language; blank prompt entry means DE |
| `--folder NAME` | restrict the run to `applications/<NAME>` (repeatable) |
| `--select LIST` | non-interactive selection, e.g. `1,3-5,12` |
| `--all` | select every numbered candidate |
| `--overwrite` | allow replacing existing CV files (old file backed up first) |
| `--yes`, `-y` | skip the confirmation prompt |
| `--dry-run` | scan, select and print the plan; write nothing |

Execution:

| option | meaning |
|---|---|
| `--jobs N` | parallel fresh sessions (1..`[limits] max_jobs`) |
| `--session-mode {fresh,reuse,none}` | session policy (default `fresh`; built-in `prime-agent` adapter) |
| `--model {auto,astra,deepseek}` | model policy (default from `cv.toml` `model`, else `deepseek`) |
| `--thinking LEVEL` | prime-agent reasoning level (built-in adapter) |
| `--model-command EXE` | prime-agent executable (default `prime-agent`, or `$CV_PRIME_AGENT`; built-in adapter) |
| `--agent-command CMD` | drive another CLI agent instead of prime-agent (default: `$CV_AGENT_COMMAND`, else `cv.toml [agent] command`) |
| `--timeout SECONDS` | model timeout (default `[limits] model_timeout`, else 240) |
| `--content-retries N` | paid model retries after a rejected answer, 0..3 (default `[limits] content_retries`, else 1) |
| `--no-trim` | never drop bullets to fit one page; an overflow then fails |
| `--trim-attempts N` | trim steps, 1..32 (default `[limits] trim_attempts`, else 8) |
| `--regenerate-invalid` | allow replacing safe invalid target files |
| `--quiet` | no plan output |
| `--plain` | no ANSI colours and plain input (default when stdout is not a TTY) |

Paths (defaults follow the job root):

| option | meaning |
|---|---|
| `--root DIR` | job root (default `$CV_JOB_ROOT`, else the current directory) |
| `--applications-dir DIR` | where the posting folders live |
| `--backlog-de FILE`, `--backlog-en FILE` | the two career backlogs |
| `--generator-project DIR` | uv project of the renderer (default `tools/cv_generator`) |
| `--generator-exe EXE` | renderer executable override, same CLI contract |
| `--layout NAME` | forwarded verbatim to `cvgen render --layout` (default `classic`) |
| `--state-dir DIR` | tool-owned private state (default `state`) |
| `--config FILE` | configuration file (default `$CV_CONFIG`, else `<job root>/config/cv.toml`) |

Limits:

| option | meaning (default from `[limits]`) |
|---|---|
| `--max-bullets-per-entry N` | bullets per job entry (1..50, `bullets_per_entry` = 5) |
| `--max-bullets-total N` | bullets in the whole CV (1..50, `bullets_total` = 13) |
| `--max-headline N` | headline length (1..200, `headline_chars` = 60) |
| `--max-posting-chars N` | characters of the posting sent to the model (1..200000, `posting_chars` = 6000) |
| `--render-timeout SECONDS` | renderer timeout (0 < t <= 3600, `render_timeout` = 150) |

Compatibility: `./cv --help` lists exactly these options, `./cv --version`
prints `cv 0.1.0`, and `--explain-modes` prints the documentation and exits 0.

## Input bounds

These are rejected before anything runs:

| input | rule |
|---|---|
| `--jobs` | 1..4 (the upper bound comes from `[limits] max_jobs`) |
| `--content-retries` | 0..3 |
| `--trim-attempts` | 1..32 |
| `--timeout`, `--render-timeout` | finite, positive, at most 3600 seconds |
| `--max-bullets-per-entry`, `--max-bullets-total` | 1..50 |
| `--max-headline` | 1..200 |
| `--max-posting-chars` | 1..200000 |

For example:

```bash
$ ./cv --jobs 9 --dry-run --all --language de
error: --jobs must be between 1 and 4
$ echo $?
2
```

## Exit codes

| code | meaning |
|---|---|
| 0 | success, or nothing to do (`--dry-run` too) |
| 1 | at least one selected job failed (the others still ran) |
| 2 | usage error: bad flag, bad `--folder`, missing profile input, bad config, bad input bound |
| 130 | cancelled (Ctrl-C); remaining jobs were stopped and cancelled |


## Troubleshooting

| symptom | cause and fix |
|---|---|
| `fail: <folder> [de] model call failed: agent executable not found: /nonexistent/prime-agent` (also printed as `error: <folder>: model call failed: ...`), exit 1 | The agent executable is not on `PATH`. Install `prime-agent`, or pass `--model-command` / set `$CV_PRIME_AGENT` (or point `--agent-command` at your own agent). Nothing was written to the application folder. |
| `error: baseline input not found: <profile>/inputs/current_de.json`, exit 2 | The profile is incomplete. Copy `examples/profile` to `profile` or fix `[paths] profile_dir` in `cv.toml`. |
| `error: --folder NAME: no such folder under <applications>`, exit 2 | The name is not the basename of a usable folder. Unusable folders report their own reason (`--folder NAME: no posting.txt`). |
| `nothing to do: every usable posting folder already has a valid DE CV.`, exit 0 | Nothing is missing. Use `--overwrite` to replace a CV deliberately. |
| `fail: <folder> [de] layout overflow and no safe trim left after N attempt(s); no PDF was written for <folder>`, exit 1 | The CV does not fit one page and no bullet could be dropped. Shorten the profile material, raise the limits, or set `--trim-attempts` higher. `--no-trim` makes the first overflow fatal. |
| `error: --session-mode reuse cannot be combined with --jobs > 1 (a live session is never shared)`, exit 2 | Run the batch with `--jobs 1`, or use `--session-mode fresh`. |
| `fail: <folder> [de] PlaceError: the target X changed after it was inspected; refusing to replace it`, exit 1 | Another process changed the CV between the scan and the replacement. Inspect the file and run again. |
| `error: no selection given and stdin is not a terminal; use --select or --all`, exit 2 | A scripted run needs `--select` or `--all`. Add `--yes` for a real run. |
| `error: refusing to start without --yes on a non-interactive stdin`, exit 2 | Same cause: pass `--yes` when stdin is not a terminal. |
| Everything named "already valid" although a file looks broken | A target that is a symlink or a directory is never selectable. The plan lists it under "Never replaceable". |
