# Acceptance checklist: EC2 → ECS Fargate migration

Last updated: 2026-10-25 (Task 13). One row per mandatory criterion.

Verification levels: **L** = local (tests, scans, mocked Terraform, read-only AWS plan or
simulation), **CI** = GitHub Actions run passed, **AWS** = observed on live AWS resources.
**Verified** means the verification the criterion needs has actually passed. Anything
that needs a running AWS environment stays **In progress** until AWS evidence exists.

CI runs that passed (revision `854d6ef`, before Tasks 11-13):
[CI push](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178212067),
[Terraform CI push](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178212108),
[CI manual](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178748039),
[Terraform CI manual](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178749931).
Changes from Tasks 11-13 have **not** run on GitHub yet.

No AWS resources exist yet: the bootstrap plan (Task 12) is waiting for approval.

| # | Requirement | Implementation | Verification (L / CI / AWS) | Evidence | Status | Remaining gap |
|---|---|---|---|---|---|---|
| 1 | Production Docker image | [Dockerfile](../Dockerfile) (digest-pinned base, non-root 10001, read-only rootfs, `/health` healthcheck, Gunicorn) | L: build, smoke, non-root, Trivy. CI: `container` job passed | [task-04](../evidence/task-04-container/), [task-05](../evidence/task-05-shared-state/), CI runs above | **Verified** (L + CI) | Not yet pushed to ECR or run on Fargate |
| 2 | ECR repository | [bootstrap/main.tf](../terraform/bootstrap/main.tf) (immutable tags, scan on push, lifecycle) | L: mocked tests, Checkov, authenticated plan. AWS: — | [task-12 plan](../evidence/task-12-iam-blockers/02-bootstrap-plan-redacted.txt) | In progress | Bootstrap apply (needs approval); first image push |
| 3 | ECS Fargate in private subnets | [ecs.tf](../terraform/ecs.tf), [network.tf](../terraform/network.tf) (`assign_public_ip = false`, NAT, gateway endpoints) | L: `terraform test` (18), Checkov. CI: Terraform CI passed (pre-Task 11 code). AWS: — | [task-06](../evidence/task-06-terraform/), [task-08](../evidence/task-08-readiness/) | In progress | Platform apply; tasks running in private subnets |
| 4 | Two availability zones | [network.tf](../terraform/network.tf), [main.tf](../terraform/main.tf) (public and private subnet in each of 2 AZs) | L: test `pinned_availability_zones_are_used`. AWS: — | [task-06](../evidence/task-06-terraform/) | In progress | Live tasks and ALB nodes in both AZs |
| 5 | ALB and security groups | [alb.tf](../terraform/alb.tf), [main.tf](../terraform/main.tf) (ALB → tasks only, EC2 egress 80/443, verification routes need operator CIDRs) | L: tests `secure_network_defaults`, `workloads_never_use_the_default_security_group`; Checkov (exceptions B1-B5, B11). AWS: — | [security-exceptions.md](security-exceptions.md), [task-12](../evidence/task-12-iam-blockers/04-checkov.txt) | In progress | Live target health and SG rules |
| 6 | IAM least privilege | [iam.tf](../terraform/iam.tf) (task, execution, EC2 roles); [bootstrap policies](../terraform/bootstrap/policies/) (pipeline roles) | L: 320 CI tests incl. rendered-policy gate, AWS policy simulator 172/172, Access Analyzer 0 findings. AWS: — | [iam-permission-matrix.md](iam-permission-matrix.md), [task-12](../evidence/task-12-iam-blockers/) | In progress | Roles created; first real apply proves no permission is missing |
| 7 | Autoscaling | [ecs.tf](../terraform/ecs.tf) (target tracking, CPU 60 %) | L: mocked plan. AWS: — | — | In progress | Scaling observed under load |
| 8 | GitHub Actions and OIDC | [.github/workflows/](../.github/workflows/), [bootstrap/github_oidc.tf](../terraform/bootstrap/github_oidc.tf) (exact immutable `sub`, no stored keys) | L: workflow policy tests, actionlint. CI: `ci.yml`, `terraform-ci.yml` passed. AWS: — | CI runs above, [ci-cd.md](ci-cd.md) | In progress | OIDC roles not created; `deploy.yml` never run; Task 11-13 changes not yet run in CI |
| 9 | Deployment approval | `deploy.yml` `production` environment (designed); apply role trusts only that environment | — | [iam-permission-matrix.md §6](iam-permission-matrix.md), [ci-cd.md §5](ci-cd.md) | Not started | Private repo on Free has no required reviewers. Owner must choose: public + protected environment, or a separately controlled apply |
| 10 | CloudWatch logs and alarms | [ecs.tf](../terraform/ecs.tf) (`/ecs/legacy-api-production`), [monitoring.tf](../terraform/monitoring.tf) (alarms, SNS, dashboard), VPC flow logs | L: mocked tests, Checkov. AWS: — | [task-06](../evidence/task-06-terraform/) | In progress | Logs flowing and an alarm firing on live resources |
| 11 | EC2 → ECS cutover | Weighted listener `traffic_stage` 0-3, shared DynamoDB state, [migration-state-runbook.md](migration-state-runbook.md), `ci/plan_guard.py` | L: tests `half_split_with_shared_storage`, `full_cutover_and_rollback_weights`, DynamoDB integration tests. CI: test job passed. AWS: — | [task-05](../evidence/task-05-shared-state/), [task-07](../evidence/task-07-cicd/) | In progress | Live staged traffic shift with `verify_rollout.py` |
| 12 | Rollback | `traffic_stage` back to 0 (EC2 stays the rollback target), [ci-cd.md §8](ci-cd.md) | L: tests `full_cutover_and_rollback_weights`, `rollback_to_dynamodb_backed_ec2`. AWS: — | [task-06](../evidence/task-06-terraform/) | In progress | Rollback drill on AWS with timings |
| 13 | Architecture diagram | — ([ARCHITECTURE.md](../ARCHITECTURE.md) describes the original EC2 app) | — | — | Not started | Diagram of the target ECS architecture |
| 14 | Documentation and evidence | [docs/](.), [evidence/](../evidence/), this checklist | L: evidence files from real runs (redacted). CI: links above | [evidence/](../evidence/) | In progress | AWS runtime evidence; final README for the migrated platform |
| 15 | Safe teardown | [aws-platform.md §8](aws-platform.md) (order, deletion protection, state bucket `prevent_destroy`) | — | — | Not started | Teardown run and confirmed on AWS (costs stopped, state kept or removed deliberately) |

Shift-left checks (Task 13): [.pre-commit-config.yaml](../.pre-commit-config.yaml), run
locally and in the CI lint job; verified locally (positive and negative tests), not yet
on GitHub.
