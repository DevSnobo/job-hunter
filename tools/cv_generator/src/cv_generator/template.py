"""The template contract: reading a template directory's ``template.toml``.

A *template* is a directory that holds

* ``layout.py`` - the drawing code.  It must define ``LAYOUT``: a layout
  instance, or a zero-argument factory/class returning one (see
  :class:`cv_generator.layouts.Layout`), with a unique ``name``, a one-line
  ``description`` and ``render(cv, options) -> bytes``.
* ``template.toml`` - the *pure values* of that layout: page size, palette,
  font names, geometry, anchors, sizes and optional limits.  Relational values
  (the gaps that make one section reflow below another) belong in the code.

This module only reads and validates that file; it has no rendering dependency,
so a template written for another PDF engine can use it too.

A missing ``template.toml`` is not fatal - the layout then falls back to the
built-in values in its own code - but a *broken* one is an error: the loader
raises :class:`TemplateError`, and the discovery scan reports it as a warning
instead of registering a half-configured layout.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

#: File name of a template's value file.
TEMPLATE_FILE = "template.toml"
#: File name of a template's drawing code.
LAYOUT_FILE = "layout.py"


class TemplateError(RuntimeError):
    """Raised when a template directory or its ``template.toml`` is broken."""


def read_template_toml(path: str | Path) -> dict[str, Any]:
    """Parse ``path`` and return the table, or raise :class:`TemplateError`.

    An unreadable file (permissions, a directory) and invalid TOML are both
    reported with the file name and the problem; callers never get a partial
    silent result.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise TemplateError(f"cannot read template file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{path} is not valid TOML: {exc}") from exc


def value(values: dict[str, Any], section: str, key: str, *, where: str) -> Any:
    """Return ``values[section][key]`` or raise a :class:`TemplateError`.

    ``where`` names the file the values came from, so the error says exactly
    which ``template.toml`` is incomplete.
    """
    table = values.get(section)
    if not isinstance(table, dict) or key not in table:
        raise TemplateError(
            f"{where}: missing [{section}] {key}; every pure value of a template "
            f"must be declared in its {TEMPLATE_FILE}"
        )
    return table[key]


def optional(values: dict[str, Any], section: str, key: str, default: Any) -> Any:
    """Like :func:`value`, but returns ``default`` for a missing entry."""
    table = values.get(section)
    if isinstance(table, dict) and key in table:
        return table[key]
    return default


__all__ = [
    "LAYOUT_FILE",
    "TEMPLATE_FILE",
    "TemplateError",
    "optional",
    "read_template_toml",
    "value",
]
