# First deployment checklist (operator runbook)

Nothing in this file has been executed in AWS yet (26 Oct 2026). Every state-changing
step needs your explicit decision at the time. Commands are PowerShell from the
repository root unless stated. Design: [aws-platform.md](aws-platform.md); data rules:
[migration-state-runbook.md](migration-state-runbook.md); pipeline: [ci-cd.md](ci-cd.md).

**Golden rule:** at any STOP, do not continue, re-run or "try again with more
permissions". Record what you saw, roll back if traffic is affected (phase 9), and
investigate.

## Phase 0: before touching AWS

| # | Step | Expected | STOP if |
|---|---|---|---|
| 0.1 | Approval gate runtime tests 1-3 ([approval-gate-test.md](approval-gate-test.md)) | Reject blocks, approve runs, other branch refused | The job ran without a review request |
| 0.2 | Latest `main` has green `CI` and `Terraform CI` runs | Both green | Any red gate |
| 0.3 | `aws sts get-caller-identity --query Account --output text` | Account ending **7756** | Any other account |
| 0.4 | `aws configure get region` | `eu-west-2` | Any other region |
| 0.5 | Cost: about **$3.6-3.9 per day** while everything runs (NAT, ALB, 4 public IPv4, Fargate; [aws-platform.md §7](aws-platform.md)). Optional: create an AWS Budget alert yourself in the console | You accept the cost and a teardown date | No teardown date agreed |

## Phase 1: bootstrap (23 resources, local state)

| # | Step | Expected | STOP if |
|---|---|---|---|
| 1.1 | Fresh read-only plan: `terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan` (short-lived credentials exported in-process, as in Task 10) | `Plan: 23 to add, 0 to change, 0 to destroy`; OIDC provider only *read* | Any change/destroy, a new OIDC provider, a different count |
| 1.2 | Independent review of the plan text + [iam-permission-matrix.md](iam-permission-matrix.md); explicit approval | Written approval | Not approved |
| 1.3 | **Apply (you, manually):** `terraform -chdir=terraform/bootstrap apply bootstrap.tfplan` | `Apply complete! Resources: 23 added` | Any error, especially `AccessDenied` or `LimitExceeded`; do not re-plan with broader rights |
| 1.4 | Protect the bootstrap state: copy `terraform/bootstrap/terraform.tfstate` to a private backup (never Git). Optional later: migrate it into the bucket (`versions.tf` comment, key `legacy-api/bootstrap.tfstate`) as a reviewed change | Backup exists | Backup missing: losing this file loses Terraform control of the state bucket, ECR and roles |
| 1.5 | `terraform -chdir=terraform/bootstrap output` | `state_bucket_name`, `ecr_repository_url`, three `github_*_role_arn`, `saved_plans_prefix` = `plans/legacy-api`, `trusted_oidc_subjects` | Values missing |
| 1.6 | Check: bucket versioning `Enabled`, public access blocked; ECR `IMMUTABLE`, scan on push; three roles exist with the expected trust (`aws iam get-role --role-name gha-legacy-api-apply --query Role.AssumeRolePolicyDocument`) | Matches the plan | Trust subject differs from `trusted_oidc_subjects` |

## Phase 2: GitHub configuration (read-back, then set variables)

| # | Step | Expected | STOP if |
|---|---|---|---|
| 2.1 | Repository **variables** ([ci-cd.md §6](ci-cd.md)): `AWS_ACCOUNT_ID`, `AWS_REGION=eu-west-2`, `AWS_IMAGE_PUSH_ROLE_ARN`, `AWS_PLAN_ROLE_ARN`, `AWS_APPLY_ROLE_ARN`, `ECR_REPOSITORY=legacy-api`, `TF_STATE_BUCKET`, **`TF_PLAN_PREFIX=plans/legacy-api`** (IAM only allows this prefix), `TF_AVAILABILITY_ZONES=["eu-west-2a","eu-west-2b"]` | Set | `TF_PLAN_PREFIX` differs from `saved_plans_prefix` (the plan job would get AccessDenied) |
| 2.2 | Optional **secrets**: `TF_VERIFICATION_CIDR_BLOCKS` (your IP `/32` for header routes), `TF_ALARM_EMAIL`, `TF_ALLOWED_CIDR_BLOCKS` | Set as secrets | A `0.0.0.0/0` value (validation rejects it) |
| 2.3 | Re-read `production` environment protection (API commands in approval-gate-test.md) | Reviewer, no admin bypass, `main` only | Anything changed |

## Phase 3: remote state and locking

Before Deploy #1 the platform state does not exist yet; the backend creates it.

| # | Step | Expected | STOP if |
|---|---|---|---|
| 3.1 | `terraform -chdir=terraform init -lockfile=readonly -backend-config="bucket=<state_bucket_name>" -backend-config="region=eu-west-2"` | `Successfully configured the backend "s3"` | Init asks to migrate or copy existing state |
| 3.2 | Lock test (read-only): in both terminals set `$env:TF_VAR_availability_zones = '["eu-west-2a","eu-west-2b"]'` (avoids PowerShell 5.1 stripping quotes from `-var`). Terminal A: `terraform -chdir=terraform plan -var-file=environments/production.tfvars`; while it refreshes, terminal B: the same with `-lock-timeout=0s` | B fails with `Error acquiring the state lock` and names `legacy-api/platform.tfstate.tflock` | B also runs: locking is not working; do not deploy |
| 3.3 | After both end: `aws s3api head-object --bucket <state_bucket_name> --key legacy-api/platform.tfstate.tflock` | 404 (lock released) | Lock object remains: find the holder; use `terraform force-unlock` only if you are certain no run is active |

## Phase 4: Deploy #1, platform at stage 0 (EC2 still in memory mode)

`environments/production.tfvars` as committed: `ecs_service_enabled = false`,
`legacy_storage_backend = "memory"`, `traffic_stage = 0`.

| # | Step | Expected | STOP if |
|---|---|---|---|
| 4.1 | Actions → Deploy → `deployment_kind = platform` on `main` | preflight, CI and Terraform CI gates green | Any gate red |
| 4.2 | `publish`: image built, Trivy gate, pushed as tag = commit SHA; summary shows `sha256:` digest | Digest shown | Trivy fails; push denied |
| 4.3 | `plan`: plan_guard summary | Only **creates**: VPC, subnets, NAT, endpoints, ALB (stage 0 weights EC2 100 / ECS 0), table, IAM roles, task definitions **app + seed**, log groups, alarms, dashboard, EC2, bucket. **No** `aws_ecs_service` | Any delete/replace, an ECS service, weights other than 100/0 |
| 4.4 | Read the full plan text (`plans/legacy-api/<run>/plan.log` in the state bucket) and approve in GitHub | Apply starts only after approval | Apply starts without a review request |
| 4.5 | `apply` + `verify_rollout.py` | "ECS service not enabled yet"; EC2 target healthy (bootstrap takes minutes); `/health` and `/ready` 200 via `alb_url` | Verification failed: see phase 8 |
| 4.6 | Confirm SNS email subscription (if `TF_ALARM_EMAIL` set) | Subscription confirmed | — |

## Phase 5: seed DynamoDB once, then start the ECS service (0 % traffic)

| # | Step | Expected | STOP if |
|---|---|---|---|
| 5.1 | `terraform -chdir=terraform output -raw seed_command`, run it (you, with your own credentials) | Task `STOPPED` with exit code 0; log stream `seed/...` in `/ecs/legacy-api-production` shows `Seed complete. Created: ...` | Exit code ≠ 0, AccessDenied |
| 5.2 | **Reseeding warning:** re-running the seed only creates *missing* items and never resets stock or the order counter; do not delete and recreate the table to "start fresh" (deletion protection, PITR) | — | Someone proposes deleting the table |
| 5.3 | PR: `ecs_service_enabled = true` → merge → Deploy (`platform`) | Plan: create `aws_ecs_service.app[0]`, autoscaling target and policy; listener unchanged (EC2 100 / ECS 0) | Any other change, especially to `aws_instance.app` |
| 5.4 | After approval/apply: verify output | ECS service stable, `rolloutState COMPLETED`, running = desired (2), ECS targets healthy, `/ready` 200 | Unhealthy ECS targets: stop; read ECS service events and `/ecs/legacy-api-production` logs |

## Phase 6: write freeze and EC2 conversion to DynamoDB

**EC2 replacement warning:** this deploy **replaces** `aws_instance.app`. At stage 0
the ALB sends all traffic to EC2, so expect 503s for several minutes while the new
instance boots. Orders held in the old instance's memory are discarded (agreed write
freeze, [migration-state-runbook.md §6](migration-state-runbook.md)).

| # | Step | Expected | STOP if |
|---|---|---|---|
| 6.1 | Announce the write freeze; stop POST traffic | Freeze in effect | — |
| 6.2 | PR: `legacy_storage_backend = "dynamodb"` → Deploy (`platform`) | plan_guard notes `LEGACY STORAGE MODE CHANGE` and `aws_instance.app is REPLACED`; nothing else replaced or deleted (the Elastic IP association and target attachment follow the instance) | Any other replacement/deletion, especially `aws_dynamodb_table.app` (the guard refuses it anyway) |
| 6.3 | Approve; `verify_rollout.py` waits for the new EC2 target | EC2 target healthy, `/ready` 200 | EC2 never healthy: keep the freeze; check `/var/log/cloud-init-output.log` via SSM; do not start stages |
| 6.4 | Consistency checks ([runbook §9](migration-state-runbook.md)), from your IP via header routes: `curl.exe -H "X-Migration-Target: ecs" <alb_url>/api/v1/stats` and the same with `ec2`; place one order via each and see it on the other | Identical products, orders, stats | Any difference: do not lift the freeze |
| 6.5 | Lift the write freeze | — | — |

## Phase 7: staged cutover (one reviewed `platform` deploy per stage)

| Stage | `traffic_stage` | Weights EC2 / ECS | Hold at least |
|---|---|---|---|
| 1 | 1 | 90 / 10 | 15 min |
| 2 | 2 | 50 / 50 | 15 min |
| 3 | 3 | 0 / 100 | until accepted |

For each stage: PR → Deploy → plan shows the listener weight change (and, because
every commit builds a new image, a task definition update; see
"Known issue" below) → approve → verify → then check:

- dashboard `legacy-api-production-migration`: traffic split, 5xx, target health;
- `aws cloudwatch describe-alarms --alarm-name-prefix legacy-api-production --state-value ALARM` returns nothing;
- runbook §9 consistency checks.

**STOP and roll back (phase 9)** on any alarm in ALARM, unhealthy targets, `/ready`
≠ 200, EC2/ECS data mismatch, IAM AccessDenied, or a plan with unexpected changes.

## Phase 8: failed deployment response

| Symptom | Response |
|---|---|
| Plan refused by plan_guard | Read the reason; fix the PR; never switch to `platform` just to pass |
| Unexpected replacement/deletion in the plan | Reject the approval; investigate |
| `AccessDenied` in apply | Stop. Note the action and resource; propose one scoped IAM addition through a reviewed bootstrap change (matrix §5). Never broaden to `*` |
| `Error acquiring the state lock` | Another run is active; wait. `force-unlock` only with proof that no run holds it |
| ECS circuit breaker rolled back | Service is back on the previous task definition; read stopped-task reasons |
| Verification failed after apply | Traffic stage unchanged? Look at the diagnostics in the job log; if users are affected, roll back |

## Phase 9: rollback

1. PR `traffic_stage = 0` → Deploy (`platform`) → approve (about a minute after approval).
2. Expected plan: only `aws_lb_listener.http` changes.
3. Verify: EC2 target healthy, `/ready` 200, orders written via ECS visible on EC2.
4. Emergency without the pipeline: an administrator sets the weights in the console,
   then the same `traffic_stage` is committed immediately ([ci-cd.md §8](ci-cd.md)).
5. Never roll back to `legacy_storage_backend = "memory"` after shared writes began.

## Phase 10: evidence (before teardown)

Screenshots + text: approval pause and approver; plan_guard summaries; ECR image with
SHA tag and scan result; ECS service events and both target groups healthy; dashboard
at each stage; alarm list (all OK); rollback to stage 0 with timing; `terraform output`
(no secrets); bootstrap `trusted_oidc_subjects` next to the `sub` printed by the
publish job; video walkthrough.

## Phase 11: teardown and cost check

| # | Step | Expected | STOP if |
|---|---|---|---|
| 11.1 | Stage 0; export data if wanted (`aws dynamodb create-backup`) | — | — |
| 11.2 | PR `alb_deletion_protection = false` and `dynamodb_deletion_protection = false` → Deploy (`platform`) | Only those attributes change | Anything else |
| 11.3 | Empty the versioned bundle bucket (all versions) — Terraform does not force-destroy it | Bucket empty | — |
| 11.4 | `terraform -chdir=terraform destroy` (you, locally, with the backend initialised) — review the destroy plan before typing `yes` | All platform resources destroyed | `BucketNotEmpty`, deletion protection errors: fix the cause, not with `-target` tricks |
| 11.5 | Delete ECR images (`aws ecr batch-delete-image`), then `terraform -chdir=terraform/bootstrap destroy` if you also remove the bootstrap; the state bucket has `prevent_destroy` (keep it, it costs cents, or follow [aws-platform.md §8](aws-platform.md)) | — | — |
| 11.6 | Billable leftovers, all must be empty in eu-west-2: `aws ec2 describe-nat-gateways --filter Name=state,Values=available`, `aws ec2 describe-addresses`, `aws elbv2 describe-load-balancers`, `aws ecs list-clusters`, `aws ec2 describe-instances --filters Name=instance-state-name,Values=running`, `aws dynamodb list-tables`, `aws logs describe-log-groups --log-group-name-prefix /ecs/legacy-api` | No project resources | Anything left: remove it before the next billing hour |
| 11.7 | GitHub: remove the repository variables (or the roles no longer exist anyway) | Deploy fails at preflight | — |

## Known issue to review before Stage 1

Image builds are not bit-reproducible (evidence/task-04-container/10-reproducibility.txt),
and `deploy.yml` builds and pushes a new image for every commit. A `traffic_stage` PR
therefore also rolls out a new task definition at the same time as the weight change.
`verify_rollout.py` waits for the service to be stable, so this is safe, but it mixes
two changes. Minimal correction (workflow only, not applied): an optional
`image_digest` input that skips build/push for `platform` runs and reuses an existing,
verified ECR digest. See the Task 16 readiness review.
