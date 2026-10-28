# Acceptance checklist: EC2 → ECS Fargate migration

Last updated: 28 Oct 2026, after the live migration in AWS. The previous version
(26 Oct 2026, Task 16) was written before deployment and showed "Nothing is deployed in
AWS yet". It is preserved in Git history.

Status values:

- **Implemented only:** code or docs exist, not tested.
- **Verified locally.**
- **Verified in GitHub Actions.**
- **Verified in AWS:** a live demonstration succeeded.
- **Pending:** not done yet.

Live results, run links and commit SHAs are in
[evidence/live-migration-2026-10-28](../evidence/live-migration-2026-10-28/README.md).

| # | Requirement | Implementation | Verified in AWS (28 Oct 2026) | Status | Remaining gap |
|---|---|---|---|---|---|
| 1 | Legacy EC2 baseline | [main.tf](../terraform/main.tf), [user data](../terraform/templates/user_data.sh.tftpl), `app/`, `nginx/`, `systemd/` | EC2 healthy behind the ALB throughout; converted to DynamoDB storage; kept as the rollback target | Verified in AWS | — |
| 2 | Production Docker image | [Dockerfile](../Dockerfile): digest-pinned base, UID 10001, read-only FS, healthcheck | Built and Trivy-gated in CI, pushed to ECR by digest, running on Fargate | Verified in AWS | Unfixed base-image CVEs documented ([security-exceptions.md](security-exceptions.md)) |
| 3 | DynamoDB shared persistence | [storage.py](../app/storage.py), [dynamodb.tf](../terraform/dynamodb.tf) | Order via ECS visible on EC2 and vice versa; stock 98, revenue 59.98; matched by a DynamoDB scan | Verified in AWS | No idempotency key on `POST` |
| 4 | ECR | [bootstrap/main.tf](../terraform/bootstrap/main.tf) | Immutable tags (commit SHA), scan on push, ECS pulls by digest | Verified in AWS | — |
| 5 | ECS Fargate | [ecs.tf](../terraform/ecs.tf) | Service `ACTIVE`, 2/2 tasks, rollouts `COMPLETED` at every stage | Verified in AWS | — |
| 6 | Two availability zones | [network.tf](../terraform/network.tf), [main.tf](../terraform/main.tf) | ECS targets in both private subnets (AZ A and B); ALB nodes in both public subnets | Verified in AWS | Single NAT gateway (AZ A) |
| 7 | Private task networking | `assign_public_ip = false`, private subnets, NAT, gateway endpoints | Tasks registered by private IP only; reachable only through the ALB | Verified in AWS | — |
| 8 | ALB routing and security groups | [alb.tf](../terraform/alb.tf) | Weighted listener and both target groups healthy; `X-Migration-Target` rules (operator `/32` only) reached the intended backend | Verified in AWS | HTTP only (no ACM/HTTPS) |
| 9 | Staged traffic cutover | `traffic_stage` 0–3, listener preconditions, `plan_guard` | 90/10 → 50/50 → 0/100, each one reviewed deploy followed by a 15-minute monitored hold, all PASS | Verified in AWS | — |
| 10 | Rollback | `traffic_stage = 0`; EC2 kept DynamoDB-backed | 100 % ECS → 100 % EC2 (apply about 3 min), then restored to 100 % ECS; no data divergence | Verified in AWS | Weights don't fail over automatically |
| 11 | IAM least privilege and OIDC | [bootstrap policies](../terraform/bootstrap/policies/), [iam.tf](../terraform/iam.tf) | Every deploy used OIDC roles, with no static keys. Two real gaps found and fixed narrowly: the AMI owner alias, and EIP disassociation of a released association | Verified in AWS | — |
| 12 | GitHub CI | [ci.yml](../.github/workflows/ci.yml) | n/a | Verified in GitHub Actions | — |
| 13 | Terraform CI | [terraform-ci.yml](../.github/workflows/terraform-ci.yml) | n/a | Verified in GitHub Actions | — |
| 14 | Pre-commit security | [.pre-commit-config.yaml](../.pre-commit-config.yaml) | n/a | Verified in GitHub Actions | — |
| 15 | Protected deployment | `deploy.yml` apply in `production`; [approval-gate-test.md](approval-gate-test.md) | Every migration deploy paused for `production` approval, then applied the exact saved plan | Verified in AWS | — |
| 16 | CloudWatch logs, alarms and dashboard | [monitoring.tf](../terraform/monitoring.tf) | ECS logs flowing; six alarms `OK` throughout; migration dashboard present | Verified in AWS | No alarm-firing drill; no alarm email subscription configured |
| 17 | Remote state and locking | [backend.tf](../terraform/backend.tf) (`use_lockfile`), state bucket | Every deploy planned and applied against the S3 backend with lock files | Verified in AWS | Bootstrap state is local (backed up) |
| 18 | Architecture diagrams | [docs/architecture/](architecture/README.md) | Updated to the deployed state on 28 Oct 2026, PNGs re-exported | Verified locally | — |
| 19 | Demonstration video | [demo-walkthrough.md](demo-walkthrough.md) | — | Pending | Record before teardown |
| 20 | Safe teardown | [first-deployment-checklist.md, Phase 11](first-deployment-checklist.md#phase-11-teardown-and-cost-check), [aws-platform.md §8](aws-platform.md) | — | Pending | Run after recording, then check billing |
| 21 | Known limitations and cost | [README](../README.md#known-limitations), [aws-platform.md §7, §11](aws-platform.md) | n/a | Documented | — |
