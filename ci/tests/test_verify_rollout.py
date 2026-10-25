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
             deployments=1, running=2, desired=2, stable=True, ec2_waits_failing=0):
    calls = []
    ec2_waits = {"failures_left": ec2_waits_failing}

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
            deps = [{"status": "PRIMARY", "rolloutState": rollout}] + [{"status": "ACTIVE"}] * (deployments - 1)
            return {"services": [{"desiredCount": desired, "runningCount": running, "deployments": deps,
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
    return aws


def verifier(aws, http_codes=None, outputs=OUTPUTS):
    codes = http_codes or {}
    return verify_rollout.Verifier(dict(outputs), "eu-west-2", aws=aws,
                                   http=lambda url: codes.get(url.rsplit("/", 1)[-1], 200),
                                   sleep=lambda s: None, http_attempts=2)


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
