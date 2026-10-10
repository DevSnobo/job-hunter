"""Parsing of the interactive selection syntax (``1,3-5,12``)."""

from __future__ import annotations

import re

_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_INT = re.compile(r"^\d+$")


class SelectionError(ValueError):
    """A selection string the user must correct."""


def parse_selection(spec: str | None, count: int) -> list[int]:
    """Return the selected 1-based indices in display order, deduplicated.

    * ``None`` or a blank string returns ``[]`` (cancel; never "all").
    * ``all`` / ``*`` selects every candidate.
    * ``1,3-5,12`` selects 1, 3, 4, 5, 12.
    * Bad tokens, out-of-bounds values and reversed ranges raise
      :class:`SelectionError`; nothing is selected silently.
    """
    if count < 0:
        raise SelectionError("no candidates to select from")
    if spec is None:
        return []
    text = spec.strip()
    if text == "":
        return []
    if text.lower() in {"all", "*", "alle"}:
        return list(range(1, count + 1))

    picked: set[int] = set()
    for raw in text.split(","):
        token = raw.strip()
        if token == "":
            raise SelectionError(f"empty entry in {text!r} (use e.g. 1,3-5,12)")
        match = _RANGE.match(token)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                raise SelectionError(f"reversed range {token!r} (write {high}-{low} instead)")
            for value in range(low, high + 1):
                _check_bounds(value, count, token)
                picked.add(value)
            continue
        if _INT.match(token):
            value = int(token)
            _check_bounds(value, count, token)
            picked.add(value)
            continue
        raise SelectionError(f"cannot read {token!r} as a number or range")
    if not picked:
        raise SelectionError("nothing selected")
    return sorted(picked)


def _check_bounds(value: int, count: int, token: str) -> None:
    if value < 1 or value > count:
        if count == 0:
            raise SelectionError(f"{token!r} is out of range: there is no candidate to select")
        raise SelectionError(
            f"{token!r} is out of range: choose between 1 and {count}"
        )


__all__ = ["SelectionError", "parse_selection"]
