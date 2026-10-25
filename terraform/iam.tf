# IAM for the shared DynamoDB table and ECS.
#
# DynamoDB calls made by the application (app/storage.py):
#   GetItem   product, SEQUENCE (readiness)
#   Query     pk=PRODUCT, pk=ORDER
#   UpdateItem SEQUENCE (ADD last_order_id), PRODUCT (stock, inside TransactWriteItems)
#   PutItem   ORDER (inside TransactWriteItems)
# TransactWriteItems is authorised per item by UpdateItem/PutItem.
# Seeding (app/seed_dynamodb.py) only calls PutItem on PRODUCT and SEQUENCE items.
# dynamodb:LeadingKeys restricts each action to the partition keys it needs.

locals {
  account_id         = data.aws_caller_identity.current.account_id
  partition          = data.aws_partition.current.partition
  ecr_repository_arn = "arn:${local.partition}:ecr:${var.aws_region}:${local.account_id}:repository/${var.ecr_repository_name}"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy_document" "app_dynamodb" {
  statement {
    sid       = "ReadItems"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.app.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["PRODUCT", "ORDER", "SEQUENCE"]
    }
  }

  statement {
    sid       = "UpdateStockAndOrderSequence"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.app.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["PRODUCT", "SEQUENCE"]
    }
  }

  statement {
    sid       = "CreateOrders"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.app.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["ORDER"]
    }
  }
}

data "aws_iam_policy_document" "seed_dynamodb" {
  statement {
    sid       = "CreateCatalogueAndSequence"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.app.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["PRODUCT", "SEQUENCE"]
    }
  }
}

# --- Legacy EC2: same table access as ECS (needed before it can be the rollback target) ---

resource "aws_iam_role_policy" "ec2_dynamodb" {
  name   = "${var.project_name}-app-dynamodb"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.app_dynamodb.json
}

# --- ECS ---------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# Execution role: used by the ECS agent to pull the image and write logs.
# Scoped to the one repository and log group (instead of the AWS managed policy).
resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "ecs_execution" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # does not support resource-level permissions
  }

  statement {
    sid = "PullAppImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [local.ecr_repository_arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ecs_execution" {
  name   = "pull-image-and-write-logs"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution.json
}

# Task role: what the application code itself may do.
resource "aws_iam_role" "ecs_task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "ecs_task_dynamodb" {
  name   = "app-dynamodb"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.app_dynamodb.json
}

# Seed task role: used only by the one-off seed task definition.
resource "aws_iam_role" "ecs_seed" {
  name               = "${local.name}-ecs-seed"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "ecs_seed_dynamodb" {
  name   = "seed-dynamodb"
  role   = aws_iam_role.ecs_seed.id
  policy = data.aws_iam_policy_document.seed_dynamodb.json
}
