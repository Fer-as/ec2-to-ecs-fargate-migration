# Deployment readiness review (Task 16, 26 Oct 2026)

> **Pre-deployment snapshot.** This review was written before anything ran in AWS. The
> platform has since been deployed and the full cutover, rollback and restoration were
> verified on 28 Oct 2026. See [evidence/live-migration-2026-10-28](../evidence/live-migration-2026-10-28/README.md)
> and the [acceptance checklist](acceptance-checklist.md). The text below is unchanged.

Source of truth: the Terraform and workflow code at commit `9dd04a4` plus the
uncommitted Task 14/16 changes. Nothing has run in AWS. Operator steps:
[first-deployment-checklist.md](first-deployment-checklist.md).

Legend for "Verified": **L** = local tests/rehearsal, **CI** = GitHub Actions,
**AWS** = live, **–** = not yet.

## 1. Lifecycle trace

| # | Stage | Preconditions | Key variables / permissions | Expected resources and readiness checks | Failure → response | Verified |
|---|---|---|---|---|---|---|
| 1 | Bootstrap | Account …7756, eu-west-2, reviewed 23-create plan | Operator credentials; `create_github_oidc_provider = false` | S3 state bucket, ECR, 3 `gha-*` roles, 5 inline + 3 managed policies | AccessDenied/quota → stop; nothing to roll back except `terraform destroy` of bootstrap | Plan **L** (authenticated read-only); IAM **L** (172 simulator cases) |
| 2 | Remote state | Bucket exists | backend key `legacy-api/platform.tfstate`, `use_lockfile = true`; plan/apply roles: Get/Put/Delete only on `….tflock`, Put only on the state key | Lock object appears during plans | Lock conflict → wait; never force-unlock blindly | – |
| 3 | Deploy #1 (EC2 baseline + platform) | Approval gate runtime-verified; repo variables set | `ecs_service_enabled=false`, `legacy_storage_backend=memory`, `traffic_stage=0`; apply role write policies | VPC/2 AZ, NAT, endpoints, ALB 100/0, table, task definitions app+seed, alarms, EC2 | verify_rollout fails → no traffic change needed (stage 0); fix and redeploy | Mocked plan **L**, Terraform CI **CI** |
| 4 | Image build/scan/publish | CI + Terraform CI gates pass in the same run | image-push role (`ref:refs/heads/main`), ECR immutable tags | Tag = commit SHA, digest read back and validated | Trivy gate fails → nothing pushed | Build, smoke, Trivy **L**/**CI**; push – |
| 5 | Task definitions | Digest exists in ECR (data source checks it at plan time) | `app_image_digest` from publish | Families `legacy-api-production-app` / `-seed` pinned by digest | Plan fails if the digest is missing | Mocked **L** |
| 6 | Seed | Table exists | Operator credentials (`ecs:RunTask` is deliberately NOT in the apply role) | `seed_command` output; log `Seed complete` | Re-run is safe (create-if-missing) | **L** (DynamoDB Local + container rehearsal) |
| 7 | ECS service healthy | Seeded (else `/ready` 503 keeps targets unhealthy and the circuit breaker rolls back) | `ecs_service_enabled=true`; PassRole to ecs-tasks; SLRs | 2 tasks, private subnets, no public IP, ALB target healthy, weight 0 | Circuit breaker rollback; stop | /ready gating **L**; AWS – |
| 8 | EC2 → DynamoDB | Write freeze | `legacy_storage_backend=dynamodb` → `aws_instance.app` replaced (`replace_triggered_by`) | New EC2 healthy, both backends identical | EC2 unhealthy → keep freeze, do not start stages | Shared state **L** |
| 9 | Staged cutover | Preconditions in `aws_lb_listener.http` (service exists, EC2 on DynamoDB) | `traffic_stage` 1→2→3, one `platform` deploy each | Weights 90/10, 50/50, 0/100; alarms OK | Any STOP condition → stage 0 | Weights **L** (mocked tests) |
| 10 | Production on ECS | Stage 3 | – | 100 % ECS, EC2 at weight 0 kept registered | – | – |
| 11 | Rollback | EC2 still DynamoDB-backed and healthy | `traffic_stage=0` | Listener change only | Console fallback, then commit | Weights **L**; simulated rollback **L** (rehearsal); AWS – |
| 12 | Teardown | Evidence captured | Deletion protections off first | All billable resources gone | `BucketNotEmpty`/`RepositoryNotEmpty` → empty first | – |

Cross-checks that hold (code read, no change needed):
- Plan role reads everything the plan refreshes (Describe/List/Get on `*`, ECR
  `DescribeImages` for the digest data source, project IAM and buckets, DynamoDB describe).
- `TF_PLAN_PREFIX` must equal `plans/legacy-api`; the plan job writes plan and log
  there and the apply job reads only there.
- `verify_rollout.py` runs inside the approved apply job before it can succeed; it
  waits for ECS stability and EC2 bootstrap, then checks `/health` and `/ready`.
- Header routes exist only with `verification_cidr_blocks` and require a source IP
  match; they cannot bypass the weights for the public.
- `plan_guard.py` refuses any deletion/replacement of `aws_dynamodb_table.app` and,
  for `application` runs, any weight or storage-mode change.
- Approval: only `deploy.yml` `publish`/`plan`/`apply` can obtain OIDC tokens
  (`test_workflow_privileges.py`, all workflow files). Apply runs only in
  `production`; the apply role trusts only that environment subject.

## 2. Findings (severity-ranked)

No defect found that requires changing Terraform, IAM or OIDC before the first deployment.

| Sev. | Finding | Evidence | Action |
|---|---|---|---|
| **Blocker (process)** | Approval gate configured but not runtime-verified | approval-gate-test.md | Run Tests 1-3 before setting deploy variables |
| **Blocker (process)** | Bootstrap needs independent plan review + explicit approval; local bootstrap state must be backed up at once | Task 12 evidence | Checklist phase 1 |
| Medium | Every deploy builds a new image; builds are not bit-reproducible, so each `traffic_stage` deploy also rolls a new task definition | evidence/task-04-container/10-reproducibility.txt; deploy.yml `publish` | Proposed (not applied): optional `image_digest` input that skips build/push and verifies an existing ECR digest for `platform` runs; policy tests to be extended with it |
| Medium | EC2 conversion at stage 0 is a full outage of a few minutes (all traffic on the instance being replaced; ECS weight 0) | `replace_triggered_by`, listener precondition | Accepted with the write freeze; documented in the checklist. Avoiding it needs an infrastructure change (out of scope) |
| Medium | Teardown of the versioned bundle bucket and ECR repository needs manual emptying (no `force_destroy`/`force_delete`) | main.tf, bootstrap/main.tf | Fails safe; documented in checklist phase 11 |
| Medium | Client retries of `POST /orders` can create duplicate orders (no idempotency key) | migration-state-runbook.md §11 | Known limitation; not changed |
| Low | Full AWS account ID will appear in public Actions logs (`mask-aws-account-id` not set) | Task 14 | Optional workflow change |
| Low | Single NAT gateway in AZ A: an AZ A outage stops image pulls/log delivery for tasks in both AZs | network.tf | Cost trade-off (aws-platform.md §7) |
| Low | Alarm email needs confirmation before alarms notify anyone | monitoring.tf | Checklist 4.6 |
| Info | Seeding runs with operator credentials, outside the pipeline | outputs.tf `seed_command`; apply role has no `ecs:RunTask` | By design |

## 3. Local rehearsal coverage (not AWS)

[evidence/task-16-predeployment/03-local-migration-rehearsal.txt](../evidence/task-16-predeployment/03-local-migration-rehearsal.txt):
two production containers sharing DynamoDB Local (22/22 checks), the full test suite on
Linux with DynamoDB Local (84 passed, 2 expected failures), and the hardened-container
smoke test (17/17). It proves application invariants only, not ALB routing, ECS
behaviour or AWS IAM.
