# Offline tests for the bootstrap root (MOCKED provider, no AWS).
#   terraform -chdir=terraform/bootstrap init -backend=false && terraform -chdir=terraform/bootstrap test

mock_provider "aws" {
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  aws_region           = "eu-west-2"
  github_owner         = "example-owner"
  github_repository    = "example-repo"
  github_owner_id      = "1000001"
  github_repository_id = "2000002"
}

run "plan_expiry_can_never_touch_state_or_locks" {
  command = plan

  # The only rule that expires CURRENT objects is scoped to plans/
  assert {
    condition = alltrue([
      for r in aws_s3_bucket_lifecycle_configuration.state.rule :
      length(r.expiration) == 0 || (r.id == "expire-saved-plans" && r.filter[0].prefix == "plans/")
    ])
    error_message = "Only the saved-plan rule may expire current objects, and only under plans/."
  }

  # State key, lock file and bootstrap state do not start with plans/
  assert {
    condition = alltrue([
      for key in ["legacy-api/platform.tfstate", "legacy-api/platform.tfstate.tflock", "legacy-api/bootstrap.tfstate"] :
      !startswith(key, "plans/")
    ]) && startswith(local.plans_prefix, "plans/")
    error_message = "State, lock and plan objects must live under different prefixes."
  }

  # The general rule only removes old NONCURRENT versions and always keeps 10
  assert {
    condition = anytrue([
      for r in aws_s3_bucket_lifecycle_configuration.state.rule :
      r.id == "expire-old-state-versions" && length(r.expiration) == 0 &&
      r.noncurrent_version_expiration[0].noncurrent_days == 90 &&
      r.noncurrent_version_expiration[0].newer_noncurrent_versions == 10
    ])
    error_message = "State history must be kept (noncurrent versions only, 90 days, newest 10 kept)."
  }

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "State bucket must be versioned."
  }
}

run "immutable_oidc_subjects" {
  command = plan

  assert {
    condition = output.trusted_oidc_subjects == {
      image_push = "repo:example-owner@1000001/example-repo@2000002:ref:refs/heads/main"
      plan       = "repo:example-owner@1000001/example-repo@2000002:ref:refs/heads/main"
      apply      = "repo:example-owner@1000001/example-repo@2000002:environment:production"
    }
    error_message = "Immutable subjects must be repo:OWNER@OWNER_ID/REPO@REPO_ID:<context>."
  }
}

run "legacy_oidc_subjects" {
  command = plan

  variables {
    github_subject_format = "legacy"
    github_owner_id       = null
    github_repository_id  = null
  }

  assert {
    condition = output.trusted_oidc_subjects == {
      image_push = "repo:example-owner/example-repo:ref:refs/heads/main"
      plan       = "repo:example-owner/example-repo:ref:refs/heads/main"
      apply      = "repo:example-owner/example-repo:environment:production"
    }
    error_message = "Legacy subjects must be repo:OWNER/REPO:<context>."
  }
}

run "immutable_format_requires_numeric_ids" {
  command = plan

  variables {
    github_repository_id = null
  }

  expect_failures = [var.github_repository_id]
}

run "wildcards_are_not_accepted_in_the_repository_name" {
  command = plan

  variables {
    github_repository = "*"
  }

  expect_failures = [var.github_repository]
}
