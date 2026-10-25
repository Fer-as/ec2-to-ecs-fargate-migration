#!/usr/bin/env python3
"""Markdown summary of a Trivy JSON report (all findings, fixed and unfixed).

    python ci/trivy_summary.py trivy-full.json >> "$GITHUB_STEP_SUMMARY"

Informational only: the blocking gate is a separate Trivy run with
--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1. Unfixed findings are
listed here so they stay visible for risk review (docs/security-exceptions.md).
"""

import collections
import json
import sys

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]


def summarise(report):
    unique = collections.defaultdict(set)          # severity -> CVE ids
    fixable = collections.defaultdict(set)         # severity -> CVE ids with a fix
    by_type = collections.Counter()
    high_rows = {}
    secrets = 0
    for result in report.get("Results", []):
        secrets += len(result.get("Secrets") or [])
        for v in result.get("Vulnerabilities") or []:
            sev, vid = v.get("Severity", "UNKNOWN"), v["VulnerabilityID"]
            unique[sev].add(vid)
            by_type[result.get("Type", "?")] += 1
            if v.get("FixedVersion"):
                fixable[sev].add(vid)
            if sev in ("CRITICAL", "HIGH"):
                row = high_rows.setdefault(vid, {"sev": sev, "pkgs": set(), "fixed": v.get("FixedVersion") or "", "status": v.get("Status", "")})
                row["pkgs"].add(v.get("PkgName", "?"))
    return unique, fixable, by_type, high_rows, secrets


def render(report):
    unique, fixable, by_type, high_rows, secrets = summarise(report)
    out = ["## Trivy image scan (full report)", "",
           "| Severity | Unique CVEs | With a fix available |", "|---|---|---|"]
    out += [f"| {s} | {len(unique[s])} | {len(fixable[s])} |" for s in SEVERITIES]
    out += ["", f"- Findings by package type: {dict(by_type) or 'none'}", f"- Secrets: {secrets}", ""]
    gate = sum(len(fixable[s]) for s in ("CRITICAL", "HIGH"))
    out.append(f"**Gate input:** {gate} fixable HIGH/CRITICAL CVE(s) "
               f"({'gate will FAIL' if gate or secrets else 'gate passes'}).")
    if high_rows:
        out += ["", "| HIGH/CRITICAL CVE | Packages | Status | Fixed in |", "|---|---|---|---|"]
        for vid, r in sorted(high_rows.items()):
            out.append(f"| {vid} ({r['sev']}) | {', '.join(sorted(r['pkgs']))} | {r['status']} | {r['fixed'] or '-'} |")
        out += ["", "Unfixed HIGH findings are tracked in docs/security-exceptions.md."]
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        sys.stdout.write(render(json.load(fh)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
