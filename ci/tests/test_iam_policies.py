"""Offline tests for the GitHub Actions role policies (terraform/bootstrap/policies).

The templates are rendered with example values and evaluated by a small IAM evaluator
(explicit Deny > Allow > implicit deny; the condition operators these policies use).
This is a model of IAM, not AWS itself: it checks the intent of each statement. AWS
behaviour is checked separately with `aws iam simulate-custom-policy` (see
docs/iam-permission-matrix.md); neither is a substitute for a real apply.
"""

import fnmatch
import json
from pathlib import Path
from string import Template

import pytest

POLICY_DIR = Path(__file__).resolve().parents[2] / "terraform" / "bootstrap" / "policies"
ACCOUNT, REGION, PROJECT = "111122223333", "eu-west-2", "legacy-api"
VARS = {
    "partition": "aws", "region": REGION, "account_id": ACCOUNT, "project": PROJECT,
    "table_arn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/legacy-api-production",
    "state_bucket_arn": f"arn:aws:s3:::legacy-api-tfstate-{ACCOUNT}-{REGION}",
    "ecr_repository_arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/legacy-api",
    "platform_state_key": "legacy-api/platform.tfstate", "plans_prefix": "plans/legacy-api",
    "legacy_instance_type": "t3.micro", "ami_owner": "099720109477",
}
EC2 = f"arn:aws:ec2:{REGION}:{ACCOUNT}"
ELB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}"
ECS = f"arn:aws:ecs:{REGION}:{ACCOUNT}"
IAM = f"arn:aws:iam::{ACCOUNT}"
STATE = VARS["state_bucket_arn"]


def load(name):
    text = (POLICY_DIR / f"{name}.json.tftpl").read_text(encoding="utf-8")
    return json.loads(Template(text).substitute(VARS))


ROLES = {
    "apply": [load("platform-read"), load("apply-network"), load("apply-services"), load("apply-core"),
              load("plan-store-read")],
    "plan": [load("platform-read"), load("plan-store-write")],
    "image_push": [load("image-push")],
}


# --- minimal IAM evaluator -----------------------------------------------------------------

def _list(v):
    return v if isinstance(v, list) else [v]


def _like(value, pattern, case=True):
    if not case:
        value, pattern = value.lower(), pattern.lower()
    return fnmatch.fnmatchcase(value, pattern)


def _match_op(op, ctx_values, policy_values):
    if op in ("StringEquals", "ArnEquals"):
        return any(c == p for c in ctx_values for p in policy_values)
    if op in ("StringLike", "ArnLike"):
        return any(_like(c, p) for c in ctx_values for p in policy_values)
    if op == "StringNotEquals":
        return not any(c == p for c in ctx_values for p in policy_values)
    raise NotImplementedError(op)


def _condition_holds(cond, ctx):
    for raw_op, keys in cond.items():
        for key, pvals in keys.items():
            pvals = [str(v).lower() if isinstance(v, bool) else v for v in _list(pvals)]
            present = key in ctx
            vals = _list(ctx.get(key, []))
            if raw_op == "Null":
                if (pvals[0] == "true") != (not present):
                    return False
                continue
            if raw_op.startswith("ForAllValues:"):
                op = raw_op.split(":", 1)[1]
                if not all(_match_op(op, [v], pvals) for v in vals):  # empty set -> true
                    return False
                continue
            op = raw_op
            if op.endswith("IfExists"):
                if not present:
                    continue
                op = op[: -len("IfExists")]
            if not present:
                if op == "StringNotEquals":  # negated operators match a missing key
                    continue
                return False
            if not _match_op(op, vals, pvals):
                return False
    return True


def _applies(stmt, action, resource, ctx):
    if not any(_like(action, a, case=False) for a in _list(stmt["Action"])):
        return False
    if not any(_like(resource, r) for r in _list(stmt["Resource"])):
        return False
    return _condition_holds(stmt.get("Condition", {}), ctx)


def decide(role, action, resource, **ctx):
    ctx.setdefault("aws:RequestedRegion", REGION)
    stmts = [s for p in ROLES[role] for s in p["Statement"]]
    matched = [s for s in stmts if _applies(s, action, resource, ctx)]
    if any(s["Effect"] == "Deny" for s in matched):
        return "explicitDeny"
    return "allowed" if any(s["Effect"] == "Allow" for s in matched) else "implicitDeny"


TAG = {"aws:RequestTag/Project": PROJECT}
OWNED = {"aws:ResourceTag/Project": PROJECT}
OTHER = {"aws:ResourceTag/Project": "other-project"}


def allowed(role, action, resource, **ctx):
    return decide(role, action, resource, **ctx) == "allowed"


# --- structure ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["platform-read", "apply-network", "apply-services", "apply-core",
                                  "plan-store-write", "plan-store-read", "image-push"])
def test_templates_render_with_unique_sids(name):
    sids = [s["Sid"] for s in load(name)["Statement"]]
    assert len(sids) == len(set(sids))


def test_sids_are_unique_across_each_role():
    for role, policies in ROLES.items():
        sids = [s["Sid"] for p in policies for s in p["Statement"]]
        assert len(sids) == len(set(sids)), role


def test_resource_star_allows_are_only_the_documented_ones():
    star = {s["Sid"] for p in ROLES["apply"] + ROLES["image_push"] for s in p["Statement"]
            if s["Effect"] == "Allow" and "*" in _list(s["Resource"])}
    assert star == {"DescribePlatformServices", "EcsDeregisterTaskDefinitionNoResourceLevel",
                    "ServiceLinkedRoles", "EcrLogin"}


def test_no_service_wide_allows():
    for p in ROLES["apply"]:
        for s in p["Statement"]:
            if s["Effect"] == "Allow":
                for a in _list(s["Action"]):
                    assert a not in {"ec2:*", "ecs:*", "elasticloadbalancing:*", "application-autoscaling:*",
                                     "cloudwatch:*", "logs:*", "sns:*", "iam:*"}, (s["Sid"], a)


# --- the legacy-api platform can still be managed ------------------------------------------------

@pytest.mark.parametrize("action,resource,ctx", [
    ("ec2:CreateVpc", f"{EC2}:vpc/*", TAG),
    ("ec2:CreateSubnet", f"{EC2}:subnet/*", TAG),
    ("ec2:CreateSubnet", f"{EC2}:vpc/vpc-1", OWNED),
    ("ec2:CreateSecurityGroup", f"{EC2}:security-group/*", TAG),
    ("ec2:AuthorizeSecurityGroupIngress", f"{EC2}:security-group/sg-1", OWNED),
    ("ec2:AuthorizeSecurityGroupIngress", f"{EC2}:security-group-rule/*", {}),
    ("ec2:CreateNatGateway", f"{EC2}:natgateway/*", TAG),
    ("ec2:CreateNatGateway", f"{EC2}:elastic-ip/eipalloc-1", OWNED),
    ("ec2:CreateVpcEndpoint", f"{EC2}:route-table/rtb-1", OWNED),
    ("ec2:CreateFlowLogs", f"{EC2}:vpc-flow-log/*", TAG),
    ("ec2:RunInstances", f"{EC2}:instance/*",
     dict(TAG, **{"ec2:InstanceType": "t3.micro", "ec2:MetadataHttpTokens": "required"})),
    ("ec2:RunInstances", f"arn:aws:ec2:{REGION}::image/ami-1", {"ec2:Owner": "099720109477"}),
    ("ec2:RunInstances", f"{EC2}:subnet/subnet-1", OWNED),
    ("ec2:RunInstances", f"{EC2}:volume/*", {}),
    ("ec2:CreateTags", f"{EC2}:instance/*", {"ec2:CreateAction": "RunInstances", **TAG}),
    ("ec2:TerminateInstances", f"{EC2}:instance/i-1", OWNED),
    ("ec2:DeleteTags", f"{EC2}:instance/i-1", dict(OWNED, **{"aws:TagKeys": ["Name"]})),
    ("elasticloadbalancing:CreateLoadBalancer", f"{ELB}:loadbalancer/app/legacy-api-production/*", TAG),
    ("elasticloadbalancing:AddTags", f"{ELB}:targetgroup/legacy-api-production-ecs/abc", {}),
    ("elasticloadbalancing:ModifyListener", f"{ELB}:listener/app/legacy-api-production/abc/def", {}),
    ("elasticloadbalancing:CreateRule", f"{ELB}:listener/app/legacy-api-production/abc/def", {}),
    ("ecs:CreateCluster", f"{ECS}:cluster/legacy-api-production", TAG),
    ("ecs:RegisterTaskDefinition", f"{ECS}:task-definition/legacy-api-production-app:7", TAG),
    ("ecs:UpdateService", f"{ECS}:service/legacy-api-production/legacy-api-production-app", {}),
    ("ecs:DeregisterTaskDefinition", "*", {}),
    ("application-autoscaling:RegisterScalableTarget", f"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/x",
     {"application-autoscaling:service-namespace": "ecs",
      "application-autoscaling:scalable-dimension": "ecs:service:DesiredCount"}),
    ("cloudwatch:PutMetricAlarm", f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:legacy-api-production-alb-5xx",
     {"cloudwatch:AlarmActions": [f"arn:aws:sns:{REGION}:{ACCOUNT}:legacy-api-production-alarms"]}),
    ("cloudwatch:PutMetricAlarm",
     f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:TargetTracking-service/legacy-api-production/legacy-api-production-app-AlarmHigh-1",
     {"cloudwatch:AlarmActions": [f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:scalingPolicy:1:resource/ecs/service/x"]}),
    ("cloudwatch:PutDashboard", f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/legacy-api-production-migration", {}),
    ("logs:CreateLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/ecs/legacy-api-production", TAG),
    ("logs:PutRetentionPolicy", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/vpc/legacy-api-production/flow-logs:*", {}),
    ("sns:CreateTopic", f"arn:aws:sns:{REGION}:{ACCOUNT}:legacy-api-production-alarms", TAG),
    ("sns:Subscribe", f"arn:aws:sns:{REGION}:{ACCOUNT}:legacy-api-production-alarms", {"sns:Protocol": "email"}),
    ("dynamodb:UpdateTable", VARS["table_arn"], {}),
    ("s3:PutBucketPolicy", "arn:aws:s3:::legacy-api-app-123", {}),
    ("s3:PutObject", f"{STATE}/legacy-api/platform.tfstate", {}),
    ("s3:GetObject", f"{STATE}/plans/legacy-api/run-1.tfplan", {}),
    ("iam:CreateRole", f"{IAM}:role/legacy-api-production-ecs-task", {}),
    ("iam:PassRole", f"{IAM}:role/legacy-api-production-ecs-task", {"iam:PassedToService": "ecs-tasks.amazonaws.com"}),
    ("iam:CreateServiceLinkedRole", "*", {"iam:AWSServiceName": "ecs.amazonaws.com"}),
])
def test_apply_role_can_manage_legacy_api(action, resource, ctx):
    assert allowed("apply", action, resource, **ctx)


# --- other projects and unrelated resources are out of reach ---------------------------------------

@pytest.mark.parametrize("action,resource,ctx", [
    ("ec2:CreateVpc", f"{EC2}:vpc/*", {}),                                   # untagged create
    ("ec2:CreateVpc", f"{EC2}:vpc/*", {"aws:RequestTag/Project": "other"}),
    ("ec2:TerminateInstances", f"{EC2}:instance/i-other", OTHER),
    ("ec2:TerminateInstances", f"{EC2}:instance/i-untagged", {}),
    ("ec2:DeleteVpc", f"{EC2}:vpc/vpc-default", {}),
    ("ec2:CreateSubnet", f"{EC2}:vpc/vpc-other", OTHER),
    ("ec2:AuthorizeSecurityGroupIngress", f"{EC2}:security-group/sg-other", OTHER),
    ("ec2:DeleteSecurityGroup", f"{EC2}:security-group/sg-untagged", {}),
    ("ec2:CreateTags", f"{EC2}:instance/i-other", dict(OTHER, **TAG)),      # cannot claim others' resources
    ("ec2:CreateTags", f"{EC2}:instance/i-1", dict(OWNED, **{"aws:RequestTag/Project": "other"})),
    ("ec2:DeleteTags", f"{EC2}:instance/i-1", dict(OWNED, **{"aws:TagKeys": ["Project"]})),
    ("ec2:DeleteTags", f"{EC2}:instance/i-1", OWNED),                       # no keys = delete all tags
    ("ec2:RunInstances", f"{EC2}:instance/*",
     dict(TAG, **{"ec2:InstanceType": "m5.24xlarge", "ec2:MetadataHttpTokens": "required"})),
    ("ec2:RunInstances", f"{EC2}:instance/*",
     dict(TAG, **{"ec2:InstanceType": "t3.micro", "ec2:MetadataHttpTokens": "optional"})),
    ("ec2:RunInstances", f"arn:aws:ec2:{REGION}::image/ami-x", {"ec2:Owner": "123456789012"}),
    ("ec2:RunInstances", f"{EC2}:key-pair/anything", {}),
    ("ec2:ModifyInstanceAttribute", f"{EC2}:instance/i-1", dict(OWNED, **{"ec2:Attribute/InstanceType": "p5.48xlarge"})),
    ("ec2:DeleteFlowLogs", f"{EC2}:vpc-flow-log/fl-other", OTHER),
    ("elasticloadbalancing:DeleteLoadBalancer", f"{ELB}:loadbalancer/app/other-app/abc", {}),
    ("elasticloadbalancing:CreateLoadBalancer", f"{ELB}:loadbalancer/app/legacy-api-production/*", {}),
    ("ecs:DeleteService", f"{ECS}:service/other-cluster/other-svc", {}),
    ("ecs:RunTask", f"{ECS}:task-definition/legacy-api-production-app:1", {}),
    ("ecs:ExecuteCommand", f"{ECS}:cluster/legacy-api-production", {}),
    ("application-autoscaling:RegisterScalableTarget", f"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/x",
     {"application-autoscaling:service-namespace": "dynamodb",
      "application-autoscaling:scalable-dimension": "dynamodb:table:ReadCapacityUnits"}),
    ("cloudwatch:DeleteAlarms", f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:other-alarm", {}),
    ("cloudwatch:PutMetricAlarm", f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:legacy-api-x",
     {"cloudwatch:AlarmActions": [f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:x"]}),
    ("logs:DeleteLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/other", {}),
    ("sns:DeleteTopic", f"arn:aws:sns:{REGION}:{ACCOUNT}:other-topic", {}),
    ("sns:Subscribe", f"arn:aws:sns:{REGION}:{ACCOUNT}:legacy-api-production-alarms", {"sns:Protocol": "https"}),
    ("sns:Publish", f"arn:aws:sns:{REGION}:{ACCOUNT}:legacy-api-production-alarms", {}),
    ("dynamodb:DeleteTable", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/other", {}),
    ("s3:DeleteBucket", "arn:aws:s3:::other-bucket", {}),
    ("iam:CreateRole", f"{IAM}:role/other-role", {}),
    ("iam:AttachRolePolicy", f"{IAM}:role/legacy-api-production-ecs-task", {}),
    ("iam:PassRole", f"{IAM}:role/legacy-api-production-ecs-task", {"iam:PassedToService": "lambda.amazonaws.com"}),
    ("iam:CreateServiceLinkedRole", "*", {"iam:AWSServiceName": "rds.amazonaws.com"}),
    ("iam:CreateUser", f"{IAM}:user/legacy-api-x", {}),
])
def test_apply_role_cannot_touch_unrelated_resources(action, resource, ctx):
    assert decide("apply", action, resource, **ctx) != "allowed"


def test_other_regions_are_denied():
    ctx = dict(TAG, **{"aws:RequestedRegion": "us-east-1"})
    assert decide("apply", "ec2:CreateVpc", f"{EC2}:vpc/*", **ctx) == "explicitDeny"


@pytest.mark.parametrize("action,resource", [
    ("ec2:ModifySnapshotAttribute", f"arn:aws:ec2:{REGION}::snapshot/snap-1"),
    ("ec2:ModifyImageAttribute", f"arn:aws:ec2:{REGION}::image/ami-1"),
    ("ec2:CreateKeyPair", f"{EC2}:key-pair/k"),
    ("ec2:ImportKeyPair", f"{EC2}:key-pair/k"),
    ("ec2:CreateVpcPeeringConnection", f"{EC2}:vpc-peering-connection/*"),
    ("ec2:AcceptVpcPeeringConnection", f"{EC2}:vpc-peering-connection/pcx-1"),
    ("ec2:CreateTransitGatewayVpcAttachment", f"{EC2}:transit-gateway-attachment/*"),
    ("ec2:CreateVpnConnection", f"{EC2}:vpn-connection/*"),
    ("ec2:CreateSnapshot", f"arn:aws:ec2:{REGION}::snapshot/*"),
    ("ec2:GetPasswordData", f"{EC2}:instance/i-1"),
])
def test_sharing_key_pairs_and_unrelated_networking_are_explicitly_denied(action, resource):
    # Denied even on a project-tagged resource
    assert decide("apply", action, resource, **OWNED) == "explicitDeny"


@pytest.mark.parametrize("action", [
    "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:DeleteRole", "iam:AttachRolePolicy", "iam:PassRole",
])
@pytest.mark.parametrize("role", ["gha-legacy-api-apply", "gha-legacy-api-plan", "gha-other-project-deploy"])
def test_apply_role_cannot_change_pipeline_roles(action, role):
    assert decide("apply", action, f"{IAM}:role/{role}") == "explicitDeny"


def test_apply_role_cannot_modify_its_managed_policies():
    for action in ["iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"]:
        assert decide("apply", action, f"{IAM}:policy/gha-legacy-api-apply-core") == "explicitDeny"
    assert decide("apply", "iam:DetachRolePolicy", f"{IAM}:role/gha-legacy-api-apply") == "explicitDeny"


@pytest.mark.parametrize("action", [
    "iam:UpdateOpenIDConnectProviderThumbprint", "iam:AddClientIDToOpenIDConnectProvider",
    "iam:DeleteOpenIDConnectProvider", "iam:TagOpenIDConnectProvider",
])
def test_apply_role_cannot_modify_oidc_provider(action):
    arn = f"{IAM}:oidc-provider/token.actions.githubusercontent.com"
    assert decide("apply", action, arn) == "explicitDeny"


@pytest.mark.parametrize("action", ["s3:DeleteBucket", "s3:PutBucketPolicy", "s3:PutBucketVersioning",
                                    "s3:PutLifecycleConfiguration"])
def test_bootstrap_state_bucket_is_protected(action):
    assert decide("apply", action, STATE) == "explicitDeny"


def test_apply_role_cannot_overwrite_other_state_or_saved_plans():
    assert not allowed("apply", "s3:PutObject", f"{STATE}/other-project/platform.tfstate")
    assert not allowed("apply", "s3:PutObject", f"{STATE}/plans/legacy-api/run-1.tfplan")
    assert not allowed("apply", "s3:DeleteObject", f"{STATE}/legacy-api/platform.tfstate")


# --- plan and image-push roles stay narrower --------------------------------------------------------

@pytest.mark.parametrize("action,resource", [
    ("ec2:RunInstances", f"{EC2}:instance/*"),
    ("ec2:CreateVpc", f"{EC2}:vpc/*"),
    ("ecs:UpdateService", f"{ECS}:service/legacy-api-production/legacy-api-production-app"),
    ("iam:CreateRole", f"{IAM}:role/legacy-api-x"),
    ("iam:PassRole", f"{IAM}:role/legacy-api-x"),
    ("dynamodb:DeleteTable", VARS["table_arn"]),
    ("s3:PutObject", f"{STATE}/legacy-api/platform.tfstate"),
    ("s3:GetObject", f"{STATE}/plans/legacy-api/run-1.tfplan"),
    ("ecr:PutImage", VARS["ecr_repository_arn"]),
])
def test_plan_role_cannot_write_platform_or_state(action, resource):
    assert not allowed("plan", action, resource, **TAG)


def test_plan_role_can_read_and_store_plans():
    assert allowed("plan", "ec2:DescribeVpcs", "*")
    assert allowed("plan", "s3:GetObject", f"{STATE}/legacy-api/platform.tfstate")
    assert allowed("plan", "s3:PutObject", f"{STATE}/legacy-api/platform.tfstate.tflock")
    assert allowed("plan", "s3:PutObject", f"{STATE}/plans/legacy-api/run-1.tfplan")
    assert not allowed("plan", "s3:PutObject", f"{STATE}/plans/other/run-1.tfplan")


def test_image_push_role_only_pushes_to_its_repository():
    assert allowed("image_push", "ecr:GetAuthorizationToken", "*")
    assert allowed("image_push", "ecr:PutImage", VARS["ecr_repository_arn"])
    assert not allowed("image_push", "ecr:PutImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other")
    assert not allowed("image_push", "ecr:DeleteRepository", VARS["ecr_repository_arn"])
    assert not allowed("image_push", "ecr:SetRepositoryPolicy", VARS["ecr_repository_arn"])
    assert not allowed("image_push", "ec2:DescribeVpcs", "*")


# --- Task 12: realistic request contexts (exact decisions; also run through
# `aws iam simulate-custom-policy`, see docs/iam-permission-matrix.md section 7) ----------------

GLOBAL = {"aws:RequestedRegion": "us-east-1"}  # IAM is global; its endpoint signs as us-east-1
US = f"arn:aws:ec2:us-east-1:{ACCOUNT}"
OIDC = f"{IAM}:oidc-provider/token.actions.githubusercontent.com"
RUN_OK = dict(TAG, **{"ec2:InstanceType": "t3.micro", "ec2:MetadataHttpTokens": "required"})

REALISTIC = [
    # IAM through the global endpoint (us-east-1)
    ("iam create project role", "iam:CreateRole", f"{IAM}:role/legacy-api-production-ecs-task", GLOBAL, "allowed"),
    ("iam put project role policy", "iam:PutRolePolicy", f"{IAM}:role/legacy-api-production-ecs-task", GLOBAL, "allowed"),
    ("iam create instance profile", "iam:CreateInstanceProfile", f"{IAM}:instance-profile/legacy-api-ec2-abc", GLOBAL, "allowed"),
    ("iam add role to profile", "iam:AddRoleToInstanceProfile", f"{IAM}:instance-profile/legacy-api-ec2-abc", GLOBAL, "allowed"),
    ("iam pass role to ecs-tasks", "iam:PassRole", f"{IAM}:role/legacy-api-production-ecs-execution",
     dict(GLOBAL, **{"iam:PassedToService": "ecs-tasks.amazonaws.com"}), "allowed"),
    ("iam pass role to ec2", "iam:PassRole", f"{IAM}:role/legacy-api-ec2-abc",
     dict(GLOBAL, **{"iam:PassedToService": "ec2.amazonaws.com"}), "allowed"),
    ("iam create ECS service-linked role", "iam:CreateServiceLinkedRole",
     f"{IAM}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS",
     dict(GLOBAL, **{"iam:AWSServiceName": "ecs.amazonaws.com"}), "allowed"),
    ("iam create RDS service-linked role", "iam:CreateServiceLinkedRole",
     f"{IAM}:role/aws-service-role/rds.amazonaws.com/AWSServiceRoleForRDS",
     dict(GLOBAL, **{"iam:AWSServiceName": "rds.amazonaws.com"}), "implicitDeny"),
    ("iam create unrelated role", "iam:CreateRole", f"{IAM}:role/other-project-role", GLOBAL, "implicitDeny"),
    ("iam pass unrelated role", "iam:PassRole", f"{IAM}:role/other-project-role",
     dict(GLOBAL, **{"iam:PassedToService": "ecs-tasks.amazonaws.com"}), "implicitDeny"),
    ("iam pass project role to lambda", "iam:PassRole", f"{IAM}:role/legacy-api-production-ecs-task",
     dict(GLOBAL, **{"iam:PassedToService": "lambda.amazonaws.com"}), "implicitDeny"),
    ("iam change apply role trust", "iam:UpdateAssumeRolePolicy", f"{IAM}:role/gha-legacy-api-apply", GLOBAL, "explicitDeny"),
    ("iam change other project's gha role", "iam:PutRolePolicy", f"{IAM}:role/gha-other-project-deploy", GLOBAL, "explicitDeny"),
    ("oidc thumbprint change", "iam:UpdateOpenIDConnectProviderThumbprint", OIDC, GLOBAL, "explicitDeny"),
    ("oidc add client id", "iam:AddClientIDToOpenIDConnectProvider", OIDC, GLOBAL, "explicitDeny"),
    ("oidc delete", "iam:DeleteOpenIDConnectProvider", OIDC, GLOBAL, "explicitDeny"),
    ("oidc create", "iam:CreateOpenIDConnectProvider", OIDC, GLOBAL, "explicitDeny"),
    ("new version of apply managed policy", "iam:CreatePolicyVersion", f"{IAM}:policy/gha-legacy-api-apply-core", GLOBAL,
     "explicitDeny"),
    ("detach write policy from apply role", "iam:DetachRolePolicy", f"{IAM}:role/gha-legacy-api-apply", GLOBAL,
     "explicitDeny"),
    ("attach admin policy to project role", "iam:AttachRolePolicy", f"{IAM}:role/legacy-api-production-ecs-task",
     dict(GLOBAL, **{"iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess"}), "implicitDeny"),
    # EC2 / ECS in eu-west-2
    ("ec2 create vpc tagged", "ec2:CreateVpc", f"{EC2}:vpc/*", TAG, "allowed"),
    ("ec2 create vpc without tag", "ec2:CreateVpc", f"{EC2}:vpc/*", {}, "implicitDeny"),
    ("ec2 create vpc other tag", "ec2:CreateVpc", f"{EC2}:vpc/*", {"aws:RequestTag/Project": "other"}, "implicitDeny"),
    ("ec2 run t3.micro tagged", "ec2:RunInstances", f"{EC2}:instance/*", RUN_OK, "allowed"),
    ("ec2 run without tag", "ec2:RunInstances", f"{EC2}:instance/*",
     {"ec2:InstanceType": "t3.micro", "ec2:MetadataHttpTokens": "required"}, "implicitDeny"),
    ("ec2 run into untagged (default) SG", "ec2:RunInstances", f"{EC2}:security-group/sg-default", {}, "implicitDeny"),
    ("ec2 terminate project instance", "ec2:TerminateInstances", f"{EC2}:instance/i-1", OWNED, "allowed"),
    ("ec2 terminate untagged instance", "ec2:TerminateInstances", f"{EC2}:instance/i-2", {}, "implicitDeny"),
    ("ec2 terminate other project's instance", "ec2:TerminateInstances", f"{EC2}:instance/i-3", OTHER, "implicitDeny"),
    ("sg revoke on project SG", "ec2:RevokeSecurityGroupIngress", f"{EC2}:security-group/sg-1", OWNED, "allowed"),
    ("sg revoke on untagged default SG", "ec2:RevokeSecurityGroupIngress", f"{EC2}:security-group/sg-default", {}, "implicitDeny"),
    ("sg revoke egress on untagged default SG", "ec2:RevokeSecurityGroupEgress", f"{EC2}:security-group/sg-default", {}, "implicitDeny"),
    ("sg authorize on untagged default SG", "ec2:AuthorizeSecurityGroupIngress", f"{EC2}:security-group/sg-default", {}, "implicitDeny"),
    ("sg tag untagged default SG", "ec2:CreateTags", f"{EC2}:security-group/sg-default", TAG, "implicitDeny"),
    ("sg revoke on other project's SG", "ec2:RevokeSecurityGroupIngress", f"{EC2}:security-group/sg-9", OTHER, "implicitDeny"),
    ("ecs create cluster tagged", "ecs:CreateCluster", f"{ECS}:cluster/legacy-api-production", TAG, "allowed"),
    ("ecs create cluster without tag", "ecs:CreateCluster", f"{ECS}:cluster/legacy-api-production", {}, "implicitDeny"),
    ("ecs update project service", "ecs:UpdateService", f"{ECS}:service/legacy-api-production/legacy-api-production-app", {}, "allowed"),
    ("ecs update other service", "ecs:UpdateService", f"{ECS}:service/other/other-app", {}, "implicitDeny"),
    # EC2 / ECS outside eu-west-2
    ("ec2 create vpc tagged in us-east-1", "ec2:CreateVpc", f"{US}:vpc/*", dict(TAG, **GLOBAL), "explicitDeny"),
    ("ec2 terminate project instance in eu-central-1", "ec2:TerminateInstances",
     f"arn:aws:ec2:eu-central-1:{ACCOUNT}:instance/i-1", dict(OWNED, **{"aws:RequestedRegion": "eu-central-1"}), "explicitDeny"),
    ("ecs create cluster in us-east-1", "ecs:CreateCluster",
     f"arn:aws:ecs:us-east-1:{ACCOUNT}:cluster/legacy-api-production", dict(TAG, **GLOBAL), "explicitDeny"),
    # Prohibited even on project resources
    ("create key pair", "ec2:CreateKeyPair", f"{EC2}:key-pair/k", TAG, "explicitDeny"),
    ("import key pair", "ec2:ImportKeyPair", f"{EC2}:key-pair/k", TAG, "explicitDeny"),
    ("run with key pair", "ec2:RunInstances", f"{EC2}:key-pair/k", {}, "implicitDeny"),
    ("share snapshot", "ec2:ModifySnapshotAttribute", f"arn:aws:ec2:{REGION}::snapshot/snap-1", OWNED, "explicitDeny"),
    ("share AMI", "ec2:ModifyImageAttribute", f"arn:aws:ec2:{REGION}::image/ami-1", OWNED, "explicitDeny"),
    # Task 22: AWS evaluates ec2:Owner as the image's owner ALIAS. Canonical's Ubuntu AMIs
    # (OwnerId 099720109477) carry the alias "amazon"; run 40538277000 was denied because the
    # policy accepted only the account ID. Context below is the decoded real request.
    ("Canonical Ubuntu AMI as AWS evaluates it (owner alias amazon)", "ec2:RunInstances",
     f"arn:aws:ec2:{REGION}::image/ami-057b46c043e81dcf4", {"ec2:Owner": "amazon"}, "allowed"),
    ("Canonical root snapshot (owner alias amazon)", "ec2:RunInstances",
     f"arn:aws:ec2:{REGION}::snapshot/snap-1", {"ec2:Owner": "amazon"}, "allowed"),
    ("AMI owned by another account", "ec2:RunInstances",
     f"arn:aws:ec2:{REGION}::image/ami-other", {"ec2:Owner": "123456789012"}, "implicitDeny"),
    ("Marketplace AMI", "ec2:RunInstances",
     f"arn:aws:ec2:{REGION}::image/ami-market", {"ec2:Owner": "aws-marketplace"}, "implicitDeny"),
    ("AMI without an owner in the request", "ec2:RunInstances", f"arn:aws:ec2:{REGION}::image/ami-x", {}, "implicitDeny"),
    ("Canonical AMI in another region", "ec2:RunInstances", "arn:aws:ec2:us-east-1::image/ami-1",
     dict(GLOBAL, **{"ec2:Owner": "amazon"}), "explicitDeny"),
    ("amazon-owned AMI still needs t3.micro on the instance", "ec2:RunInstances", f"{EC2}:instance/*",
     dict(TAG, **{"ec2:InstanceType": "m5.large", "ec2:MetadataHttpTokens": "required"}), "implicitDeny"),
]


@pytest.mark.parametrize("name,action,resource,ctx,expected", REALISTIC, ids=[r[0] for r in REALISTIC])
def test_realistic_contexts(name, action, resource, ctx, expected):
    assert decide("apply", action, resource, **ctx) == expected
