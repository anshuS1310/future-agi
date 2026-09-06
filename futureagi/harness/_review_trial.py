"""Local-only: exercise the review pass against a finished suite."""
from __future__ import annotations
import asyncio, importlib, sys, time
from pathlib import Path
sys.path.insert(0, "/app/src")
scenarios = importlib.import_module("harness.scenarios")
understand = importlib.import_module("harness.understand")

SESSION = Path("/app/artifacts/sessions/ride-planned")

async def main():
    contract = understand.load(SESSION)
    suite = scenarios.load(SESSION)
    print(f"reviewing {len(suite)} scenarios against a target of 12", flush=True)
    started = time.time()
    gaps = await scenarios.gaps_in(contract, suite, destination=SESSION, wanted=12)
    print(f"\n=== {len(gaps)} gaps in {time.time()-started:.0f}s ===")
    for g in gaps:
        print(f"  {g.use_case[:44]!r}")
        print(f"      angle: {g.angle[:110]}")
        if g.why: print(f"      why:   {g.why[:110]}")

asyncio.run(main())
