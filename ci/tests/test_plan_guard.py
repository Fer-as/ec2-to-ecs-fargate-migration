"""Unit tests for ci/plan_guard.py (synthetic `terraform show -json` plans)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_guard  # noqa: E402

DIGEST_IMAGE = "111122223333.dkr.ecr.eu-west-2.amazonaws.com/legacy-api@sha256:" + "a" * 64


def rc(address, *actions, mode="managed"):
    return {"address": address, "mode": mode, "change": {"actions": list(actions)}}


def plan(*changes, weights=None, storage=None, image=DIGEST_IMAGE):
    outputs = {"app_image": {"actions": ["update"], "before": None, "after": image}}
    if weights:
        outputs["traffic_weights"] = {"actions": ["update"], "before": weights[0], "after": weights[1]}
    if storage:
        outputs["legacy_storage_backend"] = {"actions": ["update"], "before": storage[0], "after": storage[1]}
    return {"resource_changes": list(changes), "output_changes": outputs}


def w(stage, ec2, ecs):
    return {"stage": stage, "ec2": ec2, "ecs": ecs}


def run(tmp_path, p, kind):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(p), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = plan_guard.main(["--plan-json", str(f), "--kind", kind, "--summary", str(summary)])
    return code, summary.read_text(encoding="utf-8")


# --- application deployments -------------------------------------------------------

def test_application_release_is_allowed(tmp_path):
    p = plan(rc("aws_ecs_task_definition.app[0]", "delete", "create"),
             rc("aws_ecs_task_definition.seed[0]", "delete", "create"),
             rc("aws_ecs_service.app[0]", "update"),
             rc("aws_s3_object.app", "update"),
             rc("data.aws_ecr_image.app[0]", "read", mode="data"),
             rc("aws_vpc.main", "no-op"))
    code, md = run(tmp_path, p, "application")
    assert code == 0
    assert "allowed - awaiting approval" in md
    assert "sha256:" + "a" * 64 in md


def test_application_cannot_change_traffic_weights(tmp_path):
    p = plan(rc("aws_lb_listener.http", "update"), weights=(w(0, 100, 0), w(1, 90, 10)))
    code, md = run(tmp_path, p, "application")
    assert code == 1
    assert "TRAFFIC WEIGHTS CHANGE" in md and "aws_lb_listener.http" in md


def test_application_cannot_switch_storage_or_replace_ec2(tmp_path):
    p = plan(rc("terraform_data.legacy_storage_mode", "update"),
             rc("aws_instance.app", "delete", "create"),
             storage=("memory", "dynamodb"))
    code, md = run(tmp_path, p, "application")
    assert code == 1
    assert "LEGACY STORAGE MODE CHANGE" in md and "aws_instance.app" in md


def test_application_cannot_change_other_infrastructure(tmp_path):
    p = plan(rc("aws_ecs_service.app[0]", "update"), rc("aws_security_group.ec2", "update"))
    code, md = run(tmp_path, p, "application")
    assert code == 1 and "aws_security_group.ec2" in md


def test_application_cannot_create_or_delete_the_service(tmp_path):
    assert run(tmp_path, plan(rc("aws_ecs_service.app[0]", "create")), "application")[0] == 1
    assert run(tmp_path, plan(rc("aws_ecs_service.app[0]", "delete")), "application")[0] == 1


# --- platform deployments ----------------------------------------------------------

def test_platform_stage_change_is_allowed_but_highlighted(tmp_path):
    p = plan(rc("aws_lb_listener.http", "update"), weights=(w(1, 90, 10), w(2, 50, 50)))
    code, md = run(tmp_path, p, "platform")
    assert code == 0
    assert "TRAFFIC WEIGHTS CHANGE: stage 1 (EC2 90% / ECS 10%) -> stage 2 (EC2 50% / ECS 50%)" in md


def test_platform_highlights_rollback_target_replacement(tmp_path):
    p = plan(rc("aws_instance.app", "delete", "create"), storage=("memory", "dynamodb"))
    code, md = run(tmp_path, p, "platform")
    assert code == 0
    assert "legacy rollback target is rebuilt" in md and "write freeze required" in md


@pytest.mark.parametrize("kind", ["application", "platform"])
@pytest.mark.parametrize("actions", [("delete",), ("delete", "create"), ("create", "delete")])
def test_table_is_never_deleted_or_replaced(tmp_path, kind, actions):
    code, md = run(tmp_path, plan(rc("aws_dynamodb_table.app", *actions)), kind)
    assert code == 1 and "never allowed" in md


def test_no_changes(tmp_path):
    code, md = run(tmp_path, plan(rc("aws_vpc.main", "no-op")), "application")
    assert code == 0 and "no changes" in md


def test_summary_contains_no_attribute_values(tmp_path):
    p = plan(rc("aws_ecs_service.app[0]", "update"))
    p["resource_changes"][0]["change"]["after"] = {"secret_looking_value": "do-not-print-me"}
    _, md = run(tmp_path, p, "application")
    assert "do-not-print-me" not in md


def test_unreadable_plan_is_a_usage_error(tmp_path):
    bad = tmp_path / "plan.json"
    bad.write_text("not json", encoding="utf-8")
    assert plan_guard.main(["--plan-json", str(bad), "--kind", "platform"]) == 2
