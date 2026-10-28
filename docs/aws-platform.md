# AWS platform: Terraform structure, deployment order, cost and teardown

Status: **deployed in AWS (eu-west-2) and verified on 28 October 2026, then decommissioned
the same day.** The last production configuration was `traffic_stage = 3` (EC2 0 % /
ECS 100 %), with the legacy EC2 kept as the rollback target. Results:
[evidence/live-migration-2026-10-28](../evidence/live-migration-2026-10-28/README.md).
Teardown: [§8](#8-teardown-do-not-run-until-migration-acceptance) and
[evidence/aws-teardown-2026-10-28](../evidence/aws-teardown-2026-10-28/README.md). This
document describes the architecture that was deployed; none of it is running now.
The design sections below were written before deployment; where the live result differs, it
is noted inline. The ALB is **HTTP only**, with no Route 53 record or ACM certificate.
Data handling and cutover rules are in [migration-state-runbook.md](migration-state-runbook.md).

## 1. Terraform structure and state ownership

```
terraform/
  bootstrap/            separate root, LOCAL state at first
    main.tf             S3 state bucket, ECR repository
    github_oidc.tf      GitHub OIDC provider + plan / image-push / apply roles
  main.tf               legacy EC2, VPC, IGW, public subnet a, app bundle (existing)
  network.tf            public subnet b, private subnet b, NAT, private routes, gateway endpoints
  dynamodb.tf           shared application table
  iam.tf                DynamoDB policies, ECS execution / task / seed roles
  alb.tf                ALB, target groups (EC2 + ECS), weighted listener
  ecs.tf                cluster, task definitions (app + seed), service, autoscaling
  monitoring.tf         SNS topic, alarms, dashboard
  backend.tf            S3 backend with use_lockfile (partial config)
  templates/user_data.sh.tftpl
  tests/platform.tftest.hcl   offline tests with mocked providers
```

Why two roots:
- **`bootstrap/`** creates the state bucket, so it cannot itself start with that bucket as
  its backend. It also holds the pipeline's own permissions, which the pipeline must never
  be able to change.
- **`terraform/`** keeps the legacy EC2 and adds the platform as separate files in the
  same state. The weighted ALB listener must reference both the EC2 instance and the ECS
  target group, and both share one VPC. Separate roots would need cross-state lookups and
  would risk two roots owning the same VPC.

There is no existing state (nothing has been applied), so nothing needs migrating.

Every existing resource keeps its address, and no existing argument that forces
replacement was changed. Changes to legacy resources:
- **EC2 security group:** a new ingress rule from the ALB (in-place update).
- **EC2 role:** a new DynamoDB policy.
- **App bucket:** a new lifecycle rule.
- **User data:** moved to a template. It renders byte-identical text in memory mode
  (same SHA-256).
- **EC2 lifecycle:**
  - `ignore_changes = [ami]`, so a new Ubuntu AMI cannot replace the rollback target.
  - The instance is replaced *only* when `legacy_storage_backend` changes, and the plan
    shows it.

## 2. Network and AZ layout

| | AZ a | AZ b |
|---|---|---|
| Public | 10.0.1.0/24 (existing): legacy EC2, NAT gateway, ALB | 10.0.3.0/24: ALB |
| Private | 10.0.2.0/24 (existing): ECS tasks | 10.0.4.0/24: ECS tasks |

**Traffic paths:**
- Internet → ALB (port 80) → EC2 Nginx (port 80) or ECS tasks (port 5000).
- ECS tasks have no public IP:
  - ECR API and CloudWatch Logs via the NAT gateway
  - image layers (S3) and DynamoDB via free gateway endpoints

**Security groups:**

| Group | Inbound | Outbound |
|---|---|---|
| ALB | 80 from `alb_ingress_cidr_blocks` | only EC2:80 and ECS:5000 |
| ECS | 5000 from the ALB group only | 443 |
| EC2 | 80 from the ALB group only. Direct path via `allowed_cidr_blocks`: default `[]` (closed); 0.0.0.0/0 is rejected | TCP 80/443 only |

VPC flow logs record **rejected** traffic to CloudWatch (30 days).

DynamoDB needs no inbound rules: it is an outbound HTTPS call through the endpoint. The
DynamoDB endpoint policy only allows the application table.

### NAT gateway vs VPC endpoints

| Option | Monthly (eu-west-2, approx.) | Notes |
|---|---|---|
| **A. 1 NAT + free S3/DynamoDB gateway endpoints (chosen)** | ~$37 + $0.05/GB | Simple; also gives tasks general outbound access |
| B. Interface endpoints (ecr.api, ecr.dkr, logs) in 2 AZs + gateway endpoints | ~$48 + $0.01/GB | No NAT, but more resources and higher fixed cost |

The single NAT gateway is in AZ a. If AZ a fails, tasks in AZ b keep serving requests:
DynamoDB uses the gateway endpoint, not the NAT. But new tasks cannot pull images and logs
cannot be shipped until AZ a recovers. That is an accepted trade-off for a demo or
assessment. Production would use one NAT per AZ (about +$37/month) or option B.

## 3. ECS service

**Task:**
- Fargate 1.4.0, x86_64, 0.25 vCPU / 0.5 GB per task, `WEB_CONCURRENCY=2`.
- 2 tasks across both private subnets, autoscaling 2–4 on average CPU (target 60%).

**Container:**
- Image `ECR@sha256:…`: an immutable digest, verified in ECR by a data lookup.
- UID 10001, read-only root filesystem, all capabilities dropped, `stopTimeout` 30 s.
- Logs via `awslogs` (non-blocking mode).
- Health check on `/health` (liveness). The ALB uses `/ready` (DynamoDB reachable and seeded).
- Environment always includes `STORAGE_BACKEND=dynamodb`, `DYNAMODB_TABLE_NAME` (from
  Terraform) and `AWS_REGION`.

**Deployment:**
- Rolling: 100% minimum, 200% maximum.
- Circuit breaker with automatic rollback; 30 s health-check grace.
- Deregistration delay 30 s.

**Roles:**
- **Execution role:** pull from the one repository; write to the one log group.
- **Task role:** DynamoDB items only.
- **Seed role:** used only by the one-off seed task.

The service is created only after `app_image_digest` is set. Before that, the cluster,
ALB, table and IAM exist without a service.

## 4. IAM summary

| Principal | Permissions |
|---|---|
| App: ECS task role, EC2 instance role | `GetItem`, `Query` on PRODUCT/ORDER/SEQUENCE; `UpdateItem` on PRODUCT/SEQUENCE; `PutItem` on ORDER. All on the table ARN, limited with `dynamodb:LeadingKeys` |
| Seed task role | `PutItem` on PRODUCT/SEQUENCE |
| ECS execution role | `ecr:GetAuthorizationToken`; pull from the one repository; `logs:CreateLogStream` and `PutLogEvents` on the one log group |
| `gha-legacy-api-plan` | Describe/List of platform services, read project IAM, read state, write the state lock file |
| `gha-legacy-api-image-push` | Push to the one ECR repository |
| `gha-legacy-api-apply` | Plan rights, plus write access to the platform services in the chosen region; IAM only on `legacy-api-*`; `PassRole` only to ECS tasks and EC2; cannot change the state bucket's policy or versioning |

**GitHub trust (StringEquals, exact repository, no wildcards):**
- plan role: `pull_request` and `refs/heads/main`
- image-push role: `refs/heads/main`
- apply role: GitHub Environment `production` (add required reviewers)

## 5. Ownership: Terraform vs GitHub Actions

Terraform owns everything: task definitions, service, listener weights, autoscaling.
GitHub Actions only builds and pushes images and runs `terraform plan` / `apply` with the
saved plan. Migration settings live in the committed
`terraform/environments/production.tfvars`; an `application` deployment refuses any
plan that changes them (`ci/plan_guard.py`). Details: [ci-cd.md](ci-cd.md).

## 6. Deployment order

The full, approval-gated sequence is in [ci-cd.md](ci-cd.md) section 7.
**No `-target` is needed**: `ecs_service_enabled = false` on the first deployment
creates the task definitions (including the one-off seed task) but no service. Once the
seed has run, `ecs_service_enabled = true` creates the service.

Pre-cutover comparison of both backends uses the header routes. They exist only when
`verification_cidr_blocks` (operator IPs) is set, and only for requests **from those IPs**:
```
curl -H "X-Migration-Target: ecs" <alb_url>/ready
curl -H "X-Migration-Target: ec2" <alb_url>/api/v1/products
```
Without the source-IP restriction, any internet client could bypass the staged rollout
and, before EC2 uses DynamoDB, write orders into the shared table.

### Moving clients from the EC2 address to the ALB

Today clients use the Elastic IP (`application_url`). At stage 0 the ALB sends 100% to the
same EC2, so moving clients to the ALB changes nothing for them:
1. Publish `alb_url`. With a Route 53 record, lower its TTL to 60 s a day ahead, then
   change the record to an alias of the ALB. This is a future Terraform change. **Not done
   in this deployment:** there is no domain, and clients use the ALB's HTTP hostname.
2. Keep the Elastic IP path working (`allowed_cidr_blocks`) until old clients have moved.
3. Only then start weighted stages, and finally set `allowed_cidr_blocks = []`.

## 7. Indicative cost (eu-west-2 London, on-demand, 730 h/month)

Prices are from AWS public price lists as known at the time of writing. **Verify them in
the AWS Pricing Calculator before applying.**

| Item | Assumption | $/month | $/day |
|---|---|---|---|
| Legacy EC2 t3.micro | 24/7 | 8.61 | 0.28 |
| EBS gp3 20 GB | | 1.86 | 0.06 |
| Public IPv4 | EC2 EIP + NAT EIP + 2 ALB IPs, $0.005/h each | 14.60 | 0.48 |
| ECS Fargate | 2 tasks × (0.25 vCPU + 0.5 GB) | 20.72 | 0.68 |
| ALB | $0.02646/h + ~0.5 LCU average | 22.39 | 0.74 |
| NAT gateway | $0.050/h + ~2 GB processed | 36.60 | 1.20 |
| DynamoDB | on-demand, PITR, tiny data | < 0.50 | < 0.02 |
| CloudWatch | ~0.5 GB logs, REJECT-only VPC flow logs (< $1), 6 alarms, Container Insights, 1 dashboard* | 4–13 | 0.13–0.43 |
| ECR | ~0.1 GB | < 0.05 | — |
| S3 (state + bundles) | versioned, KBs to MBs | < 0.10 | — |
| **Total** | | **≈ 110–118** | **≈ 3.6–3.9** |

\* Dashboards are free for the first three in an account. Container Insights is the most
uncertain line; disable it with `enable_container_insights = false` to save cost, at the
loss of the running-vs-desired metric and alarm.

What this means:
- A 7-day assessment window costs roughly **$25–30**.
- The NAT gateway, ALB and public IPv4 addresses (≈ $73) are the fixed floor of a
  two-AZ private design.
- The legacy baseline alone is about $14/month.
- Autoscaling to 4 tasks adds about $21/month while scaled out.

## 8. Teardown (do not run until migration acceptance)

**Executed on 28 October 2026.** The steps below are the original plan. The actual
sequence was:

1. a targeted protection-disable plan (2 changed);
2. the bundle bucket emptied by version ID;
3. the platform destroy (70 destroyed);
4. the ECR images deleted, then a targeted bootstrap plan (16 destroyed);
5. the Container Insights log group deleted separately.

The state bucket and the shared GitHub OIDC provider were kept. There was no Stage 0
deploy and no blanket bootstrap destroy. Details and the deviations from this plan:
[first-deployment-checklist.md, Executed teardown](first-deployment-checklist.md#executed-teardown-28-october-2026).

Original plan:

1. `traffic_stage = 0`. Export the table if the data matters:
   `aws dynamodb scan`, or an on-demand backup.
2. Set `dynamodb_deletion_protection = false` and `alb_deletion_protection = false`
   (both default to true), then apply.
3. `terraform destroy` in `terraform/`. This removes the ECS service, ALB, NAT, endpoints,
   table (and its PITR history), the EC2 instance, the Elastic IPs and the bundle bucket.
   The bucket is versioned; if destroy reports it is not empty, delete all object
   versions first.
4. In `terraform/bootstrap/`, delete the images in ECR first
   (`aws ecr batch-delete-image`; `force_delete` is off on purpose).
5. The state bucket has `prevent_destroy`. To remove it:
   - delete the lifecycle block
   - empty all object versions
   - `terraform destroy`

   Otherwise, keep it as an audit trail; it costs cents.
6. Check in the console that no Elastic IPs, NAT gateways or load balancers remain.

## 9. Security scan

Checkov 3.3.26 is a **blocking** gate in `terraform-ci.yml`: **334 passed, 0 failed,
30 reviewed inline exceptions.** In Task 7, 3 of the earlier 31 findings were fixed
outright: VPC flow logs, ALB deletion protection on by default, and EC2 egress limited
to 80/443. The direct EC2 path is closed by default and cannot be opened to 0.0.0.0/0.
Each exception, with class, reason, mitigation and revisit condition, is in
[security-exceptions.md](security-exceptions.md).

## 10. Decisions needed before real provisioning (answered before the 27 Oct 2026 deployment)

1. AWS region (examples use `eu-west-2`) and the two AZs to pin.
2. GitHub owner and repository name; create the `production` Environment with required
   reviewers. Does the account already have a GitHub OIDC provider?
3. Budget approval (about $3.7/day) and a budget alarm.
4. Your public IP for `allowed_cidr_blocks` (and SSH, if wanted).
5. An alarm email address.
6. Domain and ACM certificate: yes or no (HTTP-only demo otherwise).
7. Approval of the single-NAT trade-off and of Container Insights cost.
8. Who approves the write freeze and the EC2 replacement (step 7).

## 11. Known technical risks

- ~~No real `terraform plan` yet.~~ **Resolved:** every deploy used a real saved plan,
  reviewed and then approved (Oct 27–28, 2026).
- **IAM `dynamodb:LeadingKeys` with `TransactWriteItems`:** documented as supported but
  untested against real IAM. If writes are denied, the first order will fail with a 500.
  Check right after step 5. **Verified 28 Oct 2026:** orders written through both ECS and EC2
  succeeded.
- **Fargate and read-only root:** Gunicorn's `--worker-tmp-dir /dev/shm` relies on Fargate
  providing a writable `/dev/shm`. **Verified on Fargate:** tasks healthy at 100 % traffic.
- **Weighted routing has no automatic failover:** if ECS targets are unhealthy, their share
  still goes to them. Mitigated by the unhealthy-target alarm and the rollback to stage 0.
- **Single NAT** (section 2); **single-instance legacy EC2** (no high availability for the
  rollback target).
- **Bootstrap state** is local until migrated; keep it safe.
