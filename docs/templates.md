# Templates and layouts

A **template** is a directory with two files: `layout.py` (the drawing code) and
`template.toml` (its pure values). The renderer discovers every template and
registers it as a **layout** under its own `name`, which is also the
`--layout NAME` selector. This page describes the contract, where each value
belongs, how discovery and selection work, and how to write your own template or
ship one as a package. The authoritative sources are the module docstrings of
`tools/cv_generator/src/cv_generator/template.py` and
`tools/cv_generator/src/cv_generator/layouts.py`. The render command itself is
documented in [cvgen-cli.md](cvgen-cli.md).

Two conventions used below: the absolute repository path is shortened to
`<repo>`, and transcripts keep the important lines (the full output is longer).

## The contract

A layout provides three things:

| item | rule |
|---|---|
| `name` | The CLI selector. It must match `[A-Za-z0-9][A-Za-z0-9._-]*` and be unique in the registry. `classic` is taken by the shipped template. |
| `description` | One short line, shown by `cvgen layouts`. |
| `render(cv, options) -> bytes` | Return the PDF bytes for a validated `CVData`. |

There is no base class to inherit. `layout.py` must expose a module-level
`LAYOUT`, which may be an instance, a class or a zero-argument factory that
returns one. Values such as `name` and `description` normally come from
`template.toml`, so the same layout can be renamed by editing one file.

Rules a layout must follow:

- It must not print to stdout. The CLI owns stdout.
- It must return PDF bytes. A layout that returns anything else is reported as a
  contract error.
- When the content does not fit the page, it must raise `LayoutOverflowError`
  (or a subclass). The CLI then reports it and writes **nothing**, so a caller
  never receives a clipped or half-written PDF.

`template.toml` is optional - without it a layout falls back to the values in
its own code - but a *broken* one (invalid TOML, unreadable) is an error, and
the template is skipped with a warning instead of being registered
half-configured.

## The shipped templates

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

| template | what it is |
|---|---|
| `classic` | The reference design: A4, photo sidebar, vertical divider, thin rules, letter-spaced name and headline, lower-right signature. Fixed anchors measured from the source CVs; empty sections are suppressed and later sections reflow up. Bullet limits are `0` (= none): the page geometry decides, and a too dense CV fails as an overflow. |
| `minimal` | A plain single-column A4 CV with no sidebar, no photo and no icons. It is the smallest complete template and is meant to be copied. It is stricter than `classic`: it renders `de_minimal.json` on one page, while `de_full.json` and `en_historical.json` overflow it by 5.0pt. |

## What lives where, and why

`template.toml` holds the **pure values**: a value that changes the look but
never the relationships between parts of the page. `layout.py` holds the
**relational** values: the gaps that make one section reflow below another, plus
the measurements the layout tests assert. Moving a relational gap into the
configuration file would make the reflow meaningless, so it stays in code.

The real sections of `tools/cv_generator/templates/classic/template.toml`:

| section | content |
|---|---|
| `name`, `description` (top level) | the registry name and the line `cvgen layouts` prints |
| `[page]` | `width`, `height` in PDF points (A4 = 595 x 842) |
| `[palette]` | colour names as `"#RRGGBB"` strings |
| `[fonts]` | PDF base-14 font names |
| `[geometry]` | fixed horizontal layout in points: photo size, divider, text columns, rule ends, wrap widths |
| `[anchors]` | fixed vertical anchors in points, measured from the top of the page |
| `[typography]` | font sizes, rule width and tracking (em fractions of the size) |
| `[limits]` | optional early guard rails: `max_bullets_per_entry`, `max_bullets_total` (`0` = no limit) |

An excerpt of the shipped values:

```toml
name = "classic"
description = "Photo sidebar, A4, one page (the shipped reference layout)"

[page]
width = 595.0
height = 842.0

[palette]
rule = "#BFBFBF"      # dividers/rules and the light surname line
dark = "#595959"      # name, section headers, bold labels
body = "#7F7F7F"      # main-column body text, bullets, dates
grey = "#808080"      # headline, contact text, icon glyphs

[fonts]
regular = "Helvetica"
bold = "Helvetica-Bold"
symbol = "Symbol"

[geometry]
photo_size = 214.05          # square photo side, top-left corner
divider_x = 214.4            # vertical sidebar divider
main_left = 248.5            # left edge of the main column

[anchors]
name1_y = 66.69              # first name baseline
name2_y = 106.59             # last name (light grey) baseline
headline_y = 137.32          # headline baseline
signature_date_y = 764.62    # place/date line of the signature

[typography]
body_size = 9.0
name_size = 32.0
rule_width = 0.75

[limits]
max_bullets_per_entry = 0
max_bullets_total = 0
```

## Discovery order

Scan order decides who wins when two templates claim the same name:

1. every directory listed in `cvgen.toml` `template_dirs`, in file order (a
   relative path is resolved against the repository root);
2. the shipped `<project>/templates` directory (always last);
3. the `cv_generator.layouts` entry-point group of installed distributions
   (last of all).

So a template always wins over a distribution that claims the same name, and a
personal template with the same name as a shipped one **replaces** it:

```bash
$ CVGEN_CONFIG=/tmp/cvgen-personal.toml ./cvgen layouts   # template_dirs = ["/tmp/templates"]
warning: skipping template <repo>/tools/cv_generator/templates/minimal: layout name 'minimal' is already registered by template minimal (Personal minimal replacement (Ada example)); pass override=True to replace it
  classic  template classic  Photo sidebar, A4, one page (the shipped reference layout)
  minimal  template minimal  Personal minimal replacement (Ada example)  (active template)
           at /tmp/templates/minimal
```

The first registration wins; the one that lost is skipped with a warning, never
a crash. A directory inside a `template_dirs` entry is only a candidate when it
holds `layout.py`; a hidden directory is ignored.

## Selection rule

| situation | what `cvgen render` uses |
|---|---|
| `--layout NAME` given | that name, always. It is never checked against a local list, so an unknown name fails as the renderer's own error (exit 2). |
| exactly one template discovered | it, automatically |
| several templates and `[template] name` in `cvgen.toml` | the configured name |
| several templates and no configured name | nothing: exit 2, with the list and the exact line to add |

The ambiguity message names the file and the line:

```bash
$ CVGEN_CONFIG=/tmp/cvgen-notemplate.toml ./cvgen render tools/cv_generator/fixtures/en_historical.json --allow-outside-output -o /tmp/x.pdf
error: 2 templates are available, so the active one must be named.
  classic  (template classic)  <repo>/tools/cv_generator/templates/classic
  minimal  (template minimal)  <repo>/tools/cv_generator/templates/minimal
add this to /tmp/cvgen-notemplate.toml and run again (--layout NAME works too):

    [template]
    name = "classic"
$ echo $?
2
```

(`/tmp/cvgen-notemplate.toml` is a throwaway config file with no `[template]`
table; the repository's own `config/cvgen.toml` names `classic`.)

## Write your own template

1. Copy the minimal template into a directory of your own:

   ```bash
   mkdir -p /tmp/my-templates
   cp -r tools/cv_generator/templates/minimal /tmp/my-templates/my-template
   ```

2. Give it a unique name. The name must match `[A-Za-z0-9][A-Za-z0-9._-]*` and
   must not be taken (`classic` and `minimal` are):

   ```toml
   # /tmp/my-templates/my-template/template.toml
   name = "my-template"
   description = "Ada example template (copied from minimal)"
   ```

3. Point a config file at your directory and check that it is discovered:

   ```toml
   # /tmp/cvgen-my-template.toml
   template_dirs = ["/tmp/my-templates"]

   [template]
   name = "my-template"
   ```

   ```bash
   $ CVGEN_CONFIG=/tmp/cvgen-my-template.toml ./cvgen layouts
   available layouts (select one with 'cvgen render <input> --layout <name>')
     classic      template classic      Photo sidebar, A4, one page (the shipped reference layout)
                  at <repo>/tools/cv_generator/templates/classic
     minimal      template minimal      Plain single-column A4 CV, no sidebar and no photo (copy me)
                  at <repo>/tools/cv_generator/templates/minimal
     my-template  template my-template  Ada example template (copied from minimal)  (active template)
                  at /tmp/my-templates/my-template
   active template: my-template
   ```

4. Render with it and change values until the page fits:

   ```bash
   $ CVGEN_CONFIG=/tmp/cvgen-my-template.toml ./cvgen render tools/cv_generator/fixtures/en_historical.json --layout my-template --allow-outside-output -o /tmp/my.pdf
   error: skills overflows the one-page layout: content reaches 749.0pt but the available space ends at 744.0pt (5.0pt over).
   $ echo $?
   3
   ```

   The copy of `minimal` inherits its stricter geometry, so the full fixture
   does not fit. Changing one value in `template.toml` is enough:

   ```toml
   [geometry]
   section_gap = 14.0   # was 20.0
   ```

   ```bash
   $ CVGEN_CONFIG=/tmp/cvgen-my-template.toml ./cvgen render tools/cv_generator/fixtures/en_historical.json --layout my-template --allow-outside-output -o /tmp/my.pdf
   wrote /tmp/my.pdf
   ```

   That is the whole loop: change a value in `template.toml`, render again, keep
   the PDF when it fits one page.

## Ship a layout as a package

Any installed distribution may advertise layouts through the
`cv_generator.layouts` entry-point group:

```toml
# pyproject.toml of the providing package
[project.entry-points."cv_generator.layouts"]
inline = "my_cv_layout:LAYOUT"
```

`my_cv_layout:LAYOUT` must resolve to a layout instance, a class or a
zero-argument factory. Entry points are scanned after the templates, so a
template name always wins over a distribution that claims it. A name already
taken is skipped with a warning, and a broken entry point is skipped too - it
never crashes the CLI:

```bash
$ PYTHONPATH=/tmp/dist ./cvgen layouts
warning: skipping layout entry point 'clash': layout name 'classic' is already registered by template classic (Photo sidebar, A4, one page (the shipped reference layout)); pass override=True to replace it
warning: skipping layout entry point 'broken': module 'mydist' has no attribute 'DOES_NOT_EXIST'
available layouts (select one with 'cvgen render <input> --layout <name>')
  classic  template classic                            Photo sidebar, A4, one page (the shipped reference layout)  (active template)
           at <repo>/tools/cv_generator/templates/classic
  inline   distribution mydist (entry point 'inline')  Fake third-party one-column layout (test double).
  minimal  template minimal                            Plain single-column A4 CV, no sidebar and no photo (copy me)
active template: classic
```

## The overflow contract

Content that does not fit one page raises
`LayoutOverflowError(region, needed, limit, hint=...)` from
`cv_generator.layouts`; the `region` names the part that overflowed
(`experience`, `contacts`, `skills`, ...) and `needed`/`limit` are positions in
points. `cvgen render` prints the message and writes nothing, exit 3:

```bash
$ ./cvgen render /tmp/cv-too-long.json --layout classic --allow-outside-output -o /tmp/too-long.pdf
error: experience overflows the one-page layout: content reaches 1709.1pt but the available space ends at 749.6pt (959.5pt over).
$ echo $?
3
```

A layout that cannot draw photos ignores `LayoutOptions`; a layout that can
should use `LayoutOptions.photo_for(cv)`, which returns the effective
`(path, crop)` and applies the same rule as the shipped template.

A short, complete layout. It draws one line per experience entry, so it is easy
to see the overflow rule:

```python
"""A tiny one-column layout: the name, the headline and one line per job."""

from io import BytesIO

from reportlab.pdfgen import canvas as pdfcanvas

from cv_generator.layouts import LayoutOptions, LayoutOverflowError
from cv_generator.schema import CVData

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN = 60.0
LEADING = 14.0


class TinyLayout:
    """One column, no photo: the shortest useful layout."""

    name = "tiny"
    description = "One column, no photo (minimal example layout)."

    def render(self, cv: CVData, options: LayoutOptions) -> bytes:
        lines = [f"{cv.first_name} {cv.last_name}", cv.headline]
        lines += [f"{entry.role} - {entry.meta}" for entry in cv.experience]

        bottom = PAGE_H - MARGIN  # top-down coordinate where the content must stop
        y = MARGIN  # top-down baseline of the next line
        buf = BytesIO()
        canvas = pdfcanvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
        canvas.setFont("Helvetica", 11)
        for line in lines:
            if y > bottom:
                raise LayoutOverflowError("experience", y, bottom, hint="the page is full")
            canvas.drawString(MARGIN, PAGE_H - y, line)
            y += LEADING
        canvas.showPage()
        canvas.save()
        return buf.getvalue()


LAYOUT = TinyLayout()
```

Drop that file into `<your template dir>/tiny/layout.py`, add the directory to
`template_dirs`, and `cvgen layouts` lists it:

```bash
$ CVGEN_CONFIG=/tmp/cvgen-tiny.toml ./cvgen render /tmp/cv.json --layout tiny --allow-outside-output -o /tmp/tiny.pdf
wrote /tmp/tiny.pdf
```

## Notes for existing code

- `cv_generator.render` is a thin shim. `render_pdf(cv, photo_path=None,
  photo_crop=None)` still renders with the shipped `classic` template; the
  registry-based equivalent is
  `render_layout("classic", cv, LayoutOptions(...))`.
- `cv_generator.ClassicLayout` is **no longer exported**. The class now lives in
  `tools/cv_generator/templates/classic/layout.py`. Select it by name:
  `--layout classic`, or `cv_generator.layouts.get_layout("classic")`.
- The shipped templates are package data: an installed wheel carries them under
  `cv_generator/templates`, and a source checkout uses
  `tools/cv_generator/templates`.
- A relative photo name resolves in `<profile>/photos/` first and then in the
  shipped `fixtures/` directory, so a public clone without a profile still
  renders the fixtures.
