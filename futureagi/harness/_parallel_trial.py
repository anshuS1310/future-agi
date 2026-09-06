"""Local-only: drive scenario generation and time it. Not part of the harness."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/src")

import importlib  # noqa: E402

# `from harness import understand` binds the re-exported function, not the module.
scenario_stage = importlib.import_module("harness.scenarios")
understand_stage = importlib.import_module("harness.understand")

SESSION = Path(os.environ.get("TRIAL_SESSION", "/app/artifacts/sessions/ride-parallel-trial"))
WANTED = int(os.environ.get("TRIAL_WANTED", "6"))
SLICES = int(os.environ.get("TRIAL_SLICES", "3"))
AT_ONCE = int(os.environ.get("TRIAL_AT_ONCE", "3"))
MODE = os.environ.get("TRIAL_MODE", "parallel")


def say(event: object) -> None:
    if isinstance(event, dict) and event.get("type") in {
        "planned",
        "saved",
        "slice_failed",
    }:
        print(f"[event] {json.dumps(event)[:400]}", flush=True)


async def main() -> None:
    contract = understand_stage.load(SESSION)
    if contract is None:
        raise SystemExit(f"no contract at {SESSION}")
    cases = [c for c in contract.real_use_cases if c.strip()][:SLICES]
    print(f"agent={contract.agent} use_cases_available={len(contract.real_use_cases)}")
    print(f"mode={MODE} wanted={WANTED} slices={len(cases)} at_once={AT_ONCE}", flush=True)

    started = time.time()
    if MODE == "parallel":
        kept = await scenario_stage.write_in_parallel(
            contract,
            out=SESSION,
            wanted=WANTED,
            use_cases=cases,
            at_once=AT_ONCE,
            on_event=say,
        )
    else:
        kept = await scenario_stage.write(
            contract, out=SESSION, wanted=WANTED, on_event=say
        )
    took = time.time() - started

    print(f"\n=== {MODE}: {len(kept)} scenarios in {took:.0f}s ===", flush=True)
    for one in kept:
        print(f"  {one.name}  | use_case={one.use_case[:48]!r} branch={one.branch[:48]!r}")


asyncio.run(main())
