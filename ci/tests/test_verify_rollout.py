"""Unit tests for ci/verify_rollout.py with a fake AWS CLI and fake HTTP."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_rollout  # noqa: E402

OUTPUTS = {
    "alb_url": "http://alb.example",
    "ecs_cluster_name": "legacy-api-production",
    "ecs_service_name": "legacy-api-production-app",
    "ecs_target_group_arn": "arn:tg/ecs",
    "ec2_target_group_arn": "arn:tg/ec2",
}


def fake_aws(ecs_states=("healthy", "healthy"), ec2_states=("healthy",), rollout="COMPLETED",
             deployments=1, running=2, desired=2, stable=True, ec2_waits_failing=0,
             rollout_sequence=None, pending_sequence=None, describe_error=None, missing=False,
             rollout_reason=None, invalid=False):
    """rollout_sequence / pending_sequence: values returned by successive describe-services
    calls (the last value repeats)."""
    calls = []
    ec2_waits = {"failures_left": ec2_waits_failing}
    describes = {"n": 0}

    def nth(seq, default):
        if not seq:
            return default
        return seq[min(describes["n"], len(seq) - 1)]

    def aws(args, region, **_):
        calls.append(args)
        if args[:2] == ["elbv2", "wait"]:
            if args[-1] == "arn:tg/ec2" and ec2_waits["failures_left"] > 0:
                ec2_waits["failures_left"] -= 1
                raise RuntimeError("Waiter TargetInService failed: Max attempts exceeded")
            return None
        if args[:2] == ["ecs", "wait"]:
            if not stable:
                raise RuntimeError("Waiter ServicesStable failed: Max attempts exceeded")
            return None
        if args[:2] == ["ecs", "describe-services"]:
            if describe_error:
                raise RuntimeError(describe_error)
            if invalid:
                return None
            if missing:
                return {"services": [], "failures": [{"arn": "arn:svc", "reason": "MISSING"}]}
            state, pending = nth(rollout_sequence, rollout), nth(pending_sequence, 0)
            describes["n"] += 1
            primary = {"status": "PRIMARY", "rolloutState": state}
            if rollout_reason:
                primary["rolloutStateReason"] = rollout_reason
            deps = [primary] + [{"status": "ACTIVE"}] * (deployments - 1)
            return {"services": [{"status": "ACTIVE", "desiredCount": desired, "runningCount": running,
                                  "pendingCount": pending, "deployments": deps,
                                  "events": [{"createdAt": "t", "message": "service event"}]}]}
        if args[:2] == ["elbv2", "describe-target-health"]:
            states = ecs_states if args[-1] == "arn:tg/ecs" else ec2_states
            return {"TargetHealthDescriptions": [
                {"Target": {"Id": f"t{i}"}, "TargetHealth": {"State": s, "Reason": "", "Description": ""}}
                for i, s in enumerate(states)]}
        if args[:2] == ["ecs", "list-tasks"]:
            return {"taskArns": ["arn:task/1"]}
        if args[:2] == ["ecs", "describe-tasks"]:
            return {"tasks": [{"stoppedReason": "Task failed ELB health checks", "containers": []}]}
        raise AssertionError(f"unexpected call {args}")

    aws.calls = calls
    aws.describes = describes
    return aws


def verifier(aws, http_codes=None, outputs=OUTPUTS, sleeps=None, **kw):
    codes = http_codes or {}
    record = sleeps if sleeps is not None else []
    return verify_rollout.Verifier(dict(outputs), "eu-west-2", aws=aws,
                                   http=lambda url: codes.get(url.rsplit("/", 1)[-1], 200),
                                   sleep=record.append, http_attempts=2, **kw)


def test_healthy_deployment_passes(capsys):
    assert verifier(fake_aws()).run() == 0
    assert "Deployment verified." in capsys.readouterr().out


def test_unhealthy_ecs_targets_fail_with_diagnostics(capsys):
    assert verifier(fake_aws(ecs_states=("healthy", "unhealthy"))).run() == 1
    out = capsys.readouterr().out
    assert "ECS targets not healthy" in out and "Task failed ELB health checks" in out


def test_unstable_service_fails():
    v = verifier(fake_aws(stable=False, rollout="IN_PROGRESS", deployments=2))
    assert v.run() == 1
    assert any("did not become stable" in f for f in v.failures)
    assert any("rollout not completed" in f for f in v.failures)


def test_unhealthy_rollback_target_fails():
    v = verifier(fake_aws(ec2_states=("unhealthy",)))
    assert v.run() == 1 and any("rollback target" in f for f in v.failures)


def test_ready_endpoint_failure_fails():
    v = verifier(fake_aws(), http_codes={"ready": 503})
    assert v.run() == 1 and any("/ready" in f and "503" in f for f in v.failures)


def test_running_below_desired_fails():
    v = verifier(fake_aws(running=1, ecs_states=("healthy",)))
    assert v.run() == 1


def test_first_deployment_without_service_checks_ec2_and_http_only():
    outputs = dict(OUTPUTS, ecs_service_name=None)
    aws = fake_aws()
    assert verifier(aws, outputs=outputs).run() == 0
    assert not any(c[0] == "ecs" for c in aws.calls)


def test_missing_outputs_is_a_usage_error(tmp_path):
    f = tmp_path / "out.json"
    f.write_text('{"alb_url": {"value": "http://x"}}', encoding="utf-8")
    assert verify_rollout.main(["--outputs", str(f), "--region", "eu-west-2"]) == 2


def test_waits_for_new_ec2_instance_to_finish_bootstrap():
    # First wait times out (instance still booting), second succeeds -> verified
    aws = fake_aws(ec2_waits_failing=1)
    assert verifier(aws).run() == 0
    waits = [c for c in aws.calls if c[:2] == ["elbv2", "wait"] and c[-1] == "arn:tg/ec2"]
    assert len(waits) == 2


def test_ec2_never_in_service_fails():
    v = verifier(fake_aws(ec2_waits_failing=5, ec2_states=("unhealthy",)))
    assert v.run() == 1 and any("rollback target" in f for f in v.failures)


# --- Task 28: rolloutState lags behind `ecs wait services-stable` ---------------------------
# Run 40556838063: waiter succeeded, 2/2 running, all targets healthy, /health and /ready 200,
# but the single describe-services still said rolloutState=IN_PROGRESS -> false failure.

def describe_calls(aws):
    return [c for c in aws.calls if c[:2] == ["ecs", "describe-services"]]


def test_in_progress_then_completed_passes(capsys):
    sleeps = []
    aws = fake_aws(rollout_sequence=["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
    v = verifier(aws, sleeps=sleeps, rollout_timeout=300, rollout_interval=15)
    assert v.run() == 0, v.failures
    assert aws.describes["n"] == 3
    assert sleeps.count(15) == 2, "polls at the configured interval until COMPLETED"
    assert "Deployment verified." in capsys.readouterr().out


def test_incident_state_now_passes():
    # the incident: one deployment, 2/2 running, targets healthy, first read IN_PROGRESS
    aws = fake_aws(rollout_sequence=["IN_PROGRESS", "COMPLETED"], ecs_states=("healthy", "healthy"))
    assert verifier(aws).run() == 0


def test_permanently_in_progress_times_out_and_fails():
    sleeps = []
    aws = fake_aws(rollout_sequence=["IN_PROGRESS"])
    v = verifier(aws, sleeps=sleeps, rollout_timeout=60, rollout_interval=15)
    v.check_service()  # polling only (run() adds one more describe for diagnostics)
    assert aws.describes["n"] == 4 and sleeps.count(15) == 3, "bounded: timeout / interval checks"
    assert any("rollout not completed" in f and "IN_PROGRESS" in f for f in v.failures)
    assert verifier(fake_aws(rollout_sequence=["IN_PROGRESS"]), rollout_timeout=60, rollout_interval=15).run() == 1


def test_failed_rollout_fails_promptly():
    sleeps = []
    aws = fake_aws(rollout_sequence=["FAILED", "COMPLETED"],
                   rollout_reason="ECS deployment circuit breaker: tasks failed")
    v = verifier(aws, sleeps=sleeps, rollout_timeout=300, rollout_interval=15)
    v.check_service()  # polling only (run() adds one more describe for diagnostics)
    assert aws.describes["n"] == 1 and 15 not in sleeps, "no polling after FAILED"
    assert any("rollout FAILED" in f and "circuit breaker" in f for f in v.failures)
    full = fake_aws(rollout_sequence=["FAILED", "COMPLETED"], rollout_reason="circuit breaker")
    assert verifier(full, rollout_interval=15).run() == 1


def test_missing_service_fails_promptly():
    sleeps = []
    v = verifier(fake_aws(missing=True), sleeps=sleeps, rollout_interval=15)
    assert v.run() == 1 and 15 not in sleeps
    assert any("not found" in f and "MISSING" in f for f in v.failures)


def test_aws_error_fails_promptly():
    sleeps = []
    v = verifier(fake_aws(describe_error="AccessDeniedException: not authorized"), sleeps=sleeps,
                 rollout_interval=15)
    assert v.run() == 1 and 15 not in sleeps
    assert any("AccessDenied" in f for f in v.failures)


def test_invalid_response_fails_promptly():
    v = verifier(fake_aws(invalid=True), rollout_interval=15)
    assert v.run() == 1 and any("invalid describe-services response" in f for f in v.failures)


def test_pending_tasks_block_completion_until_zero():
    aws = fake_aws(rollout_sequence=["COMPLETED"], pending_sequence=[1, 1, 0])
    assert verifier(aws, rollout_interval=15).run() == 0
    assert aws.describes["n"] == 3


def test_pending_tasks_that_never_clear_fail():
    v = verifier(fake_aws(pending_sequence=[1]), rollout_timeout=30, rollout_interval=15)
    assert v.run() == 1 and any("pending tasks" in f for f in v.failures)


def test_completed_rollout_with_unhealthy_targets_still_fails():
    aws = fake_aws(rollout_sequence=["IN_PROGRESS", "COMPLETED"], ecs_states=("healthy", "unhealthy"))
    v = verifier(aws)
    assert v.run() == 1 and any("ECS targets not healthy" in f for f in v.failures)


def test_unhealthy_ec2_rollback_target_still_fails_after_rollout_completes():
    aws = fake_aws(rollout_sequence=["IN_PROGRESS", "COMPLETED"], ec2_states=("unhealthy",))
    v = verifier(aws)
    assert v.run() == 1 and any("rollback target" in f for f in v.failures)


def test_ec2_only_verification_never_polls_ecs():
    sleeps = []
    aws = fake_aws()
    v = verifier(aws, outputs=dict(OUTPUTS, ecs_service_name=None), sleeps=sleeps, rollout_interval=15)
    assert v.run() == 0
    assert not describe_calls(aws) and 15 not in sleeps


def test_cli_passes_rollout_timeout(tmp_path, monkeypatch):
    f = tmp_path / "out.json"
    f.write_text('{"alb_url": {"value": "http://x"}, "ec2_target_group_arn": {"value": "a"},'
                 ' "ecs_target_group_arn": {"value": "b"}, "ecs_cluster_name": {"value": "c"}}', encoding="utf-8")
    seen = {}

    class Probe(verify_rollout.Verifier):
        def run(self):
            seen["attempts"] = self.rollout_attempts
            return 0

    monkeypatch.setattr(verify_rollout, "Verifier", Probe)
    assert verify_rollout.main(["--outputs", str(f), "--region", "eu-west-2", "--rollout-timeout", "120"]) == 0
    assert seen["attempts"] == 8  # 120 s / 15 s
