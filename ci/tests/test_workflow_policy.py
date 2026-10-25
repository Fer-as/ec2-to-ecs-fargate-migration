"""Static policy checks for .github/workflows (runs locally and in CI).

These are the security invariants of the pipeline. A change that breaks one of
them must be a deliberate, reviewed edit of this file.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WF_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = {p.name: p for p in sorted(WF_DIR.glob("*.yml"))}
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
DIGEST_PIN = re.compile(r"^[\w./-]+@sha256:[0-9a-f]{64}$")


def load(name):
    return yaml.safe_load(WORKFLOWS[name].read_text(encoding="utf-8"))


def triggers(wf):
    return wf.get("on", wf.get(True))  # PyYAML (YAML 1.1) reads the key "on" as True


def steps(wf):
    for job_id, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, step


def test_expected_workflows_exist():
    assert {"ci.yml", "terraform-ci.yml", "deploy.yml"} <= set(WORKFLOWS)


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_no_static_aws_credentials_or_dangerous_triggers(name):
    text = WORKFLOWS[name].read_text(encoding="utf-8")
    assert "aws-access-key-id" not in text and "aws-secret-access-key" not in text
    assert not re.search(r"AKIA[0-9A-Z]{16}", text)
    assert "pull_request_target" not in triggers(load(name))
    assert "secrets.AWS_" not in text


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_actions_pinned_to_commit_sha_and_images_to_digest(name):
    wf = load(name)
    for _, step in steps(wf):
        if "uses" in step:
            assert SHA_PIN.match(step["uses"]), f"{name}: unpinned action {step['uses']}"
    for job in wf["jobs"].values():
        if job.get("uses"):
            assert job["uses"].startswith("./.github/workflows/"), "only local reusable workflows"
        for svc in job.get("services", {}).values():
            assert DIGEST_PIN.match(svc["image"]), f"{name}: service image not pinned by digest"
    for key, value in wf.get("env", {}).items():
        if key.endswith("_IMAGE"):
            assert DIGEST_PIN.match(value), f"{name}: {key} not pinned by digest"


@pytest.mark.parametrize("name", ["ci.yml", "terraform-ci.yml"])
def test_ci_workflows_never_get_aws_credentials(name):
    wf = load(name)
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        assert "id-token" not in job.get("permissions", {})
    for _, step in steps(wf):
        assert "configure-aws-credentials" not in step.get("uses", "")
        assert "aws " not in step.get("run", "")
    assert "workflow_call" in triggers(wf), "must be reusable as a deploy gate"


def test_only_deploy_jobs_request_oidc_tokens():
    wf = load("deploy.yml")
    assert wf["permissions"] == {"contents": "read"}
    oidc_jobs = {j for j, job in wf["jobs"].items() if job.get("permissions", {}).get("id-token") == "write"}
    assert oidc_jobs == {"publish", "plan", "apply"}


def test_deploy_is_manual_main_only_and_serialised():
    wf = load("deploy.yml")
    assert set(triggers(wf)) == {"workflow_dispatch"}
    assert wf["concurrency"] == {"group": "deploy-production", "cancel-in-progress": False}
    guard = wf["jobs"]["preflight"]["steps"][0]
    assert guard["if"] == "github.ref != 'refs/heads/main'"


def test_deploy_job_order_gates_publish_plan_apply():
    jobs = load("deploy.yml")["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["terraform-ci"]["uses"] == "./.github/workflows/terraform-ci.yml"
    assert set(jobs["publish"]["needs"]) == {"ci", "terraform-ci"}
    assert jobs["plan"]["needs"] == "publish"
    assert jobs["apply"]["needs"] == ["plan"]


def test_each_job_uses_its_own_role():
    jobs = load("deploy.yml")["jobs"]
    roles = {}
    for job_id in ("publish", "plan", "apply"):
        creds = [s for s in jobs[job_id]["steps"] if "configure-aws-credentials" in s.get("uses", "")]
        assert len(creds) == 1
        roles[job_id] = creds[0]["with"]["role-to-assume"]
        assert creds[0]["with"]["allowed-account-ids"] == "${{ vars.AWS_ACCOUNT_ID }}"
    assert roles == {"publish": "${{ vars.AWS_IMAGE_PUSH_ROLE_ARN }}",
                     "plan": "${{ vars.AWS_PLAN_ROLE_ARN }}",
                     "apply": "${{ vars.AWS_APPLY_ROLE_ARN }}"}


def test_apply_only_in_approved_environment_and_only_the_saved_plan():
    for name in WORKFLOWS:
        for job_id, step in steps(load(name)):
            run = step.get("run", "")
            if re.search(r"terraform\b.*\bapply\b", run):
                assert (name, job_id) == ("deploy.yml", "apply"), f"terraform apply outside the gated job: {name}/{job_id}"
                assert re.search(r"apply -lock-timeout=5m -no-color tfplan$", run.strip()), "apply must use the saved plan"
                assert "-auto-approve" not in run and "-target" not in run and "destroy" not in run
    apply = load("deploy.yml")["jobs"]["apply"]
    assert apply["environment"]["name"] == "production"
    names = [s.get("name", s.get("uses", "")) for s in apply["steps"]]
    creds = next(i for i, n in enumerate(names) if "configure-aws-credentials" in n)
    verify = names.index("Fetch the approved plan and verify its checksum")
    do_apply = names.index("terraform apply EXACTLY the approved saved plan")
    assert creds < verify < do_apply, "fresh credentials, then checksum, then apply"
    assert "sha256sum -c" in apply["steps"][verify]["run"]


def test_plan_is_saved_privately_not_as_artifact():
    plan = load("deploy.yml")["jobs"]["plan"]
    runs = "\n".join(s.get("run", "") for s in plan["steps"])
    assert "-out=tfplan" in runs and "plan_guard.py" in runs and "--sse AES256" in runs
    assert not any("upload-artifact" in s.get("uses", "") for s in plan["steps"])
    assert "plan.log" in runs and 'cat "$RUNNER_TEMP/plan.log"' not in runs


def test_image_is_referenced_by_verified_digest():
    jobs = load("deploy.yml")["jobs"]
    digest_step = next(s for s in jobs["publish"]["steps"] if s.get("id") == "digest")
    assert "^sha256:[a-f0-9]{64}$" in digest_step["run"]
    assert jobs["publish"]["env"]["IMAGE_TAG"] == "${{ github.sha }}"
    assert jobs["plan"]["env"]["IMAGE_DIGEST"] == "${{ needs.publish.outputs.image_digest }}"
    assert ":latest" not in WORKFLOWS["deploy.yml"].read_text(encoding="utf-8")


def test_no_out_of_band_ecs_or_listener_changes():
    for name in WORKFLOWS:
        for job_id, step in steps(load(name)):
            run = step.get("run", "")
            for forbidden in ("update-service", "register-task-definition", "modify-listener", "modify-rule"):
                assert forbidden not in run, f"{name}/{job_id}: Terraform owns ECS and ALB routing ({forbidden})"


@pytest.mark.parametrize("name,gate,expected", [
    ("ci.yml", "ci-gate", {"lint", "test", "container", "secrets"}),
    ("terraform-ci.yml", "terraform-gate", {"validate", "user-data", "checkov"}),
])
def test_gate_job_depends_on_every_job(name, gate, expected):
    jobs = load(name)["jobs"]
    assert set(jobs[gate]["needs"]) == expected == set(jobs) - {gate}
    assert jobs[gate]["if"] == "always()"


def test_trivy_gate_blocks_fixable_high_and_critical():
    container = load("ci.yml")["jobs"]["container"]
    gate = next(s for s in container["steps"] if s.get("name", "").startswith("Trivy gate"))
    for flag in ("--severity HIGH,CRITICAL", "--ignore-unfixed", "--exit-code 1", "--scanners vuln,secret"):
        assert flag in gate["run"]
    assert "continue-on-error" not in gate
    text = WORKFLOWS["ci.yml"].read_text(encoding="utf-8") + WORKFLOWS["terraform-ci.yml"].read_text(encoding="utf-8")
    assert "continue-on-error" not in text and "--soft-fail" not in text
