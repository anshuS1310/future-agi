"""Local-only: prove that asking in chat fans out, rather than writing one at a time.

Opens the scenarios stage the way the UI does and says the thing a person would say. What we
are checking is which tool it reaches for: generate_suite means the fan-out happened, a run of
submit_scenario calls means it did not.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/src")

scenario_stage = importlib.import_module("harness.scenarios")
understand_stage = importlib.import_module("harness.understand")

SESSION = Path(os.environ.get("CHAT_SESSION", "/app/artifacts/sessions/ride-chat-test"))
ASK = os.environ.get("CHAT_ASK", "create 6 scenarios")

called: list[str] = []


def watch(event) -> None:
    tool = getattr(event, "tool", "") or ""
    if tool:
        called.append(tool)
        print(f"[tool] {tool}", flush=True)
    text = getattr(event, "text", "") or ""
    if getattr(event, "kind", "") == "text" and text.strip():
        print(f"[said] {text.strip()[:200]}", flush=True)


async def main() -> None:
    contract = understand_stage.load(SESSION)
    if contract is None:
        raise SystemExit(f"no contract at {SESSION}")
    stage, destination = scenario_stage.open_stage(contract, out=SESSION, wanted=6)
    print(f"asking: {ASK!r}", flush=True)
    started = time.time()
    async with stage:
        await stage.say(ASK, on_event=watch)
    took = time.time() - started

    fanned = any("generate_suite" in one for one in called)
    submits = sum(1 for one in called if "submit_scenario" in one)
    kept = scenario_stage.load(destination)
    print(f"\n=== {len(kept)} scenarios in {took:.0f}s ===")
    print(f"generate_suite called: {fanned}")
    print(f"submit_scenario calls in this session: {submits}")
    print(f"tools used: {sorted(set(called))}")


asyncio.run(main())
