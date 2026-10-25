# Bootstrap: resources that must exist before the platform root can run.
#   - S3 bucket for remote Terraform state (locking via S3 lock files)
#   - ECR repository for the application image
#   - GitHub OIDC provider and pipeline roles (github_oidc.tf)

locals {
  account_id        = data.aws_caller_identity.current.account_id
  state_bucket_name = "${var.project_name}-tfstate-${local.account_id}-${var.aws_region}"
}

# --- Remote state bucket ----------------------------------------------------------

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18:Access logging needs a separate log bucket; access is limited to 3 roles + admins and audited by CloudTrail (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_144:Cross-region replication not needed: versioned, small, re-creatable state (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) encryption at rest; a customer-managed KMS key adds cost and key-policy work (see docs/security-exceptions.md)
  #checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications on the state bucket (see docs/security-exceptions.md)
  bucket = local.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 10
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Saved plans are sensitive and only needed between plan and approved apply
  rule {
    id     = "expire-saved-plans"
    status = "Enabled"
    filter {
      prefix = "plans/"
    }

    expiration {
      days = var.plan_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

# Deny any non-TLS access to state
data "aws_iam_policy_document" "state_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- ECR ------------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  #checkov:skip=CKV_AWS_136:AES256 encryption at rest; a customer-managed KMS key adds cost and key-policy work (see docs/security-exceptions.md)
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the most recent images (rollback window)"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_keep_image_count
        }
        action = { type = "expire" }
      },
    ]
  })
}
