import json, os, sys, importlib
sys.path.insert(0, "/app/src")
pg = importlib.import_module("harness.persona_guides")
root = sys.argv[1]
rows = []
for name in sorted(os.listdir(f"{root}/scenarios")):
    p = f"{root}/scenarios/{name}/scenario.json"
    if not os.path.exists(p):
        continue
    s = json.load(open(p))
    per = s.get("persona") or {}
    rows.append((name, per, pg.unrecognised(per)))
bad = [r for r in rows if r[2]]
print(f"{len(rows)} scenarios, {len(bad)} with values the platform would not recognise")
print()
for name, per, problems in rows:
    guide = pg.guidance_for("personality", per.get("personality", ""))
    mark = "!" if problems else "+"
    print(f" {mark} {name[:38]:40s} {str(per.get('personality'))[:26]:28s} accent={str(per.get('accent'))[:10]:12s} langs={per.get('languages')}")
    if guide:
        print(f"      guidance: {guide[:96]}")
    for one in problems:
        print(f"      PROBLEM: {one[:110]}")
