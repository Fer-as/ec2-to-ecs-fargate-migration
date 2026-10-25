"""Unit tests for ci/trivy_summary.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trivy_summary  # noqa: E402


def vuln(vid, sev, pkg, fixed=""):
    return {"VulnerabilityID": vid, "Severity": sev, "PkgName": pkg, "FixedVersion": fixed,
            "Status": "fixed" if fixed else "affected"}


def test_unfixed_high_is_reported_but_not_gate_failing():
    report = {"Results": [{"Type": "debian", "Vulnerabilities": [
        vuln("CVE-1", "HIGH", "util-linux"), vuln("CVE-1", "HIGH", "mount"), vuln("CVE-2", "LOW", "perl")]}]}
    md = trivy_summary.render(report)
    assert "| HIGH | 1 | 0 |" in md
    assert "CVE-1 (HIGH) | mount, util-linux | affected | - |" in md
    assert "gate passes" in md


def test_fixable_critical_is_flagged():
    report = {"Results": [{"Type": "python-pkg", "Vulnerabilities": [vuln("CVE-3", "CRITICAL", "flask", "9.9")]}]}
    md = trivy_summary.render(report)
    assert "| CRITICAL | 1 | 1 |" in md and "gate will FAIL" in md


def test_secrets_fail_the_gate():
    md = trivy_summary.render({"Results": [{"Type": "text", "Secrets": [{"RuleID": "aws-access-key-id"}]}]})
    assert "Secrets: 1" in md and "gate will FAIL" in md
