# posting capture tool

`./posting` captures a job posting before you apply. It asks four questions,
takes the pasted posting text, and stores it in one folder under
`applications/`. The tool never touches the database and never contacts the
network.

Related pages: [job CLI](job-cli.md), [repository layout](layout.md).

## What it writes

Exactly two files per captured posting:

```text
applications/<company>[_<posting-id>]/posting.txt   the pasted text
applications/<company>[_<posting-id>]/source.txt    the source URL plus one newline
```

The folder is created with an exclusive `mkdir` after a staged write, so an
existing file, symlink, or folder is never replaced. `job add --from-folder`
can later register an application from the folder; the folder itself stays on
disk unchanged.

## Guided flow

The four questions, in order, with the exact prompt text:

| # | Prompt | Notes |
|---|---|---|
| 1 | `Company name: ` | Required. Also names the folder. |
| 2 | `Posting id (optional, blank = none): ` | Blank means no posting id. |
| 3 | `Source URL (LinkedIn, Instaffo, ...; required): ` | Required; must be `http://` or `https://` with a usable host. |
| 4 | the posting text block | Paste the text, then a line with only a dot. |

The posting text block prints these four lines first:

```text
Posting text: paste it now. Multiple lines, blank lines and Unicode are kept.
Finish with a line that contains only a dot '.' and press Enter.
Type :clipboard alone to read the text from the clipboard instead.
(Ctrl-D stops the text too, but then you may lose the paste: prefer the dot.)
```

The text is read line by line and kept as typed, including blank lines. The
paste ends when a line contains only `.` (a dot). Ctrl-D also ends the text, but
then the text is stored without the terminator and the tool warns:
`input ended without the '.' terminator line`. Type `:clipboard` alone as the
first line of the paste to read the text from the clipboard instead.

Before saving, the tool prints a review:

```text
--- review ---
Company    : Ada Demo
Posting id : -
Folder     : /path/to/applications/Ada Demo
Source URL : https://example.com/jobs/1
Text       : 12 lines, 480 characters
  | <first line of the posting>
  | ...
Save this posting? [Y/n]
```

Answer `n` (or Ctrl-C) and nothing is written. Without `--once` the tool then
asks `Another posting? [Y/n]`; "no" or the end of input stops the session, and
entries saved earlier are kept. `./job`'s `Prepare application` entry runs this
tool with `--once` (see [job CLI](job-cli.md)), so one prepared application
never turns into a batch.

## Folder name rules

The folder name is `<company>` or `<company>_<posting-id>`. Both parts are
normalized the same way:

| Input | Result |
|---|---|
| `/` and `\` | replaced with `_` |
| runs of spaces, tabs, and newlines | collapsed to one space |
| Unicode | kept, normalized to NFC |
| a control character | refused: `<what> contains a control character; please retype it on one line` |
| empty text | refused: `<what> must not be empty` |
| only dots (`.`, `..`) | refused: `<what> must not be '.'` (or `'..'`) |
| text that starts with `.` | refused: `<what> must not start with '.'` |
| more than 200 characters | refused: `<what> is longer than 200 characters; please shorten it` |

An empty posting id adds nothing, so the folder is just the company name. The
combined folder name is limited to 200 characters as well.

Example: company `Synthetic R&D / Co` with posting id `REQ 42/7` becomes
`Synthetic R&D _ Co_REQ 42_7`.

## Existing folders are never overwritten

Before the questions about the text, the tool checks the destination. When it
exists, it says so and asks what to do:

```text
Destination already exists: /path/to/applications/Synthetic Demo Company
  it is a directory (posting.txt, source.txt)
Existing folders are never overwritten, renamed or deleted.
[o]ther posting id / [s]kip this posting / [c]ancel:
```

| Answer | Effect |
|---|---|
| `o` | Ask for another posting id and check again. |
| `s` | Skip this posting; prints `Skipped <name>; the existing folder is untouched.` and the exit code stays 0. |
| `c` | Cancel the current entry (exit code 130). |

The first line names the conflict type: `symlink`, `file`, or
`directory (<up to 6 entry names>)`. The same check runs again at the write
step, so two captures can never merge into one folder.

## Options

```bash
./posting [--root ROOT] [--company COMPANY] [--posting-id POSTING_ID]
          [--source SOURCE] [--text-file TEXT_FILE] [--clipboard] [--once]
          [--no-cv] [--version]
```

| Option | Metavar | Meaning |
|---|---|---|
| `--root` | `ROOT` | Applications directory (default: `$JOB_ROOT/applications`). |
| `--company` | `COMPANY` | Answer for the company question. |
| `--posting-id` | `POSTING_ID` | Answer for the posting-id question (`''` skips it). |
| `--source` | `SOURCE` | Answer for the required source-URL question. |
| `--text-file` | `TEXT_FILE` | Read the posting text from this UTF-8 file instead of pasting. |
| `--clipboard` | | Try to read the posting text from the clipboard (wl-paste/xclip/xsel); falls back to pasting. |
| `--once` | | Capture one posting and exit. |
| `--no-cv` | | Never offer to generate the tailored CVs after a saved posting (overrides `config/posting.toml: offer_cv`). |
| `--version` | | Show the program's version number and exit. |
| `-h`, `--help` | | Show the help message and exit. |

The four pre-answering options (`--company`, `--posting-id`, `--source`,
`--text-file`) fill in the questions. They exist for scripting and tests: with
piped stdin the tool cannot ask, so every unanswered question needs its option.
The review question `Save this posting? [Y/n]` is still asked; feed it an answer
(for example `printf 'y\n' | ./posting ...`) or the run stops with exit 130.

There is no `--config` flag. The config file is found through
`$POSTING_CONFIG` or `<job root>/config/posting.toml`.

## Configuration

`config/posting.toml` (the committed example is
`config/posting.toml.example`) has three top-level keys:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `applications_dir` | string | `<job root>/applications` | Directory that receives the captured postings. A relative path resolves against the job root. |
| `offer_cv` | boolean | `true` | Offer the tailored DE and EN CVs after a saved posting, in a real interactive session only. |
| `default_source_from_clipboard` | boolean | `false` | Read the posting text from the clipboard first when neither `--text-file` nor `--clipboard` was given. |

Two environment variables matter:

| Variable | Meaning |
|---|---|
| `$JOB_ROOT` | The job root. `--root` wins over it, and both win over `applications_dir`. The `./posting` launcher exports it. |
| `$POSTING_CONFIG` | Path of the config file. A relative path resolves against the current directory. |

Rules: precedence is flag -> environment variable -> config file -> built-in
default. A missing config file is normal and silent. An unreadable or invalid
file stops the run before anything is asked, with exit code 2:

```bash
POSTING_CONFIG=/tmp/bad.toml ./posting
```

```text
error: config file /tmp/bad.toml: applications_dir must be a string, got int (42)
```

Unknown keys are ignored, because all four tools share `config/`.

## Post-save CV offer

After a successful save, and only in a real interactive session (stdin and
stdout are both terminals), the tool asks once:

```text
Generate the tailored CVs now (DE and EN)? [Y/n]
```

It then runs the sibling `./cv` tool twice, once per language:

```bash
<root>/bin/cv --folder <folder> --language de --all --yes
<root>/bin/cv --folder <folder> --language en --all --yes
```

Notes:

- `--no-cv` and `offer_cv = false` both switch the question off.
- `--no-cv` always wins over the config key.
- Only `<job root>/bin/cv` is accepted as the launcher. The key is looked up
  below the job root, never on `PATH`.
- When that launcher is missing or not executable, the tool warns and prints a
  retry command for each language:

```text
! the CV tool <root>/bin/cv is missing or not executable; the posting itself is saved
! generate the DE CV later with: <root>/bin/cv --folder <folder> --language de --all --yes
```

- A CV problem never changes the outcome of the capture: the posting is already
  saved, so every CV problem is a warning.

## Non-TTY behaviour

With piped stdin the tool still runs, but it refuses instead of hanging. An
unanswered question ends the run with exit code 130:

```bash
mkdir -p /tmp/p4-posting/apps
printf '' | ./posting --root /tmp/p4-posting/apps ; echo $?
```

```text
Job posting capture (pre-application intake).
Applications directory: /tmp/p4-posting/apps
Existing folders are never overwritten. Ctrl-C or Ctrl-D cancels the current entry.

Company name:
130
```

```text
! cancelled (end of input); the current entry was not saved (entries saved earlier in this session are kept)
```

Answer the questions with the options to make the run work.

## Worked example

A real save into a scratch directory. The posting text comes from a file, and
the review question is answered from a pipe:

```bash
mkdir -p /tmp/p4-posting/apps
printf 'Synthetic Demo Company\n' > /tmp/p4-posting/posting.txt

printf 'y\n' | ./posting --root /tmp/p4-posting/apps \
  --company "Synthetic Demo Company" \
  --posting-id "" \
  --source https://example.com/jobs/1 \
  --text-file /tmp/p4-posting/posting.txt \
  --once
```

```text
Job posting capture (pre-application intake).
Applications directory: /tmp/p4-posting/apps
Existing folders are never overwritten. Ctrl-C or Ctrl-D cancels the current entry.

--- review ---
Company    : Synthetic Demo Company
Posting id : -
Folder     : /tmp/p4-posting/apps/Synthetic Demo Company
Source URL : https://example.com/jobs/1
Text       : 1 lines, 23 characters
  | Synthetic Demo Company
Save this posting? [Y/n] Saved /tmp/p4-posting/apps/Synthetic Demo Company
```

The result:

```text
$ find /tmp/p4-posting/apps -type f | sort
/tmp/p4-posting/apps/Synthetic Demo Company/posting.txt
/tmp/p4-posting/apps/Synthetic Demo Company/source.txt
```

```text
$ cat "/tmp/p4-posting/apps/Synthetic Demo Company/source.txt"
https://example.com/jobs/1
```

Run the same command a second time. The folder exists, so nothing is
overwritten and the tool asks what to do:

```text
Destination already exists: /tmp/p4-posting/apps/Synthetic Demo Company
  it is a directory (posting.txt, source.txt)
Existing folders are never overwritten, renamed or deleted.
[o]ther posting id / [s]kip this posting / [c]ancel:
```

Answer `s` and the run ends with exit code 0 and the message
`Skipped Synthetic Demo Company; the existing folder is untouched.`. Answer
nothing (empty stdin) and the run ends with exit code 130. In both cases the
folder keeps its two files.

Then register the application in the tracker:

```bash
./job add --from-folder "Synthetic Demo Company" --role "Backend Developer" --submitted-on 2026-10-01
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Saved, or skipped with `s` at an existing folder. |
| `1` | The input cannot be used (invalid company name, posting id, source URL, or text file), or the destination appeared between the check and the write. |
| `2` | The config file is unreadable or invalid. |
| `130` | The current entry was cancelled (Ctrl-C, end of input, or `c` at an existing folder). Entries saved earlier in the session are kept. |

Messages that start with `!` go to stderr:

```text
! source URL must start with http:// or https:// (got 'not-a-url')
! company name must not start with '.'
! clipboard not read (wl-paste: not installed; xclip: not installed; xsel: not installed); paste the text manually instead
```
