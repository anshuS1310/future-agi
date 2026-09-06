"""Local-only: run a suite from a deliberate plan and time it."""
from __future__ import annotations
import asyncio, importlib, json, os, sys, time
sys.path.insert(0, "/app/src")
scenario_stage = importlib.import_module("harness.scenarios")
understand_stage = importlib.import_module("harness.understand")

SESSION = os.environ.get("TRIAL_SESSION", "/app/artifacts/sessions/ride-planned")
from pathlib import Path
SESSION = Path(SESSION)

def say(e):
    if isinstance(e, dict) and e.get("type") in {"planned", "saved", "topping_up", "slice_failed"}:
        print(f"[event] {json.dumps(e)[:400]}", flush=True)

async def main():
    contract = understand_stage.load(SESSION)
    cases = [c for c in contract.real_use_cases if c.strip()]
    # a deliberate plan: uneven, with angles, the way a thinking orchestrator would split it
    plan = [
        {"use_case": cases[0], "angle": "the rule under pressure: caller pushes to skip a step",
         "count": 3, "why": "identity and payment rules concentrate here"},
        {"use_case": cases[1], "angle": "the ordinary path, done cleanly",
         "count": 1, "why": "baseline only, little can go wrong"},
        {"use_case": cases[5], "angle": "the branch that cannot be completed",
         "count": 2, "why": "refusal behaviour is the whole question"},
    ]
    print(f"plan: {[(p['use_case'][:34], p['count']) for p in plan]}", flush=True)
    started = time.time()
    kept = await scenario_stage.write_in_parallel(
        contract, out=SESSION, wanted=6, use_cases=cases, slices=plan, at_once=3, on_event=say
    )
    print(f"\n=== {len(kept)} scenarios in {time.time()-started:.0f}s ===", flush=True)
    for one in kept:
        print(f"  {one.name[:40]:42s} uc={one.use_case[:34]!r} branch={one.branch[:44]!r}")

asyncio.run(main())
