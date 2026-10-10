#!/usr/bin/env python3
"""Fake CV renderer with the real ``cvgen render`` command-line contract.

``FAKE_RENDER_SCENARIO`` points at a directory.  A counter file counts calls:

* ``overflow_times`` (default 0): the first N calls fail with a layout overflow
  (exit 3, "overflow" on stderr) like the real generator.
* ``layouts`` (default ``["classic"]``): the layout names this fake generator
  knows.  ``--layout`` is accepted like the real ``cvgen``; a name outside this
  list fails with the real generator's exit code and stderr shape, so the
  pass-through of an unknown layout can be tested without the real generator.
* ``fail``: exit 3 with a non-overflow render error.
* ``junk``: exit 0 after writing arbitrary non-PDF bytes (F6 regression).
* ``empty``: exit 0 after writing a zero-byte file (F6 regression).
* ``sleep``: seconds to wait before writing the output (slow renderer).
* ``descendant``: a marker path; the renderer starts a background child that
  sleeps ``descendant_sleep`` seconds and then writes the marker.  Used to prove
  that a cancellation kills the whole process group.
* otherwise write a small valid-looking PDF to ``--output``.
"""

import json
import os
import subprocess
import sys
import time

scenario_dir = os.environ.get("FAKE_RENDER_SCENARIO")
if not scenario_dir:
    print("fake_render: FAKE_RENDER_SCENARIO is not set", file=sys.stderr)
    raise SystemExit(64)
os.makedirs(scenario_dir, exist_ok=True)

config_path = os.path.join(scenario_dir, "config.json")
counter_path = os.path.join(scenario_dir, "render_calls.txt")
calls_path = os.path.join(scenario_dir, "render_calls.jsonl")

argv = sys.argv[1:]
with open(calls_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "pid": os.getpid()}) + "\n")

try:
    config = json.loads(open(config_path, encoding="utf-8").read())
except (OSError, ValueError):
    config = {}

try:
    index = int(open(counter_path).read().strip())
except (OSError, ValueError):
    index = 0
with open(counter_path, "w", encoding="utf-8") as handle:
    handle.write(str(index + 1))

if len(argv) < 2 or argv[0] != "render":
    print("fake_render: expected: render <input> --output <pdf> --allow-outside-output", file=sys.stderr)
    raise SystemExit(2)

input_path = argv[1]
output_path = None
if "--output" in argv:
    output_path = argv[argv.index("--output") + 1]
if "--allow-outside-output" not in argv:
    print("error: output path escapes the output directory", file=sys.stderr)
    raise SystemExit(6)
if not os.path.isfile(input_path):
    print(f"error: input file not found: {input_path}", file=sys.stderr)
    raise SystemExit(2)

# The layout is an opaque pass-through for cv_batch; this fake only mimics the
# real generator's own validation, so an unknown name fails like there.
if "--layout" in argv:
    position = argv.index("--layout")
    requested_layout = argv[position + 1] if position + 1 < len(argv) else ""
    known_layouts = list(config.get("layouts", ["classic"]))
    if requested_layout not in known_layouts:
        print(
            f"error: unknown layout: {requested_layout!r}; "
            f"available: {', '.join(known_layouts)}",
            file=sys.stderr,
        )
        raise SystemExit(3)

if config.get("descendant"):
    child_code = (
        "import sys,time;time.sleep(float(sys.argv[2]));"
        "open(sys.argv[1], 'w').write('descendant survived')"
    )
    subprocess.Popen(
        [sys.executable, "-c", child_code, str(config["descendant"]), str(config.get("descendant_sleep", 2.5))]
    )

if config.get("sleep"):
    time.sleep(float(config["sleep"]))

if index < int(config.get("overflow_times", 0)):
    print("error: layout overflow: main column overruns by 40.0 pt", file=sys.stderr)
    raise SystemExit(3)

if config.get("fail"):
    print("error: render error: photo not found", file=sys.stderr)
    raise SystemExit(3)

if os.path.exists(output_path) and not config.get("allow_overwrite"):
    print(f"error: output already exists: {output_path}", file=sys.stderr)
    raise SystemExit(4)

if config.get("junk"):
    with open(output_path, "wb") as handle:
        handle.write(b"this is not a pdf at all\n" * 10)
    print(f"wrote junk {output_path}")
    raise SystemExit(0)

if config.get("empty"):
    open(output_path, "wb").close()
    print(f"wrote empty {output_path}")
    raise SystemExit(0)

with open(output_path, "wb") as handle:
    handle.write(b"%PDF-1.4 fake pdf for tests\n")
print(f"wrote {output_path}")
raise SystemExit(0)
