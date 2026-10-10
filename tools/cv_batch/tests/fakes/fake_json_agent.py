#!/usr/bin/env python3
"""Fake prime-agent that emits an exact JSON event fixture (provider tests).

Unlike ``fake_agent.py`` this fake does not synthesise a lifecycle: the test
hands it the literal event objects, so the assertions run against the installed
0.9.8 stream shape.

Scenario file (path in ``FAKE_JSON_AGENT_SCENARIO``)::

    {
      "events": [ {...}, ... ],   # printed as one JSON object per line
      "stdout_text": "raw",       # alternative: printed verbatim instead
      "stderr": "...",
      "exit": 0
    }

Every invocation appends its argv to ``calls.jsonl`` beside the scenario file.
"""

import json
import os
import sys

scenario_path = os.environ.get("FAKE_JSON_AGENT_SCENARIO")
if not scenario_path:
    print("fake_json_agent: FAKE_JSON_AGENT_SCENARIO is not set", file=sys.stderr)
    raise SystemExit(64)

scenario = json.loads(open(scenario_path, encoding="utf-8").read())

with open(os.path.join(os.path.dirname(scenario_path), "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")

if scenario.get("stdout_text") is not None:
    sys.stdout.write(scenario["stdout_text"])
else:
    for event in scenario.get("events", []):
        sys.stdout.write(json.dumps(event) + "\n")

if scenario.get("stderr"):
    sys.stderr.write(scenario["stderr"])

raise SystemExit(int(scenario.get("exit", 0)))
