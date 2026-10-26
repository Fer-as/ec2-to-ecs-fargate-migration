# Security exceptions register

Reviewed exceptions to the blocking security gates. Each Checkov exception is an
inline `#checkov:skip=<rule>:<reason>` comment on the **exact resource**. Nothing is
skipped globally, and CI fails on any new finding (proven by a negative test: adding
an SSH-open security group makes Checkov exit 1).

**Gate result (Checkov 3.3.26, Task 12): 285 passed, 0 failed, 30 skipped (exceptions below).**
The pass count fell from 334 (Task 7) because the pipeline IAM policies moved from
`aws_iam_policy_document` data sources to JSON templates (`terraform/bootstrap/policies/`);
Checkov now runs only CKV_AWS_62/63 on them. Checkov does not see the rendered policies.
They are gated instead by `ci/tests/test_rendered_policy_gate.py` and
`ci/tests/test_iam_policies.py` (blocking in ci.yml) and the AWS policy-simulator run in
[iam-permission-matrix.md](iam-permission-matrix.md).
Review this register whenever a "Revisit when" condition becomes true, and before real
production traffic.

Classes: **A** = genuine defect, fixed. **B** = narrow architectural exception.
**C** = false positive, with technical justification.

## 1. Task 7 reassessment of the 31 Task 6 findings

| Outcome | Rules |
|---|---|
| **A, fixed (no exception needed)** | CKV2_AWS_11: VPC flow logs added (REJECT traffic, 30-day retention). CKV_AWS_150: ALB deletion protection now on by default. CKV_AWS_382: legacy EC2 egress limited to TCP 80/443 |
| **A + C** | CKV_AWS_260 on `aws_security_group.ec2`: direct port 80 closed by default (`allowed_cidr_blocks = []`), and validation now rejects 0.0.0.0/0. The remaining report is a Checkov rendering artefact (row C1) |
| **B / C** | The other 27 findings, below. Two more rows (flow-log group) come from the A fix |

## 2. Checkov exceptions

| # | Rule | Resource | Class | Reason | Mitigation | Revisit when |
|---|---|---|---|---|---|---|
| B1 | CKV2_AWS_20, CKV_AWS_2, CKV_AWS_103 | `aws_lb.app`, `aws_lb_listener.http` | B | No domain or ACM certificate, so HTTP only | Demo data only; no credentials or PII in the API | A domain exists: add HTTPS listener (TLS 1.2+) and HTTP→HTTPS redirect |
| B2 | CKV_AWS_378 | `aws_lb_target_group.ecs`, `.ec2` | B | ALB → target HTTP stays inside the VPC; app and Nginx speak HTTP | Targets only reachable from the ALB security group | End-to-end TLS required by policy |
| B3 | CKV2_AWS_28 | `aws_lb.app` | B | WAF costs ~$5+/month plus rules | Small API; input validation; ALB drops invalid headers | Before real production traffic |
| B4 | CKV_AWS_91 | `aws_lb.app` | B | Access logs need a log bucket with an ELB bucket policy | ALB 4xx/5xx/target metrics, alarms, dashboard; VPC flow logs | Before real production traffic |
| B5 | CKV_AWS_260 | `aws_vpc_security_group_ingress_rule.alb_http` | B | The ALB is the intended public entry point | Only port 80; ALB security group egress limited to the targets | HTTPS added (port 443 instead) |
| B6 | CKV_AWS_119, CKV_AWS_136, CKV_AWS_145, CKV_AWS_158 | `aws_dynamodb_table.app` (119), `aws_ecr_repository.app` (136), `aws_s3_bucket.state` and `.app` (145), `aws_cloudwatch_log_group.app` and `.vpc_flow_logs` (158) | B | AWS-managed encryption at rest is used; customer-managed KMS keys add cost and key-policy work | Encryption at rest is on everywhere; IAM limits access | Compliance requires customer-managed keys |
| B7 | CKV_AWS_338 | `aws_cloudwatch_log_group.app`, `.vpc_flow_logs` | B | 30-day retention (`log_retention_days`) for cost | Configurable up to 90 days | Audit retention requirement |
| B8 | CKV_AWS_18, CKV_AWS_144, CKV2_AWS_62 | `aws_s3_bucket.state`, `aws_s3_bucket.app` | B | Server access logs, cross-region replication and event notifications are not needed: state is versioned; the bundle is rebuilt from Git | TLS-only state bucket; public access blocked; versioning; CloudTrail | Multi-region requirement |
| B9 | CKV_AWS_126, CKV_AWS_130 | `aws_instance.app`, `aws_subnet.public` | B | Legacy baseline kept unchanged to protect the rollback target; the bootstrap needs a public IP before the Elastic IP attaches | ALB target-health alarms; EC2 security group allows only the ALB by default | EC2 decommissioned after migration acceptance |
| B10 | CKV_AWS_394 | `data.aws_availability_zones.available` | B | Only a fallback | `availability_zones` pins the AZs (repository variable `TF_AVAILABILITY_ZONES`) | — |
| B11 | CKV2_AWS_12 | `aws_vpc.main` | B | Task 12: the VPC's default security group is no longer adopted (`aws_default_security_group` removed). Adopting it needs Revoke/Authorize/CreateTags on an **untagged** security group, and IAM has no condition key that limits that to this VPC (`ec2:Vpc` needs the VPC ARN, unknown before creation), so the apply role would have had that access on every untagged SG in eu-west-2 | The default SG keeps AWS defaults (self-referencing ingress, all egress) but nothing uses it: ALB, ECS tasks and EC2 name their own SGs (`terraform test workloads_never_use_the_default_security_group`); endpoints are gateway type; the apply role cannot launch into an untagged SG | Strip the default SG rules once, manually by the account owner after the VPC exists, if AWS Security Hub EC2.2 compliance is required |
| C1 | CKV_AWS_260 | `aws_security_group.ec2` | C | Checkov renders the *empty* dynamic HTTP block as a rule with no source. Terraform creates no such rule | `terraform test secure_network_defaults`; validation rejects 0.0.0.0/0 | Checkov fixes dynamic-block rendering |
| C2 | CKV_AWS_24 | `aws_security_group.ec2` | C | SSH exists only if `ssh_allowed_cidr_blocks` is set; validation rejects 0.0.0.0/0 | Default: no SSH rule | — |
| C3 | CKV_AWS_135 | `aws_instance.app` | C | t3 instance types are EBS-optimised by default | — | Instance type changes |
| C4 | CKV_AWS_332 | `aws_ecs_service.app` | C | Pinned to 1.4.0 (the current latest Linux platform version) for reproducibility, not "LATEST" | Review on AWS platform-version announcements | AWS releases a newer version |
| C5 | (retired in Task 11) | was `aws_iam_policy_document.platform_read` | C | The read policy is now `policies/platform-read.json.tftpl`; its `Resource "*"` Describe/List/Get statement is explained in iam-permission-matrix.md | Read-only; the plan role is trusted only for main | — |

## 3. Trivy: unfixed vulnerabilities (risk accepted, re-checked on every build)

The blocking gate fails on **fixable** HIGH/CRITICAL vulnerabilities and on any secret
(`--ignore-unfixed --severity HIGH,CRITICAL --exit-code 1`). Unfixed findings are
reported in the job summary and the `trivy-reports` artifact.

Current state of `python:3.12.15-slim-trixie`: 0 CRITICAL; 8 HIGH Debian CVEs, none with
a fix (0 Python package findings).

| CVE | Packages | Exposure in this image |
|---|---|---|
| CVE-2026-76642, -78408, -78409, -78410 | util-linux family (mount, libmount, login, …) | mount/namespace tools; not executed by the app; need privileges the container does not have (non-root, no capabilities, no-new-privileges) |
| CVE-2026-16742 | libsystemd0, libudev1 | systemd-homed path; no systemd in the container |
| CVE-2026-54369 | libacl1 | ACL symlink traversal; read-only root filesystem, non-root |
| CVE-2025-69720 | ncurses | terminal library; no interactive terminals |
| CVE-2026-9538 | perl-base (fix deferred) | Archive::Tar DoS; Perl is not used by the app |

**Revisit when:** Debian publishes fixes (the gate then forces a rebuild), or before
production. Option: a distroless/minimal base image.

## 4. gitleaks allowlist

`.gitleaks.toml` keeps **all default gitleaks rules**. It adds one exact-string allowlist
entry, `GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305`:
- **What it is:** the public CPython release-signing key fingerprint, set as an
  environment variable by the official `python` Docker image.
- **Where it appears:** captured container evidence
  (`evidence/task-05-shared-state/04-docker-final-image.txt`).
- **Why it matters:** gitleaks' generic-api-key rule flags it, which would fail the CI
  `secrets` job on the first push.

Verified: a fake AWS key pair and a *different* `GPG_KEY` value are still detected
(3 findings, exit 1).
