# The CV renderer: `./cvgen`

`./cvgen` is a local renderer. It reads a structured CV JSON file and writes a
one-page A4 PDF. The shipped `classic` template draws it with ReportLab. There
is no network call and no AI in this tool: it renders exactly the JSON it is
given. The tailoring tool [`./cv`](cv-cli.md) uses it as its last step.

In the transcripts below the absolute repository path is shortened to `<repo>`,
and unrelated lines are omitted.

## Prerequisites

| requirement | detail |
|---|---|
| `uv` | The launcher `bin/cvgen` runs `uv run --quiet --project <root> --package cv-generator cvgen ...`. Without `uv` on `PATH` it prints an error and exits 127. |
| workspace dependencies | Run `uv sync` once at the repository root. The renderer needs no extra step. |
| poppler-utils (optional) | Only `--png` needs `pdftoppm`. When it is missing, the PDF is still written and the PNG is skipped with a warning. |

The launcher also sets `CVGEN_PROFILE_DIR=<repo>/profile` unless you already
exported it.

## Commands

| command | what it does |
|---|---|
| `cvgen render [input]` | render one CV JSON file to PDF |
| `cvgen layouts` | list every discovered template and layout |

```text
usage: cvgen [-h] {render,layouts} ...
```

## `cvgen render`

```text
usage: cvgen render [-h] [-o OUTPUT] [--overwrite] [--png] [--layout NAME]
                    [--language de|en] [--allow-outside-output]
                    [--config FILE] [--profile-dir DIR]
                    [input]
```

| option | meaning |
|---|---|
| `input` | Path to the CV JSON file. Optional when `input` is set in `cvgen.toml` or `--language` is given. |
| `-h`, `--help` | Show the `render` help and exit. |
| `-o`, `--output OUTPUT` | Output PDF name or path. Default: `<output dir>/<input stem>.pdf`. |
| `--overwrite` | Replace an existing output file. |
| `--png` | Also write a PNG preview (needs poppler-utils). |
| `--layout NAME` | Template to render with. It wins over every other source. Without it: the only discovered template, or `cvgen.toml [template] name` when several exist. |
| `--language de\|en` | Shortcut for `<profile>/inputs/current_<language>.json`. |
| `--allow-outside-output` | Allow `--output` to point outside the output directory. Without it the path is confined. |
| `--config FILE` | Configuration file. Default: `$CVGEN_CONFIG`, else `<repo>/config/cvgen.toml`. |
| `--profile-dir DIR` | Private profile directory. Default: `$CVGEN_PROFILE_DIR`, else `cvgen.toml`, else `<repo>/profile`. |

Which input is rendered, in order: the `input` argument, else `--language`, else
`cvgen.toml` `input`. When none of the three names a file, the tool exits 2 and
lists all three ways.

```bash
./cvgen render tools/cv_generator/fixtures/en_historical.json --layout classic -o /tmp/p4-example.pdf --allow-outside-output
```

## Output confinement

The output directory resolves through `$CVGEN_OUTPUT_DIR`, then `cvgen.toml`
`output_dir`, then the default `<repo>/exports/generator`. A relative `--output`
name is placed inside that directory. Without `--allow-outside-output`, an
absolute path, a `..` step or any other escape is refused:

```bash
$ ./cvgen render tools/cv_generator/fixtures/en_historical.json --output /etc/x.pdf
error: output path escapes the output directory: /etc/x.pdf is not inside <repo>/exports/generator. Generated files must stay inside the output directory; pass --allow-outside-output to write elsewhere.
$ echo $?
6
```

A relative name stays inside the output directory:

```bash
$ CVGEN_OUTPUT_DIR=/tmp/cv-out ./cvgen render tools/cv_generator/fixtures/en_historical.json -o preview.pdf
wrote /tmp/cv-out/preview.pdf
```

An absolute path works only with `--allow-outside-output`:

```bash
$ ./cvgen render tools/cv_generator/fixtures/en_historical.json --layout classic --allow-outside-output -o /tmp/p4-example.pdf
wrote /tmp/p4-example.pdf
```

Output files are written atomically (temporary file plus rename), and an
existing file is replaced only with `--overwrite`:

```bash
$ ./cvgen render ... -o /tmp/p4-example.pdf --allow-outside-output
error: output already exists: /tmp/p4-example.pdf (use --overwrite to replace it)
$ echo $?
4
```

`--png` writes a preview beside the PDF. It needs `pdftoppm` from
poppler-utils; without it the PDF is still written and the PNG is skipped with a
warning:

```bash
$ CVGEN_OUTPUT_DIR=/tmp/cv-out ./cvgen render tools/cv_generator/fixtures/en_historical.json --png
wrote /tmp/cv-out/en_historical.pdf
wrote /tmp/cv-out/en_historical.png
```

## The input schema

The schema separates content from layout: the renderer draws exactly what the
JSON says. It comes from `tools/cv_generator/src/cv_generator/schema.py`
(pydantic models with `extra="forbid"`, so a misspelled field is an error, not
silently ignored).

| field | type | required | rules |
|---|---|---|---|
| `schema_version` | integer | no (default `1`) | Must be exactly `1`. No other version is accepted. |
| `language` | string | yes | `en` or `de` only. It also picks the default section headings. |
| `first_name` | string | yes | Must not be empty. |
| `last_name` | string | yes | Must not be empty. |
| `headline` | string | yes | Must not be empty. |
| `photo` | string | no | A file name or path. See "Photo resolution". |
| `photo_crop` | object | no | Square crop box in source pixels: `left` >= 0, `top` >= 0, `size` > 0 (integers). |
| `contacts` | list of objects | no (default `[]`) | Each: `kind` = `birthday`, `email`, `phone` or `address`; `lines` = 1 or 2 strings. |
| `languages` | list of objects | no | Each: `name`, `level` (strings). |
| `skills` | list of objects | no | Each: `label` plus `segments`, a list of `{text, bold}` with `bold` defaulting to `false`. |
| `experience` | list of objects | no | Each: `role`, `meta`, `bullets` (list of strings, default `[]`). |
| `education` | list of objects | no | Each: `title`, optional `dates`. |
| `certificates` | list of objects | no | Each: `title`. |
| `signature` | object | no | `place_date` and `name`, both strings. |
| `section_labels` | object | no (default `{}`) | Overrides a section heading. Keys: `experience`, `languages`, `skills`, `education`, `certificates`. |

Cross-field rules: at least one of `experience`, `education` and `certificates`
must be present; `section_labels` may use only the five keys above. Unknown keys
anywhere are rejected.

The default headings per language:

| section | `en` | `de` |
|---|---|---|
| `experience` | `EXPERIENCE` | `EXPERIENCE` |
| `languages` | `LANGUAGE SKILLS` | `SPRACHEN` |
| `skills` | `TECHNICAL SKILLS` | `TECHNISCHE SKILLS` |
| `education` | `EDUCATION` | `BILDUNG` |
| `certificates` | `CERTIFICATES` | `ZERTIFIKATE` |

A complete synthetic example (Ada Lovelace). It renders on one page with both
shipped templates:

```json
{
  "schema_version": 1,
  "language": "en",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "headline": "Software Developer",
  "photo": "placeholder_photo.jpg",
  "contacts": [
    {"kind": "email", "lines": ["ada@example.com"]},
    {"kind": "phone", "lines": ["+44 7700 900123"]}
  ],
  "languages": [
    {"name": "English", "level": "Native"},
    {"name": "German", "level": "Proficient (C1)"}
  ],
  "skills": [
    {"label": "Languages & Frameworks:", "segments": [
      {"text": "Java", "bold": true}, {"text": ", Kotlin", "bold": false}]},
    {"label": "Build & Tools:", "segments": [
      {"text": "Git, Maven", "bold": true}, {"text": ", Docker, Linux", "bold": false}]}
  ],
  "experience": [
    {"role": "Software Developer Backend",
     "meta": "Analytical Engines, Berlin | 09.2022 - present",
     "bullets": [
       "Built a billing service in Java.",
       "Wrote automated tests for the billing service."]}
  ],
  "education": [
    {"title": "B.Sc. Computer Science, University of Potsdam", "dates": "2016 - 2020"}
  ],
  "certificates": [
    {"title": "Oracle Certified Associate, Java SE 8"}
  ],
  "signature": {"place_date": "Potsdam, 10.10.2026", "name": "Ada Lovelace"}
}
```

```bash
$ ./cvgen render /tmp/cv.json --layout classic --allow-outside-output -o /tmp/cv.pdf
wrote /tmp/cv.pdf
```

Errors name the offending field:

```bash
$ ./cvgen render /tmp/cv-unknown-field.json -o /tmp/cv.pdf --allow-outside-output
error: 1 validation error for CVData
nope
  Extra inputs are not permitted [type=extra_forbidden, input_value=1, input_type=int]
$ echo $?
2
```

## Fixtures

`tools/cv_generator/fixtures/` holds three complete example inputs that use the
same synthetic Ada Lovelace persona, plus the placeholder photo they name.

| file | content |
|---|---|
| `de_full.json` | the German reference CV with every section |
| `de_minimal.json` | a shorter German CV |
| `en_historical.json` | the English reference CV |
| `placeholder_photo.jpg` | the photo the fixtures name |

```bash
$ ./cvgen render tools/cv_generator/fixtures/en_historical.json --layout classic -o /tmp/p4-example.pdf --allow-outside-output
wrote /tmp/p4-example.pdf
$ head -c 5 /tmp/p4-example.pdf
%PDF-
```

How the fixtures behave with the shipped templates:

| fixture | `--layout classic` | `--layout minimal` |
|---|---|---|
| `de_full.json` | one page | exits 3: `skills overflows the one-page layout` (5.0pt over) |
| `de_minimal.json` | one page | one page |
| `en_historical.json` | one page | exits 3: `skills overflows the one-page layout` (5.0pt over) |

`minimal` is a starting point for your own template, not a second production
layout; see [templates.md](templates.md).

## Photo resolution

`photo` may be an absolute path or a relative name. A relative name is looked up
in `<profile>/photos/` first, and then in the shipped `fixtures/` directory.
The profile wins when both hold the same name. A layout that draws no photo
ignores the field entirely.

```bash
$ ./cvgen render tools/cv_generator/fixtures/en_historical.json --profile-dir /tmp/empty-profile --layout classic -o /tmp/x.pdf --allow-outside-output
wrote /tmp/x.pdf
```

An empty profile directory works because the shipped `fixtures/placeholder_photo.jpg`
is used as a fallback. A profile file of that name is used instead, and a
missing or unreadable photo is an error (exit 3):

```bash
$ ./cvgen render /tmp/cv-nophoto.json -o /tmp/x.pdf --allow-outside-output
error: profile photo not found: <repo>/profile/photos/no-such-photo.jpg
```

## Configuration and environment

| value | `--flag` | environment | `cvgen.toml` | default |
|---|---|---|---|---|
| config file | `--config` | `$CVGEN_CONFIG` | - | `<repo>/config/cvgen.toml` |
| profile | `--profile-dir` | `$CVGEN_PROFILE_DIR` | `profile_dir` | `<repo>/profile` |
| output directory | `-o name` (relative) | `$CVGEN_OUTPUT_DIR` | `output_dir` | `<repo>/exports/generator` |
| default input | `input` argument | - | `input` (relative to the profile) | none |
| active template | `--layout` | - | `[template] name` | the only template |
| extra template dirs | - | - | `template_dirs` | none |

A missing `cvgen.toml` is normal and silent. A file that exists but is broken
(invalid TOML, wrong type) is a usage error that names the file, exit 2.

```bash
$ CVGEN_PROFILE_DIR=examples/profile CVGEN_OUTPUT_DIR=/tmp/cv-out ./cvgen render --language en
wrote /tmp/cv-out/current_en.pdf
```

## `cvgen layouts`

```text
usage: cvgen layouts [-h] [--json] [--config FILE] [--profile-dir DIR]
```

| option | meaning |
|---|---|
| `-h`, `--help` | Show the `layouts` help and exit. |
| `--json` | Print the list as JSON. |
| `--config FILE` | Configuration file, same default as `render`. |
| `--profile-dir DIR` | Private profile directory, same default as `render`. |

```bash
$ ./cvgen layouts
available layouts (select one with 'cvgen render <input> --layout <name>')
  classic  template classic  Photo sidebar, A4, one page (the shipped reference layout)  (active template)
           at <repo>/tools/cv_generator/templates/classic
  minimal  template minimal  Plain single-column A4 CV, no sidebar and no photo (copy me)
           at <repo>/tools/cv_generator/templates/minimal
active template: classic
hint: add a template directory to 'template_dirs' in <repo>/config/cvgen.toml, or install a package that provides the 'cv_generator.layouts' entry point group.
```

One line per layout: name, origin, description, and for a template the
directory it was loaded from. `(active template)` marks the template `render`
would use now. `--json` prints one object per layout with the keys `name`,
`description`, `origin`, `entry_point`, `path`, `template` and `active`.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 2 | usage: unknown layout, missing or invalid input, ambiguous template, bad config, missing profile directory |
| 3 | render error: one-page overflow, photo problem, a layout that breaks the contract |
| 4 | the output file exists and `--overwrite` was not given |
| 5 | the output file cannot be written |
| 6 | the output path escapes the output directory |

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `error: unknown layout 'x'; known layouts: classic, minimal`, exit 2 | The name is not a discovered template. Run `cvgen layouts`. |
| `error: N templates are available, so the active one must be named.`, exit 2 | Several templates exist: pass `--layout NAME` or add `[template] name = "..."` to `cvgen.toml`. The message prints the exact line. |
| `error: input file not found: ...`, exit 2 | Wrong path. Check the path, or use `--language de\|en`. |
| `error: ...: invalid JSON: ...`, exit 2 | The file is not valid JSON. |
| `error: skills overflows the one-page layout: content reaches Xpt but the available space ends at Ypt`, exit 3 | The content does not fit the page. Nothing was written. Shorten the content, or use a layout with more room. |
| `error: profile photo not found: ...`, exit 3 | `photo` names a file that is in neither `<profile>/photos/` nor the shipped `fixtures/`. |
| `error: output already exists: ... (use --overwrite to replace it)`, exit 4 | Pass `--overwrite`, or choose another name. |
| `error: cannot write ...: Permission denied`, exit 5 | The directory is not writable. Choose another output directory. |
| `error: output path escapes the output directory`, exit 6 | Use a relative `-o` name, or add `--allow-outside-output`. |
| `warning: PNG preview skipped: PNG preview requires poppler-utils (pdftoppm)`, exit 0 | Install poppler-utils, or ignore it: the PDF is written. |
