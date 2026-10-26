#!/usr/bin/env python3
"""Refresh ci/tests/data/iam-reference.json from the AWS Service Authorization Reference.

    python ci/refresh_iam_reference.py

Downloads the machine-readable service reference
(https://servicereference.us-east-1.amazonaws.com/) for every service used by the
pipeline policies in terraform/bootstrap/policies and stores a trimmed copy:

    {"services": {"<prefix>": {"actions": [all action names],
                               "keys": {"<Action>": [condition keys]}}}}

"keys" is kept only for actions the policies name exactly; it is the union of the
action's own condition keys and the keys of its resource types. CI uses this file
offline (ci/tests/test_rendered_policy_gate.py). Re-run it when a policy names a new
action; review the diff like any other change.
"""

import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "terraform" / "bootstrap" / "policies"
OUT = ROOT / "ci" / "tests" / "data" / "iam-reference.json"
BASE = "https://servicereference.us-east-1.amazonaws.com/v1"


def policy_actions():
    """All Action strings in the templates (rendered with placeholder values)."""
    actions = set()
    for f in POLICY_DIR.glob("*.json.tftpl"):
        text = re.sub(r"\$\{[a-z_]+\}", "x", f.read_text(encoding="utf-8"))
        for stmt in json.loads(text)["Statement"]:
            act = stmt["Action"]
            actions |= set(act if isinstance(act, list) else [act])
    return actions


def fetch(prefix):
    with urllib.request.urlopen(f"{BASE}/{prefix}/{prefix}.json", timeout=60) as resp:  # noqa: S310 - fixed AWS URL
        return json.load(resp)


def main():
    used = policy_actions()
    services = {}
    for prefix in sorted({a.split(":")[0] for a in used}):
        data = fetch(prefix)
        res_keys = {r["Name"]: set(r.get("ConditionKeys", [])) for r in data.get("Resources", [])}
        exact = {a.split(":")[1] for a in used if a.startswith(prefix + ":") and not re.search(r"[*?]", a)}
        keys = {}
        for act in data["Actions"]:
            if act["Name"] in exact:
                k = set(act.get("ActionConditionKeys", []))
                for r in act.get("Resources", []):
                    k |= res_keys.get(r["Name"], set())
                keys[act["Name"]] = sorted(k)
        services[prefix] = {"actions": sorted(a["Name"] for a in data["Actions"]), "keys": keys}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": BASE, "retrieved": date.today().isoformat(), "services": services},
                              indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(services)} services)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
