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
  iam_prefix        = "${local.arn_prefix}:iam::${local.account_id}"

  repo_display = "${var.github_owner}/${var.github_repository}"
  repo_subject = (var.github_subject_format == "immutable"
    ? "${var.github_owner}@${var.github_owner_id}/${var.github_repository}@${var.github_repository_id}"
  : local.repo_display)
  sub_main        = "repo:${local.repo_subject}:ref:refs/heads/${var.github_main_branch}"
  sub_environment = "repo:${local.repo_subject}:environment:${var.github_deploy_environment}"

  platform_state_key = "${var.project_name}/platform.tfstate"
  plans_prefix       = "plans/${var.project_name}"
  # Resources managed by the platform root are named "<project>-..."
  managed_name = "${var.project_name}-*"
  table_arn    = "${local.arn_prefix}:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-${var.environment}"
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

# --- Shared policy pieces -----------------------------------------------------------------

# Read access needed by `terraform plan` to refresh the platform resources.
data "aws_iam_policy_document" "platform_read" {
  #checkov:skip=CKV_AWS_356:Describe/List/Get* read APIs used by terraform refresh do not support resource-level permissions (see docs/security-exceptions.md)
  statement {
    sid = "DescribePlatformServices"
    actions = [
      "ec2:Describe*",
      "elasticloadbalancing:Describe*",
      "ecs:Describe*",
      "ecs:List*",
      "application-autoscaling:Describe*",
      "cloudwatch:Describe*",
      "cloudwatch:GetDashboard",
      "cloudwatch:ListDashboards",
      "cloudwatch:ListTagsForResource",
      "logs:Describe*",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
      "sns:GetTopicAttributes",
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTagsForResource",
      "ecr:DescribeRepositories",
      "ecr:DescribeImages",
      "ecr:ListTagsForResource",
      "route53:GetHostedZone",
      "route53:GetChange",
      "route53:ListResourceRecordSets",
      "route53:ListTagsForResource",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "DescribeAppTable"
    actions   = ["dynamodb:Describe*", "dynamodb:ListTagsOfResource"]
    resources = [local.table_arn]
  }

  statement {
    sid     = "ReadProjectIam"
    actions = ["iam:Get*", "iam:List*"]
    resources = [
      "${local.iam_prefix}:role/${local.managed_name}",
      "${local.iam_prefix}:instance-profile/${local.managed_name}",
      "${local.iam_prefix}:policy/${local.managed_name}",
    ]
  }

  statement {
    sid       = "ReadAppBucket"
    actions   = ["s3:Get*", "s3:List*"]
    resources = ["${local.arn_prefix}:s3:::${var.project_name}-app-*"]
  }

  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.platform_state_key}"]
  }

  statement {
    sid       = "StateLockFile"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.platform_state_key}.tflock"]
  }
}

# The plan job stores the saved plan privately in the state bucket (never as a
# public workflow artifact); the apply job reads it back after approval.
data "aws_iam_policy_document" "plan_store_write" {
  statement {
    sid       = "WriteSavedPlans"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.plans_prefix}/*"]
  }
}

data "aws_iam_policy_document" "plan_store_read" {
  statement {
    sid       = "ReadSavedPlans"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.plans_prefix}/*"]
  }
}

# Write access needed by `terraform apply` on the platform root.
data "aws_iam_policy_document" "platform_write" {
  statement {
    sid = "ManagePlatformServicesInRegion"
    actions = [
      "ec2:*",
      "elasticloadbalancing:*",
      "ecs:*",
      "application-autoscaling:*",
      "cloudwatch:*",
      "logs:*",
      "sns:*",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid       = "ManageAppTableOnly"
    actions   = ["dynamodb:*"]
    resources = [local.table_arn]
  }

  statement {
    sid       = "ManageAppBucketOnly"
    actions   = ["s3:*"]
    resources = ["${local.arn_prefix}:s3:::${var.project_name}-app-*"]
  }

  statement {
    sid     = "WriteState"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.state.arn}/${local.platform_state_key}",
    ]
  }

  statement {
    sid = "ManageProjectRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
      "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
      "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
      "iam:TagInstanceProfile", "iam:UntagInstanceProfile",
    ]
    resources = [
      "${local.iam_prefix}:role/${local.managed_name}",
      "${local.iam_prefix}:instance-profile/${local.managed_name}",
    ]
  }

  statement {
    sid       = "PassProjectRolesToKnownServicesOnly"
    actions   = ["iam:PassRole"]
    resources = ["${local.iam_prefix}:role/${local.managed_name}"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com", "ec2.amazonaws.com", "vpc-flow-logs.amazonaws.com"]
    }
  }

  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values = [
        "ecs.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
        "ecs.application-autoscaling.amazonaws.com",
      ]
    }
  }

  statement {
    sid    = "ProtectBootstrap"
    effect = "Deny"
    actions = [
      "s3:DeleteBucket", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
      "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration",
    ]
    resources = [aws_s3_bucket.state.arn]
  }
}

data "aws_iam_policy_document" "image_push" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # does not support resource-level permissions
  }

  statement {
    sid = "PushToAppRepository"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeImageScanFindings",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
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
  policy = data.aws_iam_policy_document.platform_read.json
}

resource "aws_iam_role_policy" "plan_store" {
  name   = "saved-plan-write"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan_store_write.json
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
  policy = data.aws_iam_policy_document.image_push.json
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
  policy = data.aws_iam_policy_document.platform_read.json
}

resource "aws_iam_role_policy" "apply_write" {
  name   = "platform-write"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.platform_write.json
}

resource "aws_iam_role_policy" "apply_plan_store" {
  name   = "saved-plan-read"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.plan_store_read.json
}
