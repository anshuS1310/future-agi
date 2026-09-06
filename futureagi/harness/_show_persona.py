import sys, json, importlib
sys.path.insert(0, "/app/src")
Scenario = importlib.import_module("harness.scenario").Scenario
path = sys.argv[1]
s = Scenario.model_validate(json.load(open(path)))
print(f"--- {s.name} ---")
print(f"use_case: {s.use_case}")
print(f"branch:   {s.branch}")
print()
print(s.persona.format_persona() if s.persona else "(no persona)")
