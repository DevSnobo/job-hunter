"""Small helpers shared by the test modules."""
from __future__ import annotations

import csv
import json
from pathlib import Path


def rows(run, *args: str, **kwargs) -> list[dict]:
    result = run(*args, **kwargs)
    assert result.ok, result.output
    return json.loads(result.output)


def payload(run, *args: str, **kwargs) -> dict:
    result = run(*args, **kwargs)
    assert result.ok, result.output
    return json.loads(result.output)


def find_id(run, company: str) -> str:
    for row in rows(run, "list", "--json"):
        if row["company"] == company:
            return row["id"]
    raise AssertionError(f"application {company} not found")


def make_app(run, company: str = "Acme GmbH", role: str = "Backend Engineer",
             submitted_on: str = "2026-01-05", *extra: str) -> str:
    result = run("add", "--company", company, "--role", role,
                 "--submitted-on", submitted_on, *extra)
    assert result.ok, result.output
    return find_id(run, company)


def read_edges(path: Path) -> dict[tuple[str, str], int]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {(row["source"], row["target"]): int(row["value"])
                for row in csv.DictReader(handle)}
