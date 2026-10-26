# Acceptance checklist: EC2 → ECS Fargate migration

Last updated: 26 Oct 2026 (Task 16). **Nothing is deployed in AWS yet.**

Status values: **Implemented only** (code/docs exist, not tested) · **Verified locally**
· **Verified in GitHub Actions** · **Verified in AWS** (live demonstration succeeded) ·
**Not yet tested** · **Blocked**. A requirement that needs a running AWS environment is
never shown as Verified in AWS until that demonstration has happened.

GitHub Actions runs (all passed):

| Commit | CI | Terraform CI |
|---|---|---|
| `9dd04a4` (IAM hardening, policy gate, pre-commit) | [run 40304611511](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40304611511) | [run 40304611545](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40304611545) |
| `854d6ef` (push) | [run 40178212067](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178212067) | [run 40178212108](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178212108) |
| `854d6ef` (manual) | [run 40178748039](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178748039) | [run 40178749931](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40178749931) |

Task 16 changes (approval-gate workflow, workflow privilege tests, rehearsal script,
diagrams, runbooks) are **not yet pushed**, so they are verified locally only.

| # | Requirement | Implementation | Local | GitHub Actions | AWS | Evidence | Status | Remaining gap |
|---|---|---|---|---|---|---|---|---|
| 1 | Legacy EC2 baseline | [main.tf](../terraform/main.tf), [user data](../terraform/templates/user_data.sh.tftpl), `app/`, `nginx/`, `systemd/` | Tests, user-data render | Terraform CI (render + shellcheck) | – | [task-06](../evidence/task-06-terraform/) | Verified in GitHub Actions | Running instance on AWS |
| 2 | Production Docker image | [Dockerfile](../Dockerfile): digest-pinned base, UID 10001, read-only FS, healthcheck | Build, smoke 17/17, rehearsal | CI `container` job, Trivy gate | – | [task-04](../evidence/task-04-container/), [task-16](../evidence/task-16-predeployment/03-local-migration-rehearsal.txt) | Verified in GitHub Actions | Pushed to ECR, running on Fargate |
| 3 | DynamoDB shared persistence | [storage.py](../app/storage.py), [dynamodb.tf](../terraform/dynamodb.tf) | 20 DynamoDB Local tests; 2-container rehearsal 22/22 | CI test job with DynamoDB Local | – | [task-05](../evidence/task-05-shared-state/), [task-16](../evidence/task-16-predeployment/) | Verified in GitHub Actions | Real table and IAM (LeadingKeys) in AWS |
| 4 | ECR | [bootstrap/main.tf](../terraform/bootstrap/main.tf) | Mocked tests, read-only plan | Terraform CI | – | [task-12 plan](../evidence/task-12-iam-blockers/02-bootstrap-plan-redacted.txt) | Implemented only (plan reviewed) | Bootstrap apply; first push |
| 5 | ECS Fargate | [ecs.tf](../terraform/ecs.tf) | Mocked tests (18) | Terraform CI, Checkov | – | [task-06](../evidence/task-06-terraform/) | Verified in GitHub Actions (code) | Service running |
| 6 | Two availability zones | [network.tf](../terraform/network.tf), [main.tf](../terraform/main.tf) | `pinned_availability_zones_are_used` | Terraform CI | – | — | Verified in GitHub Actions (code) | Tasks/ALB in both AZs |
| 7 | Private task networking | `assign_public_ip = false`, private subnets, NAT, gateway endpoints | `workloads_never_use_the_default_security_group` | Terraform CI | – | [readiness review](deployment-readiness-review.md) | Verified in GitHub Actions (code) | Live check |
| 8 | ALB routing + security groups | [alb.tf](../terraform/alb.tf) | `secure_network_defaults`, weight tests | Terraform CI, Checkov | – | [security-exceptions.md](security-exceptions.md) | Verified in GitHub Actions (code) | Live target health |
| 9 | Staged traffic cutover | `traffic_stage` 0-3, listener preconditions, plan_guard | Mocked weight tests; rehearsal (shared state) | Terraform CI | – | [migration diagram](architecture/migration-traffic-flow.png) | Verified locally (logic only) | Live 90/10 → 50/50 → 0/100 |
| 10 | Rollback | `traffic_stage = 0`; EC2 kept DynamoDB-backed | Rollback weight tests; simulated rollback in rehearsal | Terraform CI | – | [task-16](../evidence/task-16-predeployment/03-local-migration-rehearsal.txt) | Verified locally (simulation) | Live rollback drill with timing |
| 11 | IAM least privilege + OIDC | [bootstrap policies](../terraform/bootstrap/policies/), [iam.tf](../terraform/iam.tf) | Policy gate, AWS simulator 172/172, Access Analyzer 0 findings | CI pytest (policy gate) | – | [iam-permission-matrix.md](iam-permission-matrix.md), [task-12](../evidence/task-12-iam-blockers/) | Verified in GitHub Actions (policy text) | Roles created; first real apply |
| 12 | GitHub CI | [ci.yml](../.github/workflows/ci.yml) | All gates | Runs above | n/a | Runs above | Verified in GitHub Actions | — |
| 13 | Terraform CI | [terraform-ci.yml](../.github/workflows/terraform-ci.yml) | fmt/validate/test, Checkov | Runs above | n/a | Runs above | Verified in GitHub Actions | — |
| 14 | Pre-commit security | [.pre-commit-config.yaml](../.pre-commit-config.yaml) | Positive + 8 negative tests | CI lint job (`9dd04a4`) | n/a | [task-13](../evidence/task-13-precommit/) | Verified in GitHub Actions | — |
| 15 | Protected deployment | `deploy.yml` apply in `production`; [approval-gate-test.yml](../.github/workflows/approval-gate-test.yml); privilege tests on every workflow | 21 unsafe variants + 1 real file rejected | – (not pushed) | n/a | [approval-gate-test.md](approval-gate-test.md), [task-16](../evidence/task-16-predeployment/02-workflow-policy-negative-tests.txt) | Configured (owner, API read-back); runtime **Not yet tested** | Tests 1-3 on GitHub |
| 16 | CloudWatch logs, alarms, dashboard | [monitoring.tf](../terraform/monitoring.tf), log groups in ecs.tf/network.tf | Mocked tests | Terraform CI | – | — | Verified in GitHub Actions (code) | Logs flowing; alarm firing; dashboard screenshot |
| 17 | Remote state + locking | [backend.tf](../terraform/backend.tf) (`use_lockfile`), state bucket | Bootstrap tests | Terraform CI | – | [first-deployment-checklist.md §3](first-deployment-checklist.md) | Implemented only | Lock test against the real bucket |
| 18 | Architecture diagrams | [docs/architecture/](architecture/) (editable `.drawio` + PNG) | XML validated; rendered with draw.io | n/a | n/a | [README](architecture/README.md) | Verified locally | Update after deployment if anything changes |
| 19 | Demonstration video | – | – | – | – | – | Not yet tested | Record after the AWS demo |
| 20 | Safe teardown | [first-deployment-checklist.md §11](first-deployment-checklist.md), [aws-platform.md §8](aws-platform.md) | – | – | – | – | Implemented only | Teardown run + billing check |
| 21 | Known limitations and cost | [aws-platform.md §7, §11](aws-platform.md), [readiness review](deployment-readiness-review.md) | n/a | n/a | n/a | — | Documented | Confirm prices in the AWS Pricing Calculator |

Blocked items: none at code level. Process gates before AWS: approval-gate runtime
test, independent review and explicit approval of the 23-resource bootstrap plan
(publication permission from CoderCo is tracked separately in
[public-release-and-approval.md](public-release-and-approval.md)).
