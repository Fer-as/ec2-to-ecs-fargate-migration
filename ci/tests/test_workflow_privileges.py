"""Privilege policy for EVERY workflow in .github/workflows (not a fixed list of names).

`privilege_violations()` returns human-readable violations for a set of parsed
workflows. The real workflows must produce none. The negative tests feed it
deliberately unsafe variants (in memory only) and require each to be rejected, so
the policy itself is tested. The environment NAME is checked here; whether
`production` really has required reviewers is a GitHub setting, verified separately
(docs/approval-gate-test.md).
"""

import copy
import re
from pathlib import Path

import pytest
import yaml

WF_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# The only jobs that may request an OIDC token (and so reach AWS)
OIDC_JOBS = {("deploy.yml", "publish"), ("deploy.yml", "plan"), ("deploy.yml", "apply")}
# The only jobs that may use the production environment
PRODUCTION_JOBS = {("deploy.yml", "apply"), ("approval-gate-test.yml", "approval")}
# Every privileged deploy job must (transitively) depend on these
DEPLOY_GATES = {"preflight", "ci", "terraform-ci"}
PRIVILEGED_RUN = [
    (re.compile(r"(^|[\s;&|(])aws\s"), "AWS CLI call"),
    (re.compile(r"docker\s+push\b"), "image push"),
    (re.compile(r"terraform\b[^\n]*\s(apply|destroy|import)\b"), "terraform apply/destroy/import"),
    (re.compile(r"ACTIONS_ID_TOKEN_REQUEST"), "OIDC token request"),
]
ALLOWED_JOB_PERMISSIONS = {"contents": "read", "id-token": "write"}


def triggers(wf):
    on = wf.get("on", wf.get(True))  # PyYAML (YAML 1.1) reads the key "on" as True
    if isinstance(on, str):
        return {on}
    return set(on or {})


def load_all():
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(WF_DIR.glob("*.y*ml"))}


def _needs(job):
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def _ancestors(jobs, job_id):
    seen, todo = set(), _needs(jobs[job_id])
    while todo:
        j = todo.pop()
        if j not in seen and j in jobs:
            seen.add(j)
            todo += _needs(jobs[j])
    return seen


def _env_name(job):
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def _is_privileged(job):
    perms = job.get("permissions") or {}
    if isinstance(perms, dict) and perms.get("id-token") == "write":
        return True
    for step in job.get("steps", []):
        if "configure-aws-credentials" in step.get("uses", ""):
            return True
        if any(rx.search(step.get("run", "")) for rx, _ in PRIVILEGED_RUN):
            return True
    return False


def privilege_violations(workflows):
    v = []
    for name, wf in workflows.items():
        trig = triggers(wf)
        jobs = wf.get("jobs", {})
        top = wf.get("permissions")
        if top not in ({"contents": "read"}, {}):
            v.append(f"{name}: top-level permissions must be exactly 'contents: read' (got {top!r})")
        if "pull_request_target" in trig or "workflow_run" in trig:
            v.append(f"{name}: pull_request_target/workflow_run triggers are not allowed")

        for job_id, job in jobs.items():
            key = (name, job_id)
            perms = job.get("permissions")
            if isinstance(perms, str):
                v.append(f"{name}/{job_id}: permissions shorthand {perms!r} not allowed")
                perms = {}
            for scope, level in (perms or {}).items():
                if ALLOWED_JOB_PERMISSIONS.get(scope) != level:
                    v.append(f"{name}/{job_id}: permission {scope}: {level} not allowed")

            privileged = _is_privileged(job)
            if privileged and key not in OIDC_JOBS:
                v.append(f"{name}/{job_id}: unexpected privileged job (OIDC token, AWS credentials, push or apply)")
            if (perms or {}).get("id-token") == "write" and key not in OIDC_JOBS:
                v.append(f"{name}/{job_id}: id-token: write outside the approved deploy jobs")

            env = _env_name(job)
            if env and str(env).lower() == "production" and key not in PRODUCTION_JOBS:
                v.append(f"{name}/{job_id}: uses the production environment but is not an approved production job")

            if key in OIDC_JOBS or privileged:
                if trig != {"workflow_dispatch"}:
                    v.append(f"{name}/{job_id}: privileged job reachable from {sorted(trig)} (only workflow_dispatch)")
                missing = DEPLOY_GATES - _ancestors(jobs, job_id) if name == "deploy.yml" else set()
                if missing:
                    v.append(f"{name}/{job_id}: privileged job does not depend on {sorted(missing)}")
                cond = str(job.get("if", ""))
                if re.search(r"\b(always|failure|cancelled)\s*\(", cond):
                    v.append(f"{name}/{job_id}: condition {cond!r} can run after a failed gate")

        if "pull_request" in trig:
            for job_id, job in jobs.items():
                if _is_privileged(job) or _env_name(job):
                    v.append(f"{name}/{job_id}: pull-request workflow has a privileged or environment job")

    # Structural requirements for the two workflows that touch production
    deploy = workflows.get("deploy.yml")
    if deploy is None:
        v.append("deploy.yml missing")
    else:
        jobs = deploy.get("jobs", {})
        apply = jobs.get("apply", {})
        if str(_env_name(apply)).lower() != "production":
            v.append("deploy.yml/apply: must run in environment production")
        if "plan" not in _needs(apply) or not {"publish"} | DEPLOY_GATES <= _ancestors(jobs, "apply"):
            v.append("deploy.yml/apply: must depend on plan, publish, preflight, ci and terraform-ci")
        guard = (jobs.get("preflight", {}).get("steps") or [{}])[0]
        if guard.get("if") != "github.ref != 'refs/heads/main'":
            v.append("deploy.yml/preflight: first step must refuse refs other than main")

    gate = workflows.get("approval-gate-test.yml")
    if gate is not None:
        text = yaml.safe_dump(gate)
        if triggers(gate) != {"workflow_dispatch"}:
            v.append("approval-gate-test.yml: only workflow_dispatch allowed")
        if set(gate.get("jobs", {})) != {"approval"}:
            v.append("approval-gate-test.yml: exactly one job ('approval') expected")
        for word in ("secrets.", "vars.", "id-token", "aws", "terraform", "docker", "curl", "gh "):
            if word in text:
                v.append(f"approval-gate-test.yml: must not reference {word!r}")
        steps = gate.get("jobs", {}).get("approval", {}).get("steps", [])
        if not steps or steps[0].get("if") != "github.ref != 'refs/heads/main'" or "exit 1" not in steps[0].get("run", ""):
            v.append("approval-gate-test.yml: first step must refuse refs other than main")
        if any("uses" in s for s in steps):
            v.append("approval-gate-test.yml: no third-party actions (not even checkout)")
    return v


REAL = load_all()


def test_every_workflow_file_is_covered():
    assert {"ci.yml", "terraform-ci.yml", "deploy.yml", "approval-gate-test.yml"} <= set(REAL)
    assert not list(WF_DIR.glob("*.yaml")), "use .yml so every workflow is covered by one pattern"


def test_real_workflows_satisfy_the_privilege_policy():
    assert privilege_violations(REAL) == []


def test_only_the_three_deploy_jobs_are_privileged():
    privileged = {(n, j) for n, wf in REAL.items() for j, job in wf["jobs"].items() if _is_privileged(job)}
    assert privileged == OIDC_JOBS


def test_approval_gate_test_has_no_route_to_aws():
    job = REAL["approval-gate-test.yml"]["jobs"]["approval"]
    assert job["permissions"] == {"contents": "read"} and not _is_privileged(job)
    assert _env_name(job) == "production"


# --- negative tests: each unsafe variant must be rejected ------------------------------------

def _mutated(fn):
    wfs = copy.deepcopy(REAL)
    fn(wfs)
    return wfs


UNSAFE = {
    "new workflow requesting an OIDC token": lambda w: w.__setitem__("sneaky.yml", {
        "on": {"workflow_dispatch": None}, "permissions": {"contents": "read"},
        "jobs": {"x": {"runs-on": "ubuntu-24.04", "permissions": {"id-token": "write"}, "steps": [{"run": "true"}]}}}),
    "CI requesting an OIDC token": lambda w: w["ci.yml"]["jobs"]["test"].__setitem__(
        "permissions", {"contents": "read", "id-token": "write"}),
    "Terraform CI configuring AWS credentials": lambda w: w["terraform-ci.yml"]["jobs"]["validate"]["steps"].append(
        {"uses": "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd"}),
    "apply job without the production environment": lambda w: w["deploy.yml"]["jobs"]["apply"].pop("environment"),
    "apply job in a different environment": lambda w: w["deploy.yml"]["jobs"]["apply"].__setitem__(
        "environment", {"name": "staging"}),
    "deploy reachable from pull requests": lambda w: w["deploy.yml"].__setitem__(
        "on", {"workflow_dispatch": None, "pull_request": {"branches": ["main"]}}),
    "deploy on push": lambda w: w["deploy.yml"].__setitem__("on", {"push": {"branches": ["main"]}}),
    "publish skipping the CI gates": lambda w: w["deploy.yml"]["jobs"]["publish"].__setitem__("needs", []),
    "apply running even if plan failed": lambda w: w["deploy.yml"]["jobs"]["apply"].__setitem__("if", "always()"),
    "apply no longer needs plan": lambda w: w["deploy.yml"]["jobs"]["apply"].__setitem__("needs", ["ci"]),
    "main-branch guard removed from deploy": lambda w: w["deploy.yml"]["jobs"]["preflight"]["steps"].pop(0),
    "write-all permissions": lambda w: w["ci.yml"].__setitem__("permissions", "write-all"),
    "contents: write on a job": lambda w: w["ci.yml"]["jobs"]["lint"].__setitem__("permissions", {"contents": "write"}),
    "CI pushing an image": lambda w: w["ci.yml"]["jobs"]["container"]["steps"].append(
        {"run": "docker push example/legacy-api:latest"}),
    "CI job using the production environment": lambda w: w["ci.yml"]["jobs"]["lint"].__setitem__(
        "environment", "production"),
    "approval test given an OIDC token": lambda w: w["approval-gate-test.yml"]["jobs"]["approval"].__setitem__(
        "permissions", {"contents": "read", "id-token": "write"}),
    "approval test calling AWS": lambda w: w["approval-gate-test.yml"]["jobs"]["approval"]["steps"].append(
        {"run": "aws sts get-caller-identity"}),
    "approval test reading a secret": lambda w: w["approval-gate-test.yml"]["jobs"]["approval"]["steps"].append(
        {"run": "echo ${{ secrets.TF_ALARM_EMAIL }}"}),
    "approval test on pull requests": lambda w: w["approval-gate-test.yml"].__setitem__(
        "on", {"workflow_dispatch": None, "pull_request": None}),
    "approval test main guard removed": lambda w: w["approval-gate-test.yml"]["jobs"]["approval"]["steps"].pop(0),
    "pull_request_target trigger": lambda w: w["ci.yml"].__setitem__("on", {"pull_request_target": None}),
}


@pytest.mark.parametrize("label", sorted(UNSAFE))
def test_unsafe_variant_is_rejected(label):
    violations = privilege_violations(_mutated(UNSAFE[label]))
    assert violations, f"policy did not reject: {label}"
