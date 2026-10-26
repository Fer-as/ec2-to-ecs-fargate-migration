"""Security gate for the RENDERED pipeline IAM policies (blocking in ci.yml).

Checkov cannot see policies rendered with templatefile(), so this gate checks the exact
JSON Terraform will send to IAM (templates rendered with test values) against:
  - the AWS Service Authorization Reference (ci/tests/data/iam-reference.json,
    refreshed by ci/refresh_iam_reference.py): every action exists, and every
    service-specific or tag condition key is supported by every action it guards
  - structural rules: no Not* elements, no admin or service-wide wildcards outside an
    allowlist, Resource "*" only where documented, no allow for untagged resources
  - a prohibited-action list that no Allow statement may match
"""

import fnmatch
import json
import re
from pathlib import Path

import pytest

from test_iam_policies import ROLES, load

REF = json.loads((Path(__file__).parent / "data" / "iam-reference.json").read_text(encoding="utf-8"))["services"]
NAMES = ["platform-read", "apply-network", "apply-services", "apply-core", "plan-store-write", "plan-store-read",
         "image-push"]
# How each role receives its policies (must match terraform/bootstrap/github_oidc.tf)
INLINE = {"plan": ["platform-read", "plan-store-write"], "image_push": ["image-push"],
          "apply": ["platform-read", "plan-store-read"]}
MANAGED = {"apply": ["apply-network", "apply-services", "apply-core"]}
STATEMENTS = [(n, s) for n in NAMES for s in load(n)["Statement"]]
ALLOWS = [(n, s) for n, s in STATEMENTS if s["Effect"] == "Allow"]

# Global keys present on every request (no per-action support needed)
GLOBAL_KEYS = {"aws:RequestedRegion"}

# Service-wide wildcards that are acceptable, and only on these exact resources
SERVICE_WIDE_OK = {
    ("ManageAppTableOnly", "dynamodb:*"),
    ("ManageAppBucketOnly", "s3:*"),
}
IF_EXISTS_OK = {"Ec2RetagProjectResources"}  # Project may be omitted, never changed

# No Allow statement may match any of these (actions or whole services)
PROHIBITED_ACTIONS = [
    "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:PutUserPolicy",
    "iam:AttachUserPolicy", "iam:AttachRolePolicy", "iam:CreatePolicy", "iam:CreatePolicyVersion",
    "iam:SetDefaultPolicyVersion", "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
    "iam:CreateOpenIDConnectProvider", "iam:UpdateOpenIDConnectProviderThumbprint",
    "iam:AddClientIDToOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider", "iam:CreateSAMLProvider",
    "ec2:CreateKeyPair", "ec2:ImportKeyPair", "ec2:ModifySnapshotAttribute", "ec2:ModifyImageAttribute",
    "ec2:CreateVpcPeeringConnection", "ec2:AcceptVpcPeeringConnection", "ec2:CreateTransitGateway",
    "ec2:CreateTransitGatewayVpcAttachment", "ec2:CreateVpnConnection", "ec2:CreateCustomerGateway",
    "ec2:GetPasswordData", "ec2:CreateSnapshot", "ec2:CreateImage", "ec2:ExportImage",
    "ec2:CreateInstanceExportTask", "ec2:EnableSerialConsoleAccess",
    "ecs:ExecuteCommand", "ecs:RunTask", "ecs:StartTask", "sns:Publish",
    "ecr:DeleteRepository", "ecr:SetRepositoryPolicy", "ecr:PutLifecyclePolicy",
    "s3:PutBucketPublicAccessBlock",
]
PROHIBITED_SERVICES = {"kms", "sts", "organizations", "account", "lambda", "rds", "secretsmanager", "ssm", "cloudtrail"}


def _list(v):
    return v if isinstance(v, list) else [v]


def _expand(action):
    """Concrete action names a pattern matches, from the reference data."""
    prefix, name = action.split(":", 1)
    return [f"{prefix}:{a}" for a in REF[prefix]["actions"] if fnmatch.fnmatchcase(a.lower(), name.lower())]


def _normalise(key):
    key = re.sub(r"^(aws:(?:RequestTag|ResourceTag))/.+$", r"\1/${TagKey}", key)
    return re.sub(r"^(ec2:Attribute)/.+$", r"\1/${AttributeName}", key)


def _cond_keys(stmt):
    return {k for keys in stmt.get("Condition", {}).values() for k in keys}


def _sid(item):
    return f"{item[0]}:{item[1]['Sid']}"


# --- reference checks ---------------------------------------------------------------------------

@pytest.mark.parametrize("item", STATEMENTS, ids=_sid)
def test_every_action_exists_in_the_aws_reference(item):
    for action in _list(item[1]["Action"]):
        assert action.split(":")[0] in REF, f"no reference data for {action}: run ci/refresh_iam_reference.py"
        assert _expand(action), f"{action} matches no AWS action (typo or removed action)"


@pytest.mark.parametrize("item", [i for i in STATEMENTS if i[1].get("Condition")], ids=_sid)
def test_condition_keys_are_supported_by_every_guarded_action(item):
    """A key an action does not support is absent from its requests: an Allow then never
    matches (broken) or, with IfExists/Null/negation, matches too much; a Deny never fires."""
    stmt = item[1]
    for key in _cond_keys(stmt) - GLOBAL_KEYS:
        for action in _list(stmt["Action"]):
            prefix, name = action.split(":", 1)
            if re.search(r"[*?]", name):
                continue  # only global keys are used on wildcard statements (checked below)
            supported = REF[prefix]["keys"].get(name)
            assert supported is not None, f"no key data for {action}: run ci/refresh_iam_reference.py"
            assert _normalise(key) in supported, f"{stmt['Sid']}: {action} does not support {key}"


def test_wildcard_statements_use_only_global_condition_keys():
    for _, s in STATEMENTS:
        if any(re.search(r"[*?]", a.split(":", 1)[1]) for a in _list(s["Action"])):
            assert _cond_keys(s) <= GLOBAL_KEYS, s["Sid"]


# --- structural rules ---------------------------------------------------------------------------

def test_no_not_elements_or_admin_wildcards():
    for _, s in STATEMENTS:
        assert not {"NotAction", "NotResource", "NotPrincipal", "Principal"} & s.keys(), s["Sid"]
        for a in _list(s["Action"]):
            assert a not in {"*", "*:*"} and not a.startswith("*"), s["Sid"]


def test_service_wide_wildcards_only_where_allowed():
    for _, s in ALLOWS:
        for a in _list(s["Action"]):
            if a.endswith(":*"):
                assert (s["Sid"], a) in SERVICE_WIDE_OK, f"{s['Sid']} grants {a}"
                assert "*" not in _list(s["Resource"]), s["Sid"]


def test_allow_never_targets_untagged_resources():
    # Null aws:ResourceTag/... = "true" means "only resources WITHOUT the tag" (other projects' too)
    for _, s in ALLOWS:
        for key, val in s.get("Condition", {}).get("Null", {}).items():
            assert not (key.startswith("aws:ResourceTag/") and str(val).lower() == "true"), s["Sid"]


def test_if_exists_only_where_reviewed():
    for _, s in ALLOWS:
        if any(op.endswith("IfExists") for op in s.get("Condition", {})):
            assert s["Sid"] in IF_EXISTS_OK, s["Sid"]


def test_tag_conditions_require_the_project_value():
    for _, s in ALLOWS:
        for op, keys in s.get("Condition", {}).items():
            for key, val in keys.items():
                if key in ("aws:RequestTag/Project", "aws:ResourceTag/Project") and op != "Null":
                    assert op in ("StringEquals", "StringEqualsIfExists") and val == "legacy-api", s["Sid"]


@pytest.mark.parametrize("role", sorted(ROLES))
def test_no_allow_matches_a_prohibited_action(role):
    for policy in ROLES[role]:
        for s in policy["Statement"]:
            if s["Effect"] != "Allow":
                continue
            for pattern in _list(s["Action"]):
                assert pattern.split(":")[0] not in PROHIBITED_SERVICES, f"{role}/{s['Sid']}: {pattern}"
                for bad in PROHIBITED_ACTIONS:
                    if fnmatch.fnmatchcase(bad.lower(), pattern.lower()):
                        # service-wide grants are allowed only on their exact project resources
                        assert (s["Sid"], pattern) in SERVICE_WIDE_OK and not bad.startswith(("ecr:", "iam:", "ec2:")), \
                            f"{role}/{s['Sid']}: {pattern} allows {bad}"


def test_required_explicit_denies_are_present():
    denies = {s["Sid"]: s for _, s in STATEMENTS if s["Effect"] == "Deny"}
    assert {"DenyOutsideApprovedRegion", "DenyDataSharingAndUnrelatedNetworking", "DenyInstanceTypeChange",
            "DenyPipelineIdentityChanges", "ProtectBootstrap"} <= denies.keys()
    # The region deny must never include global services (IAM requests use us-east-1)
    assert not any(a.split(":")[0] in {"iam", "sts", "route53", "s3"} for a in denies["DenyOutsideApprovedRegion"]["Action"])


# --- IAM size quotas (whitespace is not counted; the rendered test values have the same
# length as real ones: 12-digit account, same partition and region) -----------------------

def _size(name):
    return len(re.sub(r"\s", "", json.dumps(load(name), separators=(",", ":"))))


@pytest.mark.parametrize("role", sorted(INLINE))
def test_inline_policies_fit_the_role_quota(role):
    total = sum(_size(n) for n in INLINE[role])
    assert total <= 10240, f"{role}: {total} characters of inline policy (IAM limit 10,240)"


@pytest.mark.parametrize("name", [n for names in MANAGED.values() for n in names])
def test_managed_policies_fit_the_policy_quota(name):
    assert _size(name) <= 6144, f"{name}: {_size(name)} characters (IAM limit 6,144)"


def test_attached_managed_policies_within_default_quota():
    assert all(len(v) <= 10 for v in MANAGED.values())


def test_every_template_is_assigned_to_a_role():
    used = {n for v in list(INLINE.values()) + list(MANAGED.values()) for n in v}
    on_disk = {p.name.removesuffix(".json.tftpl") for p in (Path(__file__).resolve().parents[2] / "terraform" /
                                                             "bootstrap" / "policies").glob("*.json.tftpl")}
    assert used == on_disk
