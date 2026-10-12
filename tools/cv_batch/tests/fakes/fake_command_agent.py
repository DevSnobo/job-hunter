#!/usr/bin/env python3
"""Fake generic CLI agent for the command-adapter tests.

The tool runs this with a template such as::

    [sys.executable, "fake_command_agent.py", "--scenario", FILE,
     "--system-prompt", "{system_prompt}", "{prompt}"]

It prints the answer from the scenario file to stdout and appends its argv to
``calls.jsonl`` beside the scenario, so a test can prove what the command
adapter passed.  The prompt is the last argument.

Scenario JSON: ``{"answer": "...", "exit": 0, "stderr": "", "sleep": 0}``.
"""

import json
import os
import sys
import time

argv = sys.argv[1:]


def take(flag, default=None):
    if flag in argv:
        return argv[argv.index(flag) + 1]
    return default


scenario_path = take("--scenario")
if not scenario_path:
    sys.stderr.write("fake_command_agent: --scenario is required\n")
    raise SystemExit(64)

scenario = json.loads(open(scenario_path, encoding="utf-8").read())

record = {
    "argv": argv,
    "cwd": os.getcwd(),
    "prompt": argv[-1] if argv else "",
    "system_prompt": take("--system-prompt"),
}
with open(os.path.join(os.path.dirname(scenario_path), "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

if scenario.get("sleep"):
    time.sleep(float(scenario["sleep"]))

if scenario.get("stderr"):
    sys.stderr.write(scenario["stderr"])
if scenario.get("answer"):
    sys.stdout.write(scenario["answer"])
raise SystemExit(int(scenario.get("exit", 0)))
