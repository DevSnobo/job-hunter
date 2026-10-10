#!/usr/bin/env python3
"""Fake prime-agent whose behaviour depends on the selected model.

Used by the provider-fallback tests: every Astra call fails after a delay, every
DeepSeek Flash call succeeds.  Call slots are allocated atomically so that the
first Astra failure always happens before the second, which makes the parallel
switch race deterministic instead of timing-dependent.

Environment:

* ``FAKE_PROVIDER_DIR``        directory for ``calls.jsonl`` (required)
* ``FAKE_PROVIDER_STDERR``     failure text of an Astra call
* ``FAKE_PROVIDER_ANSWER``     assistant text of a successful call
* ``FAKE_PROVIDER_BASE_SLEEP`` delay of the first failing call (default 0.2)
* ``FAKE_PROVIDER_SLEEP_STEP`` extra delay per earlier call (default 0.4)
"""

import json
import os
import sys
import time

directory = os.environ.get("FAKE_PROVIDER_DIR")
if not directory:
    print("fake_provider_agent: FAKE_PROVIDER_DIR is not set", file=sys.stderr)
    raise SystemExit(64)

os.makedirs(directory, exist_ok=True)


def allocate_slot() -> int:
    """Atomic first-come-first-served slot number (no read-modify-write race)."""
    index = 0
    while True:
        path = os.path.join(directory, f"slot-{index}")
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            index += 1
            continue
        os.close(handle)
        return index


argv = sys.argv[1:]
slot = allocate_slot()
with open(os.path.join(directory, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "slot": slot, "cwd": os.getcwd()}) + "\n")

model = ""
if "--model" in argv:
    model = argv[argv.index("--model") + 1]
provider = argv[argv.index("--provider") + 1] if "--provider" in argv else ""

if "flash" in model.lower():
    answer = os.environ.get("FAKE_PROVIDER_ANSWER", '{"headline": "x", "experience": []}')
    print(json.dumps({"type": "session", "version": 3, "id": f"flash-{slot}", "cwd": os.getcwd()}))
    print(json.dumps({"type": "agent_start"}))
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                    "provider": provider,
                    "model": model,
                    "usage": {
                        "input": 10,
                        "output": 5,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 15,
                        "cost": {"total": 0.0001},
                    },
                    "stopReason": "stop",
                    "timestamp": slot,
                },
            }
        )
    )
    print(json.dumps({"type": "agent_end", "messages": []}))
    raise SystemExit(0)

base = float(os.environ.get("FAKE_PROVIDER_BASE_SLEEP", "0.2"))
step = float(os.environ.get("FAKE_PROVIDER_SLEEP_STEP", "0.4"))
time.sleep(base + step * slot)
sys.stderr.write(os.environ.get("FAKE_PROVIDER_STDERR", "Error: quota exceeded for openai-codex\n"))
raise SystemExit(1)
