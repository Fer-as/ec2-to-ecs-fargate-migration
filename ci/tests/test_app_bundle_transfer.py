"""The EC2 app bundle must travel from the plan runner to the apply runner unchanged.

data.archive_file.app writes terraform/app.zip during `terraform plan`; aws_s3_object.app
reads it during `terraform apply`, which runs on a different runner (Task 21 incident:
"open ./app.zip: no such file or directory"). deploy.yml stores the exact file beside the
saved plan and verifies its SHA-256 before apply.

The step scripts are taken verbatim from deploy.yml and executed with bash, with the AWS
CLI replaced by a local fake that copies files ("s3" = a temp directory). Nothing is sent
to AWS.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"))
JOBS = DEPLOY["jobs"]
BASH = shutil.which("bash")

# Fake AWS CLI: `aws s3 cp SRC DST [flags]`, where s3://bucket/key maps to $FAKE_S3/bucket/key
FAKE_AWS = r'''
aws() {
  [ "$1 $2" = "s3 cp" ] || { echo "unexpected aws call: $*" >&2; return 99; }
  local src="$3" dst="$4"
  case "$src" in s3://*) src="$FAKE_S3/${src#s3://}";; esac
  case "$dst" in s3://*) dst="$FAKE_S3/${dst#s3://}"; mkdir -p "$(dirname "$dst")";; esac
  [ -f "$src" ] || { echo "fatal error: An error occurred (404) when calling the HeadObject operation" >&2; return 1; }
  cp "$src" "$dst"
}
'''


def step(job, name):
    return next(s for s in JOBS[job]["steps"] if s.get("name") == name)


STORE = step("plan", "Store the saved plan privately (S3, encrypted, short retention)")
FETCH = step("apply", "Fetch the planned EC2 app bundle and verify its checksum")


def run_bash(script, cwd, env):
    full_env = {**os.environ, **env}
    return subprocess.run([BASH, "-e", "-o", "pipefail", "-c", FAKE_AWS + script], cwd=cwd, env=full_env,
                          capture_output=True, text=True, timeout=60)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def runner(tmp_path):
    """A fake plan runner workspace plus a fake S3 and a fake apply runner workspace."""
    plan_ws, apply_ws, s3, tmp = (tmp_path / n for n in ("plan", "apply", "s3", "runner_temp"))
    for d in (plan_ws / "terraform", apply_ws / "terraform", s3, tmp):
        d.mkdir(parents=True)
    (plan_ws / "terraform" / "tfplan").write_bytes(b"saved plan bytes")
    (tmp / "plan.log").write_text("plan text\n")
    outputs = tmp / "github_output"
    env = {"FAKE_S3": str(s3), "RUNNER_TEMP": str(tmp), "GITHUB_OUTPUT": str(outputs),
           "GITHUB_STEP_SUMMARY": str(tmp / "summary"), "TF_PLAN_PREFIX": "plans/legacy-api",
           "TF_STATE_BUCKET": "state-bucket", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
    return plan_ws, apply_ws, s3, outputs, env


# --- static wiring ------------------------------------------------------------------------------

def test_plan_job_exports_the_bundle_checksum():
    assert JOBS["plan"]["outputs"]["app_zip_sha256"] == "${{ steps.store.outputs.app_zip_sha256 }}"
    run = STORE["run"]
    assert 'aws s3 cp terraform/app.zip "s3://${TF_STATE_BUCKET}/${key%/tfplan}/app.zip" --sse AES256' in run
    assert "-s terraform/app.zip" in run, "missing bundle must fail the plan job"


def test_apply_job_verifies_the_bundle_between_plan_checksum_and_apply():
    apply = JOBS["apply"]
    assert apply["env"]["APP_ZIP_SHA256"] == "${{ needs.plan.outputs.app_zip_sha256 }}"
    names = [s.get("name", "") for s in apply["steps"]]
    plan_check = names.index("Fetch the approved plan and verify its checksum")
    bundle_check = names.index("Fetch the planned EC2 app bundle and verify its checksum")
    do_apply = names.index("terraform apply EXACTLY the approved saved plan")
    assert plan_check < bundle_check < do_apply
    assert 'sha256sum -c' in FETCH["run"]


def test_apply_job_never_rebuilds_the_bundle_or_publishes_it():
    for s in JOBS["apply"]["steps"]:
        run = s.get("run", "")
        assert "terraform -chdir=terraform plan" not in run
        assert not re.search(r"(^|[;&|]\s*)\s*zip\s", run, re.M), "apply must not build a new zip"
        assert "upload-artifact" not in s.get("uses", "")
    for job in JOBS.values():
        for s in job.get("steps", []):
            assert "upload-artifact" not in s.get("uses", ""), "plans and bundles never go to public artifacts"


# --- executed step scripts (bash + fake AWS) ------------------------------------------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytestmark_bash
def test_bundle_round_trip_verifies(runner):
    plan_ws, apply_ws, s3, outputs, env = runner
    (plan_ws / "terraform" / "app.zip").write_bytes(b"PK\x03\x04 exact planned bundle")
    r = run_bash(STORE["run"], plan_ws, env)
    assert r.returncode == 0, r.stderr
    out = dict(line.split("=", 1) for line in outputs.read_text().split())
    assert out["app_zip_sha256"] == sha(plan_ws / "terraform" / "app.zip")
    stored = s3 / "state-bucket" / "plans" / "legacy-api" / "123-1" / "app.zip"
    assert stored.read_bytes() == (plan_ws / "terraform" / "app.zip").read_bytes()

    r = run_bash(FETCH["run"], apply_ws, {**env, "PLAN_KEY": out["plan_key"], "APP_ZIP_SHA256": out["app_zip_sha256"]})
    assert r.returncode == 0, r.stderr
    assert (apply_ws / "terraform" / "app.zip").read_bytes() == stored.read_bytes()


@pytestmark_bash
def test_plan_job_fails_closed_without_bundle(runner):
    plan_ws, _, s3, _, env = runner
    r = run_bash(STORE["run"], plan_ws, env)
    assert r.returncode != 0 and "app.zip was not produced" in r.stdout
    assert not any(s3.rglob("*")), "nothing uploaded when the bundle is missing"


@pytestmark_bash
def test_apply_fails_on_tampered_bundle(runner):
    plan_ws, apply_ws, s3, outputs, env = runner
    (plan_ws / "terraform" / "app.zip").write_bytes(b"PK original")
    assert run_bash(STORE["run"], plan_ws, env).returncode == 0
    out = dict(line.split("=", 1) for line in outputs.read_text().split())
    (s3 / "state-bucket" / "plans" / "legacy-api" / "123-1" / "app.zip").write_bytes(b"PK tampered")
    r = run_bash(FETCH["run"], apply_ws, {**env, "PLAN_KEY": out["plan_key"], "APP_ZIP_SHA256": out["app_zip_sha256"]})
    assert r.returncode != 0 and "FAILED" in r.stdout + r.stderr


@pytestmark_bash
def test_apply_fails_when_bundle_missing_in_s3(runner):
    _, apply_ws, _, _, env = runner
    r = run_bash(FETCH["run"], apply_ws, {**env, "PLAN_KEY": "plans/legacy-api/123-1/tfplan", "APP_ZIP_SHA256": "a" * 64})
    assert r.returncode != 0
    assert not (apply_ws / "terraform" / "app.zip").exists()


@pytestmark_bash
@pytest.mark.parametrize("checksum", ["", "not-a-sha", "A" * 64])
def test_apply_fails_without_a_valid_checksum(runner, checksum):
    _, apply_ws, _, _, env = runner
    r = run_bash(FETCH["run"], apply_ws, {**env, "PLAN_KEY": "plans/legacy-api/123-1/tfplan", "APP_ZIP_SHA256": checksum})
    assert r.returncode != 0 and "no app bundle checksum" in r.stdout
