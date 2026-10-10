#!/usr/bin/env python3
"""Fake prime-agent for tests.

Behaviour is driven by a scenario directory in ``FAKE_AGENT_SCENARIO``:

* ``steps.json``: list of step objects, consumed in call order; the last entry
  repeats.  A step supports:
    - ``answer``: assistant text (default: a strict JSON object)
    - ``exit``: process exit code
    - ``stderr``: text written to stderr
    - ``session_id``: session id announced in the header line
    - ``usage``: usage object put on the assistant message
    - ``raw``: if true, print the answer as plain stdout (no JSON events)
    - ``sleep``: seconds to sleep before answering
* ``barrier``: an integer N; the process waits until N invocations have reached
  this step before answering (used for the simultaneous-failure race test).
* ``calls.jsonl``: every invocation appends its argv plus cwd (test evidence).

The prompt is read from the argument after ``--``.
"""

import json
import os
import sys
import time

scenario_dir = os.environ.get("FAKE_AGENT_SCENARIO")
if not scenario_dir:
    print("fake_agent: FAKE_AGENT_SCENARIO is not set", file=sys.stderr)
    raise SystemExit(64)

os.makedirs(scenario_dir, exist_ok=True)
steps_path = os.path.join(scenario_dir, "steps.json")
counter_path = os.path.join(scenario_dir, "counter.txt")
calls_path = os.path.join(scenario_dir, "calls.jsonl")

argv = sys.argv[1:]
with open(calls_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")

try:
    index = int(open(counter_path).read().strip())
except (OSError, ValueError):
    index = 0
with open(counter_path, "w", encoding="utf-8") as handle:
    handle.write(str(index + 1))

steps = json.loads(open(steps_path, encoding="utf-8").read())
step = steps[min(index, len(steps) - 1)]

if step.get("sleep"):
    time.sleep(float(step["sleep"]))

barrier = step.get("barrier")
if barrier:
    # Atomic append: every waiter publishes itself, then waits for the others.
    barrier_path = os.path.join(scenario_dir, "barrier.txt")
    with open(barrier_path, "a", encoding="utf-8") as handle:
        handle.write("1\n")
    deadline = time.time() + 30.0
    while time.time() < deadline:
        with open(barrier_path, encoding="utf-8") as handle:
            if sum(1 for _ in handle) >= int(barrier):
                break
        time.sleep(0.02)

session_id = step.get("session_id", "fake-session-0001")
answer = step.get("answer")
if answer is None:
    answer = json.dumps({"headline": "Software Developer", "experience": []})

if step.get("raw"):
    print(answer)
else:
    print(json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": os.getcwd()}))
    print(json.dumps({"type": "agent_start"}))
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                    "usage": step.get(
                        "usage",
                        {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 5,
                            "cacheWrite": 1,
                            "totalTokens": 126,
                            "cost": {"total": 0.0012},
                        },
                    ),
                },
            }
        )
    )
    print(json.dumps({"type": "agent_end", "messages": []}))

if step.get("stderr"):
    print(step["stderr"], file=sys.stderr)
raise SystemExit(int(step.get("exit", 0)))
