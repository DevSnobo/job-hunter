# Example profile (synthetic)

This directory is a complete but fictional profile. It runs the whole CV chain
without a single real datum: the person is Ada Lovelace, `ada@example.com`, and
every company, date and bullet is invented. It uses the same persona as the
generator fixtures in `tools/cv_generator/fixtures/`, so the repository tells
one consistent story.

## Use it

1. Copy the directory to the profile location of the repository root:

   ```bash
   cp -r examples/profile profile
   ```

2. Replace every value with your own:

   - `inputs/current_de.json` and `inputs/current_en.json` - the reviewed CV
     inputs: identity, contacts, photo name, languages, skills, education,
     certificates, role titles, date lines and bullets.
   - `photos/` - your photo. Delete `placeholder_photo.jpg` and name your file
     in the `photo` field of both input files.
   - `backlogs/career-knowledge-backlog-DE.md` and `-EN.md` - the material the
     CV tool may select from. Only a marked **Resume bullet** (English) or
     **Lebenslaufpunkt** (German) - or a plain `- ` bullet line - can become a
     CV bullet. The full format and every fallback are documented in
     [docs/backlog.md](../../docs/backlog.md); to write your own file, copy the
     `career-knowledge-backlog-*.md.example` skeleton next to the filled-in
     examples in `backlogs/`.

3. Keep `profile/` private. It is gitignored and must never be committed.

## What is in here

| path | purpose |
|---|---|
| `inputs/current_de.json`, `inputs/current_en.json` | the reviewed CV inputs, one per language |
| `photos/placeholder_photo.jpg` | a neutral placeholder photo, 480x600 |
| `backlogs/career-knowledge-backlog-DE.md`, `-EN.md` | extra bullet candidates and the sidebar skills |
| `README.md` | this file |

`inputs/`, `photos/` and `backlogs/` are the three directories the tools look
for. See [docs/layout.md](../../docs/layout.md) for the full picture.

## Try it without copying anything

Render the English example input with the shipped `classic` template:

```bash
./cvgen render --language en --profile-dir examples/profile
```

This writes `exports/generator/current_en.pdf`. The photo is resolved in
`examples/profile/photos/` first, so the example proves the profile path works.

The tailoring tool reads its profile location from `config/cv.toml`, so point a
throwaway config file at the example to dry-run it:

```toml
# /tmp/cv-example.toml
[paths]
profile_dir = "examples/profile"
applications_dir = "examples"
```

```bash
./cv --config /tmp/cv-example.toml --dry-run --all --language en
```

The plan then lists one job:
`sample-application -> CV_AdaLovelace_EN_sample-application.pdf`.

## A note on the backlog examples

The English and German backlogs show the real format, including the hard rule
that the long form is background only. The dated section for the
`07.2018 - 02.2022` entry replaces the baseline bullets with four approved
items, and item 4 deliberately has no bullet form. A date line that says
`present` (German: `heute`) matches no dated section, so that entry keeps the
bullets from the input JSON. Run `./cv --dry-run` to see both, and expect them
as two `warn:` lines: for this example profile they are intentional, not a
mistake in the example.
