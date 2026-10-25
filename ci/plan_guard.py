#!/usr/bin/env python3
"""Check a saved Terraform plan against the deployment kind and summarise it.

    terraform show -json tfplan > plan.json
    python ci/plan_guard.py --plan-json plan.json --kind application --summary "$GITHUB_STEP_SUMMARY"

Deployment kinds (input of .github/workflows/deploy.yml):
  application  Only a new image: ECS task definitions, the ECS service and the EC2
               code bundle may change. Traffic weights, storage mode and all other
               infrastructure are refused, so a routine release can never move traffic.
  platform     Reviewed infrastructure or migration changes (incl. traffic_stage).
               Everything is allowed except deleting/replacing the DynamoDB table;
               traffic, storage-mode and EC2 changes are highlighted for the reviewer.

The summary is sanitised: resource addresses, actions, traffic weights and the image
digest only - never attribute values (the full plan stays in the private S3 store).
Exit codes: 0 = allowed, 1 = refused, 2 = usage/input error.
"""

import argparse
import json
import re
import sys

PROTECTED = {"aws_dynamodb_table.app"}  # shared data: never deleted or replaced by a pipeline

APPLICATION_ALLOWED = [
    (re.compile(r"^aws_ecs_task_definition\.(app|seed)\[0\]$"), {"create", "delete", "update"}),
    (re.compile(r"^aws_ecs_service\.app\[0\]$"), {"update"}),
    (re.compile(r"^aws_s3_object\.app$"), {"update"}),
]

IGNORED_ACTIONS = ({"no-op"}, {"read"})


def describe(actions):
    s = set(actions)
    if s == {"create", "delete"}:
        return "replace"
    return "+".join(actions)


def load_changes(plan):
    changes = []
    for rc in plan.get("resource_changes", []):
        if rc.get("mode") != "managed":
            continue
        actions = rc.get("change", {}).get("actions", [])
        if set(actions) in IGNORED_ACTIONS:
            continue
        changes.append((rc["address"], actions))
    return changes


def output_change(plan, name):
    oc = plan.get("output_changes", {}).get(name)
    if not oc:
        return None, None
    return oc.get("before"), oc.get("after")


def evaluate(plan, kind):
    """Return (violations, notes, changes)."""
    changes = load_changes(plan)
    violations, notes = [], []

    for address, actions in changes:
        if address in PROTECTED and "delete" in actions:
            violations.append(f"{address}: {describe(actions)} is never allowed from the pipeline (shared data)")

    if kind == "application":
        for address, actions in changes:
            allowed = next((ok for rx, ok in APPLICATION_ALLOWED if rx.match(address)), None)
            if allowed is None or not set(actions) <= allowed:
                violations.append(
                    f"{address}: {describe(actions)} is not part of an application deployment "
                    f"(use deployment_kind=platform after review)")

    w_before, w_after = output_change(plan, "traffic_weights")
    if w_after is not None and w_before != w_after:
        msg = f"TRAFFIC WEIGHTS CHANGE: {fmt_weights(w_before)} -> {fmt_weights(w_after)}"
        (violations if kind == "application" else notes).append(msg)

    s_before, s_after = output_change(plan, "legacy_storage_backend")
    if s_after is not None and s_before is not None and s_before != s_after:
        msg = f"LEGACY STORAGE MODE CHANGE: {s_before} -> {s_after} (replaces the EC2 instance; write freeze required)"
        (violations if kind == "application" else notes).append(msg)

    for address, actions in changes:
        if address == "aws_instance.app" and "delete" in actions and kind == "platform":
            notes.append("aws_instance.app is REPLACED: the legacy rollback target is rebuilt")
        elif "delete" in actions and kind == "platform" and address not in PROTECTED:
            notes.append(f"{address} will be {describe(actions)}d" if describe(actions) == "replace"
                         else f"{address} will be deleted")

    return violations, notes, changes


def fmt_weights(w):
    if not isinstance(w, dict):
        return "n/a"
    return f"stage {w.get('stage')} (EC2 {w.get('ec2')}% / ECS {w.get('ecs')}%)"


def summary_markdown(plan, kind, violations, notes, changes):
    counts = {}
    for _, actions in changes:
        counts[describe(actions)] = counts.get(describe(actions), 0) + 1
    _, image = output_change(plan, "app_image")
    _, weights = output_change(plan, "traffic_weights")

    lines = [f"## Terraform plan ({kind} deployment)", ""]
    lines.append("**Result:** " + ("REFUSED by plan guard" if violations else
                                   ("no changes" if not changes else "allowed - awaiting approval")))
    lines.append("")
    if image:
        lines.append(f"- Image: `{image.split('/')[-1]}`")
    if weights:
        lines.append(f"- Traffic after apply: {fmt_weights(weights)}")
    if counts:
        lines.append("- Changes: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    lines.append("")
    for v in violations:
        lines.append(f"> :x: {v}")
    for n in notes:
        lines.append(f"> :warning: {n}")
    if changes:
        lines += ["", "| Resource | Action |", "|---|---|"]
        lines += [f"| `{a}` | {describe(acts)} |" for a, acts in changes]
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan-json", required=True)
    ap.add_argument("--kind", required=True, choices=["application", "platform"])
    ap.add_argument("--summary", help="file to append the markdown summary to")
    args = ap.parse_args(argv)

    try:
        with open(args.plan_json, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"plan_guard: cannot read plan JSON: {exc}", file=sys.stderr)
        return 2

    violations, notes, changes = evaluate(plan, args.kind)
    md = summary_markdown(plan, args.kind, violations, notes, changes)
    print(md)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if violations:
        print(f"plan_guard: refused ({len(violations)} violation(s))", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
