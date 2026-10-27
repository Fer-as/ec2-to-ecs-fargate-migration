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

# --- Task 11: hardened pipeline permissions (offline; ci/tests/test_iam_policies.py
# evaluates the same templates statement by statement) ---------------------------------------

run "policies_are_fully_known_at_plan_time" {
  command = plan

  # ARNs are built from names, so the plan shows every policy in full
  assert {
    condition = alltrue([
      for p in [aws_iam_policy.apply_write["apply-network"].policy, aws_iam_policy.apply_write["apply-services"].policy,
        aws_iam_policy.apply_write["apply-core"].policy, aws_iam_role_policy.apply_read.policy,
      aws_iam_role_policy.plan_store.policy, aws_iam_role_policy.image_push.policy] :
      can(jsondecode(p).Statement)
    ])
    error_message = "Pipeline policies must render to valid JSON during plan."
  }
}

run "apply_role_has_no_service_wide_write" {
  command = plan

  # The old ec2:* / ecs:* / elasticloadbalancing:* ... "Resource *" allow is gone
  assert {
    condition = alltrue(flatten([
      for s in local.apply_write_statements : [
        for a in flatten([s.Action]) :
        s.Effect == "Deny" || !contains(["ec2:*", "ecs:*", "elasticloadbalancing:*", "application-autoscaling:*", "cloudwatch:*", "logs:*", "sns:*"], a)
      ]
    ]))
    error_message = "The apply role must not have service-wide (svc:*) write access to platform services."
  }

  # Only the documented statements may allow Resource "*"
  assert {
    condition = alltrue([
      for s in local.apply_write_statements :
      s.Effect == "Deny" || s.Resource != "*" || contains(["EcsDeregisterTaskDefinitionNoResourceLevel", "ServiceLinkedRoles"], s.Sid)
    ])
    error_message = "Unexpected Allow on Resource \"*\" in the apply policy."
  }

  # Task 33: DisassociateAddress on a released association is authorised against the literal
  # resource "...:*/*". The template escape $${*} must render to IAM's ${*} (a literal "*"),
  # never to a bare glob that would match every EC2 resource.
  assert {
    condition = length([
      for s in local.apply_write_statements : s
      if s.Sid == "Ec2DisassociateStaleEipAssociation" && s.Effect == "Allow" &&
      s.Action == "ec2:DisassociateAddress" &&
      s.Resource == "arn:aws:ec2:eu-west-2:111122223333:$${*}/$${*}" &&
      s.Condition.StringEquals["aws:RequestedRegion"] == "eu-west-2"
    ]) == 1
    error_message = "The stale EIP association allow must be DisassociateAddress on the literal */* resource only."
  }

  assert {
    condition = anytrue([
      for s in local.apply_write_statements :
      s.Sid == "DenyPipelineIdentityChanges" && s.Effect == "Deny" &&
      contains(s.Resource, "arn:aws:iam::111122223333:role/gha-*") &&
      contains(s.Resource, "arn:aws:iam::111122223333:oidc-provider/*") &&
      contains(s.Resource, "arn:aws:iam::111122223333:policy/gha-*")
    ])
    error_message = "The apply role must be denied changes to gha-* roles and OIDC providers."
  }

  assert {
    condition = anytrue([
      for s in local.apply_write_statements :
      s.Sid == "DenyDataSharingAndUnrelatedNetworking" && s.Effect == "Deny" &&
      contains(s.Action, "ec2:ModifySnapshotAttribute") && contains(s.Action, "ec2:CreateKeyPair") &&
      contains(s.Action, "ec2:ImportKeyPair") && contains(s.Action, "ec2:*VpcPeeringConnection*")
    ])
    error_message = "Snapshot/AMI sharing, key pairs and peering must be explicitly denied."
  }

  assert {
    condition = anytrue([
      for s in local.apply_write_statements :
      s.Sid == "Ec2RunLegacyInstanceOnly" &&
      s.Condition.StringEquals["ec2:InstanceType"] == "t3.micro" &&
      s.Condition.StringEquals["ec2:MetadataHttpTokens"] == "required" &&
      s.Condition.StringEquals["aws:RequestTag/Project"] == "legacy-api"
    ])
    error_message = "Instance launches must be t3.micro, IMDSv2-only and tagged Project=legacy-api."
  }

  assert {
    condition = anytrue([
      for s in local.apply_write_statements :
      s.Sid == "ProtectBootstrap" && s.Effect == "Deny" &&
      s.Resource == "arn:aws:s3:::legacy-api-tfstate-111122223333-eu-west-2"
    ])
    error_message = "The state bucket must stay protected from the apply role."
  }
}

run "plan_and_image_roles_stay_narrow" {
  command = plan

  # The plan role only reads, plus writing saved plans under plans/legacy-api/
  assert {
    condition = alltrue(flatten([
      for s in jsondecode(aws_iam_role_policy.plan.policy).Statement : [
        for a in flatten([s.Action]) :
        can(regex(":(Describe|Get|List)", a)) || (a == "s3:PutObject" || a == "s3:DeleteObject") && endswith(s.Resource, ".tflock")
      ]
    ]))
    error_message = "The plan role's read policy may only read (plus the state lock file)."
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.plan_store.policy).Statement[0].Resource == "arn:aws:s3:::legacy-api-tfstate-111122223333-eu-west-2/plans/legacy-api/*"
    error_message = "Saved plans may only be written under plans/legacy-api/."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.image_push.policy).Statement :
      s.Resource == "*" ? s.Action == "ecr:GetAuthorizationToken" : s.Resource == "arn:aws:ecr:eu-west-2:111122223333:repository/legacy-api"
    ])
    error_message = "The image-push role may only push to the legacy-api repository."
  }

  # Trust policies are unchanged: apply is still bound to the protected environment
  assert {
    condition     = local.sub_environment == "repo:example-owner@1000001/example-repo@2000002:environment:production"
    error_message = "The apply role must only trust the production environment subject."
  }
}

run "apply_policies_fit_iam_size_limits" {
  command = plan

  # IAM limits (not counting whitespace): 6,144 per managed policy, 10,240 for all inline
  # policies of a role. jsonencode() output is already minified.
  assert {
    condition     = alltrue([for p in aws_iam_policy.apply_write : length(p.policy) <= 6144])
    error_message = "Each apply managed policy must stay within 6,144 characters."
  }
  assert {
    condition = (length(aws_iam_role_policy.apply_read.policy) + length(aws_iam_role_policy.apply_plan_store.policy) <= 10240 &&
    length(aws_iam_role_policy.plan.policy) + length(aws_iam_role_policy.plan_store.policy) <= 10240)
    error_message = "Inline policies per role must stay within 10,240 characters in total."
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.apply_write) == 3 && alltrue([for a in aws_iam_role_policy_attachment.apply_write : a.role == "gha-legacy-api-apply"])
    error_message = "All three write policies must be attached to the apply role only."
  }
}

run "canonical_ami_owner_alias_is_accepted" {
  command = plan

  # Task 22: IAM sees ec2:Owner = "amazon" for Canonical's Ubuntu AMIs (owner alias);
  # only the Canonical account ID and that alias are accepted, for image and snapshot.
  assert {
    condition = anytrue([
      for s in local.apply_write_statements :
      s.Sid == "Ec2RunFromCanonicalUbuntuOnly" &&
      toset(s.Condition.StringEquals["ec2:Owner"]) == toset(["099720109477", "amazon"]) &&
      toset(s.Resource) == toset(["arn:aws:ec2:eu-west-2::image/*", "arn:aws:ec2:eu-west-2::snapshot/*"])
    ])
    error_message = "RunInstances images/snapshots must be limited to the Canonical account ID or its owner alias."
  }
}

run "scalable_target_tag_read_for_plan_and_apply_only" {
  command = plan

  # Task 26: provider 5.100.0 reads aws_appautoscaling_target tags on create and refresh.
  # The read is in platform-read (plan + apply roles), scoped to scalable targets in this
  # account and region; the image-push role does not get it.
  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.plan.policy, aws_iam_role_policy.apply_read.policy] : anytrue([
        for s in jsondecode(p).Statement :
        s.Sid == "ReadScalableTargetTags" && s.Effect == "Allow" &&
        s.Action == "application-autoscaling:ListTagsForResource" &&
        s.Resource == "arn:aws:application-autoscaling:eu-west-2:111122223333:scalable-target/*"
      ])
    ])
    error_message = "Plan and apply roles must read scalable-target tags in this account/region only."
  }
  assert {
    condition     = !strcontains(aws_iam_role_policy.image_push.policy, "application-autoscaling")
    error_message = "The image-push role must not get Application Auto Scaling access."
  }
}
