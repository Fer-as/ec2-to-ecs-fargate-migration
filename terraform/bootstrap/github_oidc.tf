# GitHub Actions -> AWS with OIDC (short-lived credentials, no stored keys).
#
# Three roles, each trusted only for ONE repository and ONE context, matching the
# jobs in .github/workflows/deploy.yml:
#
#   role                      workflow job         token "sub" (immutable format)
#   gha-<project>-image-push  publish (main)       repo:OWNER@OID/REPO@RID:ref:refs/heads/main
#   gha-<project>-plan        plan    (main)       repo:OWNER@OID/REPO@RID:ref:refs/heads/main
#   gha-<project>-apply       apply   (environment) repo:OWNER@OID/REPO@RID:environment:production
#
# Pull requests never get AWS credentials: PR workflows (ci.yml, terraform-ci.yml)
# run offline and do not request id-token. Fork PRs cannot match any subject.
# Role names start with "gha-" so the apply role (which may manage "<project>-*"
# IAM roles) can never modify these roles or their trust policies.

locals {
  oidc_host         = "token.actions.githubusercontent.com"
  oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
  arn_prefix        = "arn:${data.aws_partition.current.partition}"

  repo_display = "${var.github_owner}/${var.github_repository}"
  repo_subject = (var.github_subject_format == "immutable"
    ? "${var.github_owner}@${var.github_owner_id}/${var.github_repository}@${var.github_repository_id}"
  : local.repo_display)
  sub_main        = "repo:${local.repo_subject}:ref:refs/heads/${var.github_main_branch}"
  sub_environment = "repo:${local.repo_subject}:environment:${var.github_deploy_environment}"

  platform_state_key = "${var.project_name}/platform.tfstate"
  plans_prefix       = "plans/${var.project_name}"
  table_arn          = "${local.arn_prefix}:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-${var.environment}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's certificate chain itself; thumbprints are kept for older SDK paths.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://${local.oidc_host}"
}

# Trust policy: exact audience + exact subject (StringEquals, no wildcards)
data "aws_iam_policy_document" "trust" {
  for_each = {
    image_push = local.sub_main
    plan       = local.sub_main
    apply      = local.sub_environment
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value]
    }
  }
}

# --- Permission policies -------------------------------------------------------------------
#
# The policies live in policies/*.json.tftpl and are rendered with templatefile() using
# ARNs built from names (not resource attributes), so the full policy text is visible
# in `terraform plan` and the same files are evaluated offline by ci/tests/test_iam_policies.py.
# The permission matrix behind them is in docs/iam-permission-matrix.md.

locals {
  partition = data.aws_partition.current.partition
  policy_vars = {
    partition            = local.partition
    region               = var.aws_region
    account_id           = local.account_id
    project              = var.project_name
    table_arn            = local.table_arn
    state_bucket_arn     = "arn:${local.partition}:s3:::${local.state_bucket_name}"
    ecr_repository_arn   = "arn:${local.partition}:ecr:${var.aws_region}:${local.account_id}:repository/${var.ecr_repository_name}"
    platform_state_key   = local.platform_state_key
    plans_prefix         = local.plans_prefix
    legacy_instance_type = var.legacy_instance_type
    ami_owner            = var.legacy_ami_owner
  }

  policies = {
    for name in ["platform-read", "apply-network", "apply-services", "apply-core", "plan-store-write", "plan-store-read", "image-push"] :
    name => jsonencode(jsondecode(templatefile("${path.module}/policies/${name}.json.tftpl", local.policy_vars)))
  }

  # All write statements of the apply role (used by the tests)
  apply_write_statements = flatten([for n in ["apply-network", "apply-services", "apply-core"] : jsondecode(local.policies[n]).Statement])
}

# --- Roles ------------------------------------------------------------------------------------

resource "aws_iam_role" "plan" {
  name                 = "gha-${var.project_name}-plan"
  description          = "GitHub Actions: terraform plan for ${local.repo_display} main (read + saved plan)"
  assume_role_policy   = data.aws_iam_policy_document.trust["plan"].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "plan" {
  name   = "platform-read"
  role   = aws_iam_role.plan.id
  policy = local.policies["platform-read"]
}

resource "aws_iam_role_policy" "plan_store" {
  name   = "saved-plan-write"
  role   = aws_iam_role.plan.id
  policy = local.policies["plan-store-write"]
}

resource "aws_iam_role" "image_push" {
  name                 = "gha-${var.project_name}-image-push"
  description          = "GitHub Actions: push images from ${local.repo_display} main to ECR"
  assume_role_policy   = data.aws_iam_policy_document.trust["image_push"].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "image_push" {
  name   = "ecr-push"
  role   = aws_iam_role.image_push.id
  policy = local.policies["image-push"]
}

resource "aws_iam_role" "apply" {
  name                 = "gha-${var.project_name}-apply"
  description          = "GitHub Actions: terraform apply for ${local.repo_display} (environment ${var.github_deploy_environment})"
  assume_role_policy   = data.aws_iam_policy_document.trust["apply"].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "apply_read" {
  name   = "platform-read"
  role   = aws_iam_role.apply.id
  policy = local.policies["platform-read"]
}

# Write access is too large for inline policies (IAM caps a role's inline policies at
# 10,240 characters in total), so it is split into three customer-managed policies of at
# most 6,144 characters each (ci/tests/test_rendered_policy_gate.py checks both limits).
# Their names start with "gha-": the apply role is explicitly denied any change to them.
resource "aws_iam_policy" "apply_write" {
  for_each    = toset(["apply-network", "apply-services", "apply-core"])
  name        = "gha-${var.project_name}-${each.key}"
  description = "Terraform apply permissions (${each.key}) for gha-${var.project_name}-apply"
  policy      = local.policies[each.key]
}

resource "aws_iam_role_policy_attachment" "apply_write" {
  for_each   = aws_iam_policy.apply_write
  role       = aws_iam_role.apply.name
  policy_arn = each.value.arn
}

resource "aws_iam_role_policy" "apply_plan_store" {
  name   = "saved-plan-read"
  role   = aws_iam_role.apply.id
  policy = local.policies["plan-store-read"]
}
