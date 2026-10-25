# Task 6 evidence: Terraform ECS Fargate platform (offline only)

Captured on 2026-10-24. **No AWS account was used**: no credentials, no backend, no
`terraform plan` against AWS, nothing applied. Terraform commands ran with invalid dummy
credentials as a safety net.

| File | What it shows |
|---|---|
| 01-fmt-validate-test.txt | `fmt -check`, `init -backend=false` and `validate` for both roots; multi-platform provider lock files; `terraform test` (11 runs, MOCKED providers) |
| 02-checkov.txt | Full Checkov 3.3.26 output, nothing skipped (triage: docs/aws-platform.md section 9) |
| 03-ec2-dynamodb-bootstrap.txt | Real rendered user data + real Terraform app bundle on Ubuntu 24.04 with DynamoDB Local: fails safely when unseeded, succeeds when seeded, shares state with an ECS-style container; memory-mode regression |

What `terraform test` proves (mocked providers): default stage sends 100% to EC2;
no ECS service without an image digest; the ECS task is Fargate in private subnets with no
public IP, read-only, UID 10001, `drop ALL`, digest-pinned and `STORAGE_BACKEND=dynamodb`;
traffic to ECS is refused while EC2 is still in memory mode or without an image; tags
instead of digests are rejected; stage weights 90/10, 50/50, 0/100 and rollback.

What it does NOT prove: AWS-side behaviour (IAM evaluation, quotas, Fargate runtime,
health checks). That needs a real plan/apply after approval.
