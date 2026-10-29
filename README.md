# EC2 to ECS Fargate Migration — Terraform, GitHub Actions and DynamoDB

A legacy Flask API ran on **one EC2 instance with in-memory data**. It had no scaling,
no CI/CD and lost its data on every restart. This project moved it to **Amazon ECS
Fargate**, and the migration was **run live in AWS (`eu-west-2`) on 28 October 2026**:

1. **Shared state first:** both backends were moved onto one DynamoDB table, so traffic
   could be split safely.
2. **Progressive cutover:** an ALB moved traffic **90/10 → 50/50 → 0/100**. Each stage
   was one Terraform change, deployed through GitHub Actions with OIDC and a manually
   approved saved plan, then held under CloudWatch monitoring.
3. **Rollback tested:** traffic went back to EC2 on purpose, then was restored to ECS,
   through the same reviewed pipeline.
4. **Teardown:** afterwards, the project's AWS resources were removed with reviewed
   destroy plans. Only a small private Terraform state bucket was kept.

| What | Result | Proof |
|---|---|---|
| Final migrated state (before teardown) | EC2 0 % / ECS 100 %, 2 Fargate tasks in 2 AZs, EC2 healthy as the rollback target | [Final restoration](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980) |
| Progressive cutover | 600/600, 390/390 and 350/350 sampled HTTP 200 | [Stage runs](#migration-results) |
| Rollback and restoration | 300/300 via EC2, then 350/350 via ECS | [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690), [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980) |
| Shared data | An order through each backend was visible through the other; stock 98, revenue 59.98 | [DynamoDB test](evidence/live-migration-2026-10-28/README.md#shared-dynamodb-test) |
| Monitoring | Six alarms `OK`, 0 × 5xx, no unhealthy targets at every observation | [Monitoring results](evidence/live-migration-2026-10-28/README.md#monitoring-results) |
| Teardown | 70 platform and 16 bootstrap resources destroyed by reviewed plans | [Teardown record](evidence/aws-teardown-2026-10-28/README.md) |

**All proof, grouped by capability:** [evidence index](evidence/README.md) ·
**Why it was built this way:** [engineering decisions](docs/engineering-decisions.md)

> **Deployment status: decommissioned.**
>
> - The migration was completed on 28 October 2026. The rollback and the final
>   restoration were tested successfully.
> - [Watch the recorded migration walkthrough on YouTube](https://youtu.be/InSQNVuTTlw)
>   (captured before teardown).
> - All AWS resources were deliberately decommissioned on 28 October 2026 to control
>   costs, through reviewed, saved Terraform plans
>   ([teardown record](evidence/aws-teardown-2026-10-28/README.md)).
> - The live ALB URL and the AWS console links in this repository no longer work.
> - The source code, Terraform, architecture diagrams, GitHub Actions history and
>   sanitised evidence remain available.
> - The [Deploy workflow](.github/workflows/deploy.yml) is kept as source but disabled.

## Contents

1. [Project overview](#project-overview)
2. [Before and after](#before-and-after)
3. [Architecture](#architecture)
4. [Migration strategy](#migration-strategy)
5. [Migration results](#migration-results)
6. [Shared DynamoDB](#shared-dynamodb)
7. [CI/CD and security](#cicd-and-security)
8. [Testing, monitoring and evidence](#testing-monitoring-and-evidence)
9. [Rollback](#rollback)
10. [Engineering decisions](#engineering-decisions)
11. [Known limitations](#known-limitations)
12. [Teardown](#teardown-completed-28-october-2026)
13. [Demo video](#demo-video)

## Project overview

**Starting point.** The original CoderCo application was a Flask API served by
Gunicorn behind Nginx on one EC2 instance. It kept its data in memory, and all traffic
went to that one instance's Elastic IP. That meant:

- no high availability or horizontal scaling;
- no CI/CD;
- logs only on the instance;
- data lost on every restart.

**Problem.** Move the service to containers without downtime, without losing orders,
and with a rollback path at every step.

**Solution.**

1. Containerise the app and give both backends one shared DynamoDB store.
2. Put an ALB with weighted target groups in front of both the legacy EC2 instance and
   an ECS Fargate service.
3. Shift traffic in reviewed stages. Each stage is one line of Terraform, deployed
   through a pipeline that needs a human to approve the exact saved plan.

**Measured result.**

- Every stage passed a monitored hold of at least 15 minutes.
- 1,990 sampled requests returned HTTP 200 across all observations.
- There were no 5xx errors and no unhealthy targets at the observations.
- Both backends returned identical data throughout.

See [Migration results](#migration-results).

### Credits: starter code vs migration work

The starting point, kept as received, is the **CoderCo** EC2-to-ECS migration assignment
(CoderCo Community learning project), from the upstream starter
[codercohq/ec2-legacy-app](https://github.com/codercohq/ec2-legacy-app):

- the Flask application and its API;
- the Nginx and systemd configuration and the setup scripts;
- the single-EC2 Terraform and its documentation.

The original README is preserved as [docs/legacy-starter-readme.md](docs/legacy-starter-readme.md),
and the original architecture is in [ARCHITECTURE.md](ARCHITECTURE.md).

All of the following was added for the migration:

- **Application and tests:** the shared storage layer ([app/storage.py](app/storage.py)),
  readiness checks and the test suites.
- **Container:** the production Dockerfile.
- **Terraform:** the bootstrap and platform roots.
- **CI/CD:** the workflows and their CI tooling.
- **Operations:** the runbooks and the evidence.

## Key outcomes

- **Containerised Flask app on ECS Fargate:**
  - non-root;
  - read-only root filesystem;
  - the base image pinned by digest.
- **Two tasks across two Availability Zones** in private subnets with no public IPs.
  CPU target-tracking autoscaling was configured from 2 to 4 tasks.
- **Terraform with separate roots:**
  - a [bootstrap](terraform/bootstrap/) root for the state bucket, ECR and the GitHub OIDC
    roles;
  - a [platform](terraform/) root for the network, ALB, ECS, EC2, DynamoDB and monitoring.
    It uses remote S3 state with lock-file locking.
- **GitHub Actions CI/CD:** blocking CI and Terraform CI gates, and a manual,
  approval-gated [deploy.yml](.github/workflows/deploy.yml).
- **AWS OIDC, no long-lived keys:** separate plan, image-push and apply roles. Each role
  trusts only the exact GitHub job that needs it.
- **Reviewed saved plans:**
  - the plan is stored privately;
  - `plan_guard.py` refuses `application` deploys that move traffic or storage;
  - a reviewer must approve the `production` environment;
  - Terraform then applies exactly that plan, with its SHA-256 checked.
- **Immutable images:** ECR tags are commit SHAs, tags can't be overwritten, and ECS
  pulls by digest.
- **Shared DynamoDB persistence:** both EC2 and ECS ran with `STORAGE_BACKEND=dynamodb`.
  Each order and its stock decrement are written in one transaction. The table had
  point-in-time recovery and deletion protection.
- **Progressive ALB weighted cutover** in stages 0 → 1 → 2 → 3.
- **Verified rollback** to EC2 and restoration to ECS, both through the same reviewed
  pipeline.
- **CloudWatch:**
  - ECS logs;
  - six alarms: ALB 5xx, ECS target 5xx, unhealthy targets for each group, ECS CPU, and
    tasks below desired;
  - a migration dashboard;
  - VPC flow logs.

## Before and after

**Before:** the CoderCo starter, a single EC2 instance. The full description is in
[ARCHITECTURE.md](ARCHITECTURE.md#original-architecture-legacy-ec2-before-migration).

```mermaid
flowchart LR
  user([Internet]) -->|HTTP :80| eip[Elastic IP]
  subgraph vpc["VPC, one public subnet, one AZ"]
    subgraph ec2["EC2 t3.micro"]
      nginx[Nginx :80] --> gunicorn["Gunicorn + Flask :5000"]
      gunicorn --> mem[("In-memory data<br/>lost on restart")]
    end
  end
  eip --> nginx
```

**After:** the platform deployed on 28 October 2026 and decommissioned after
verification. The diagram is in [Architecture](#architecture) below.

| | Before (starter) | After (deployed 28 Oct 2026, now retired) |
|---|---|---|
| Entry point | Elastic IP on one instance | Public ALB across two AZs, weighted across two target groups |
| Compute | One EC2 instance: Nginx → Gunicorn | ECS Fargate, 2 tasks in private subnets in 2 AZs (autoscaling 2–4); EC2 kept healthy as the rollback target |
| Data | In process memory, lost on restart, inconsistent across workers | Shared DynamoDB table, transactional order writes, PITR |
| Scaling and availability | None, single AZ | Multi-AZ tasks, CPU target tracking |
| Infrastructure | Single-instance Terraform, local state | Separate bootstrap and platform roots, S3 remote state with lock files |
| Delivery | Manual, on the instance | GitHub Actions: CI gates → image by digest → saved plan → `production` approval → exact apply → rollout check |
| Credentials | — | GitHub OIDC roles, no static keys |
| Visibility | Logs on the instance | CloudWatch logs, six alarms, a migration dashboard, VPC flow logs |
| Traffic change and rollback | Not possible | One `traffic_stage` line, deployed through the same reviewed path |

## Architecture

![Deployed platform architecture (historical: live on 28 October 2026, since decommissioned)](docs/architecture/ec2-to-ecs-overview.png)

*The platform as deployed and verified on 28 October 2026. It has since been
decommissioned. The editable source is
[ec2-to-ecs-overview.drawio](docs/architecture/ec2-to-ecs-overview.drawio).*

What was deployed at the end of the live migration, as verified on 28 October 2026
before teardown:

- **Public ALB, HTTP :80.** One listener forwarded to two target groups with
  Terraform-owned weights (`traffic_stage` 0–3). The last production traffic
  configuration was stage **3**. Two extra listener rules routed
  `X-Migration-Target: ec2|ecs`. They worked only from a single operator `/32`, for
  side-by-side verification.
- **Legacy EC2, kept as the rollback target.** It ran Nginx → Gunicorn, stayed registered
  and healthy at weight 0, and also read and wrote the shared DynamoDB table.
- **ECS Fargate service, 2 tasks** in two private subnets.
  - Only the ALB security group could reach port 5000.
  - Outbound traffic went through a single NAT gateway (for ECR and logs) and through S3
    and DynamoDB gateway endpoints.
- **DynamoDB `legacy-api-production`:** products, stock, an order counter and orders,
  shared by both backends.
- **ECR, CloudWatch, Terraform and GitHub Actions:**
  - images were built and scanned in CI, pushed by digest, and deployed through a saved
    Terraform plan;
  - logs, alarms and the dashboard were in CloudWatch.

The Terraform source and
[terraform/environments/production.tfvars](terraform/environments/production.tfvars)
describe this deployed architecture. They aren't infrastructure that is running now.

There was no HTTPS, ACM certificate or Route 53 record: the demo used the ALB's HTTP
hostname. Details: [docs/aws-platform.md](docs/aws-platform.md) and the
[migration flow diagram](docs/architecture/migration-traffic-flow.png).

## Migration strategy

![Migration traffic flow: stages, gates and rollback](docs/architecture/migration-traffic-flow.png)

*Editable source: [migration-traffic-flow.drawio](docs/architecture/migration-traffic-flow.drawio).*

1. **Prepare:**
   - build and scan the image;
   - create ECS at weight 0;
   - seed DynamoDB;
   - switch EC2 to DynamoDB storage. Terraform preconditions refuse any weight above 0
     before this.
2. **Shift traffic in stages:**
   - stage 0 is 100/0, stage 1 is 90/10, stage 2 is 50/50 and stage 3 is 0/100;
   - each stage is one `traffic_stage` commit, deployed as a `platform` run;
   - the operator reviews the saved plan (only the listener weights, plus the rebuilt
     image), then approves `production`.
3. **Hold and observe:** each stage is held for at least 15 minutes, with read-only checks
   at every observation:
   - the weights;
   - target and task health;
   - all six alarms, plus 5xx and unhealthy-host metrics;
   - DynamoDB writes and a consistent scan;
   - a comparison of both backends;
   - 100–200 sampled requests.
4. **Stop or roll back:** any failed check means stopping. A rollback is
   `traffic_stage = 0` through the same pipeline. EC2 stays healthy and writes to the same
   table, so no data has to move.

The full operator runbook is in
[docs/first-deployment-checklist.md](docs/first-deployment-checklist.md), and the data
rules are in [docs/migration-state-runbook.md](docs/migration-state-runbook.md).

## Migration results

Live in AWS, 28 October 2026.

| Stage | ALB weights (EC2 / ECS) | Deploy run | Sampled `GET /health` | Answered by | Result |
|---|---|---|---|---|---|
| 0 baseline | 100 / 0 | [40703528943](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40703528943) | – | EC2 | ✅ |
| 1 canary | 90 / 10 | [40713560064](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40713560064) | 600/600 HTTP 200 | ECS 56/600 (9.3 %) | ✅ PASS |
| 2 half | 50 / 50 | [40719498237](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40719498237) | 390/390 HTTP 200 | ECS 188/390 (48.2 %) | ✅ PASS |
| 3 ECS only | 0 / 100 | [40727166757](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40727166757) | 350/350 HTTP 200 | all ECS | ✅ PASS |
| Rollback | 100 / 0 | [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690) | 300/300 HTTP 200 | all EC2 | ✅ PASS |
| Final restoration | 0 / 100 | [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980) | 350/350 HTTP 200 | all ECS | ✅ PASS |

The figures are **sampled requests during the monitored holds**, not proof of continuous
uptime. The ECS share is an estimate of the configured weight.

At every observation:

- the targets were healthy;
- all six alarms were `OK`;
- ALB and target 5xx were 0;
- unhealthy hosts were 0;
- both backends returned identical products, orders and stats.

CloudWatch publishes 1–3 minutes late, so the last minutes before each check aren't
covered.

The full timeline, plan summaries and sanitised monitor output are in the
[evidence](evidence/live-migration-2026-10-28/README.md).

## Shared DynamoDB

The table was seeded once. Then exactly two controlled orders were placed:

1. **Order 1** went through **ECS** and was visible through **EC2**.
2. **Order 2** went through **EC2** and was visible through **ECS**.

Afterwards, both backends showed orders [1, 2], product 1 stock **98** (from 100) and
total revenue **59.98** (application currency units). A consistent-read DynamoDB scan
confirmed exactly two orders, counter 2 and stock 98. These values stayed the same through
every later stage, the rollback and the restoration, because a write freeze stayed in
place. Design and limits: [docs/migration-state-runbook.md](docs/migration-state-runbook.md).

## CI/CD and security

```
CI + security gates → publish image (ECR digest) → saved Terraform plan (private S3)
  → manual approval of "production" → apply exactly that plan → verify_rollout.py
```

- **Gates:**
  - unit and contract tests, plus DynamoDB Local integration tests;
  - `pip-audit`;
  - Trivy on the exact image;
  - gitleaks;
  - Checkov;
  - Terraform `fmt`, `validate` and mocked `terraform test`;
  - a rendered-IAM-policy gate;
  - workflow privilege tests;
  - an offline check of relative Markdown links ([ci/check_markdown_links.py](ci/check_markdown_links.py));
  - pre-commit hooks.
- **GitHub OIDC:**
  - PR and push workflows get no AWS access;
  - in `deploy.yml`, each job assumes only its own role (plan, image-push or apply);
  - each role trusts one exact OIDC subject and has no static keys;
  - the apply role is region-locked, tag- and name-scoped, and explicitly denied from
    changing its own identity.
- **Approval:** the `production` environment requires a reviewer, accepts only `main`,
  and allows no admin bypass. A rejected approval means no apply credentials are issued.
  The gate was tested before any AWS deploy: a rejected run never started its job, and
  an approved run did.

<img src="evidence/task-16-predeployment/01-production-approval-waiting.png" width="720" alt="GitHub Actions run waiting for production environment approval">

*The approval-gate test run waiting for `production` review on 27 October 2026. The
rejected and approved runs are shown in the [evidence index](evidence/README.md#screenshots-production-approval-gate-27-october-2026).*

Details: [docs/ci-cd.md](docs/ci-cd.md),
[docs/iam-permission-matrix.md](docs/iam-permission-matrix.md) and
[docs/approval-gate-test.md](docs/approval-gate-test.md).

## Testing, monitoring and evidence

Every claim in this README is linked to real output in the
**[evidence index](evidence/README.md)**, grouped by capability: container, shared
state, Terraform, CI gates, IAM, approval, cutover, rollback, monitoring and teardown.
The workflows run on every push:
[CI](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/workflows/ci.yml) and
[Terraform CI](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/workflows/terraform-ci.yml).

- **Automated tests,** counted at commit `560e1e5`:
  - 410 CI tooling and policy tests (`ci/tests`);
  - 86 application tests (`tests/`), including the Gunicorn and DynamoDB Local tests that
    run on Linux in CI;
  - mocked Terraform tests: 19 for the platform root, 11 for bootstrap.
- **Container:** built and smoke-tested in CI, Trivy-gated on fixable HIGH and CRITICAL
  findings, and scanned again on push to ECR.
- **Health:**
  - ALB checks on `/ready`, which tests storage access;
  - container health checks;
  - `verify_rollout.py` after every apply (ECS stable, `rolloutState COMPLETED`, every
    target healthy, `/health` and `/ready` 200).
- **Cutover holds:** each stage held for at least 15 minutes with at least three read-only
  observations (two for the rollback). Each observation checked:
  - the ALB weights;
  - target and task health;
  - alarms;
  - 5xx and unhealthy-host metrics;
  - DynamoDB write metrics;
  - a consistent DynamoDB scan;
  - a comparison of both backends;
  - 100–200 unheadered requests.

## Rollback

Rollback is the same reviewed path as any stage, not a console edit:

1. Commit `traffic_stage = 0`.
2. Run a `platform` deploy.
3. Review the saved plan: the listener weights change, and ECS rolls the rebuilt image.
4. Approve.

It was tested from 100 % ECS: [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690),
an apply of about 3 minutes. All 300 sampled requests were then served by EC2, with no
data divergence, because both backends share the table. 100 % ECS was then restored with
[40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980).

EC2 must never be rolled back to in-memory storage once shared writes exist. Runbook:
[docs/first-deployment-checklist.md](docs/first-deployment-checklist.md#phase-9-rollback).

## Engineering decisions

[docs/engineering-decisions.md](docs/engineering-decisions.md) gives the reasoning,
trade-offs and verified behaviour for each choice. In short:

- **Fargate** removed host management.
- **DynamoDB had to come before any traffic split,** so that requests could land on
  either backend.
- **EC2 stayed healthy at weight 0** as a rollback target that already had the same data.
- **ALB weights** gave gradual, reversible shifts without DNS caching delays.
- **Rollback went through Terraform,** so it was reviewed and couldn't drift from the
  code.
- **OIDC** meant no static keys.
- **Saved, approved plans** meant the reviewer approved exactly what was applied.
- **Separate bootstrap and platform roots** kept identity and state out of reach of a
  normal deploy.
- **Two AZs, one NAT gateway and HTTP only** were cost choices for a short
  demonstration. What would change for production is listed in that document.

A note on Terraform layout: the subnets and route-table associations are written as
separate resources (`public`, `public_b`, `private`, `private_b`) instead of `for_each`.
The first ones come from the CoderCo starter. Keeping their addresses avoided replacing the
deployed network, and keeps the code identical to what produced the saved plans. ECS CPU,
memory and task counts are variables, and tags come from the provider's `default_tags`.

## Known limitations

This is a short-lived portfolio and learning deployment, not a hardened or compliance-reviewed
production system.

- **HTTP only:** no ACM certificate, HTTPS listener or custom domain.
- **Base-image vulnerabilities:** the Debian base image has unfixed HIGH CVEs. They are
  documented as time-limited exceptions and re-checked on every build: the Trivy gate
  blocks only *fixable* findings. See
  [docs/security-exceptions.md](docs/security-exceptions.md). The project does not claim
  zero vulnerabilities.
- **Fixed ALB weights:**
  - the ECS circuit breaker rolls back a bad ECS deployment, but it doesn't move traffic
    back to EC2;
  - recovery means a reviewed `traffic_stage = 0` deploy;
  - the alarms make the problem visible.
- **No zero-downtime guarantee:** the measured holds showed no errors in the samples, but
  that isn't a guarantee.
- **Cost and resilience trade-offs:**
  - one NAT gateway (a single-AZ egress dependency, and the largest fixed cost);
  - a single-instance EC2 rollback target;
  - the bootstrap state is kept locally with backups.
- **Image rebuilt on every deploy:** builds aren't bit-reproducible, so every
  `traffic_stage` deploy also rolls ECS to a freshly built image.
- **No idempotency key on `POST /api/v1/orders`:** a client retry after a timeout can
  create a duplicate order.

## Evidence and links

| Topic | Where |
|---|---|
| Live migration results (28 Oct 2026) | [evidence/live-migration-2026-10-28/](evidence/live-migration-2026-10-28/README.md) |
| Architecture and migration diagrams | [docs/architecture/](docs/architecture/README.md) |
| CI/CD, OIDC and approval | [docs/ci-cd.md](docs/ci-cd.md) |
| AWS platform, cost and teardown | [docs/aws-platform.md](docs/aws-platform.md) |
| Shared-state migration runbook | [docs/migration-state-runbook.md](docs/migration-state-runbook.md) |
| Operator runbook (deploy, cutover, rollback, teardown) | [docs/first-deployment-checklist.md](docs/first-deployment-checklist.md) |
| Acceptance checklist | [docs/acceptance-checklist.md](docs/acceptance-checklist.md) |
| Security exceptions and IAM matrix | [docs/security-exceptions.md](docs/security-exceptions.md), [docs/iam-permission-matrix.md](docs/iam-permission-matrix.md) |
| All evidence, by capability (container, IAM, pre-commit, incidents, approval) | [evidence/README.md](evidence/README.md) |
| Engineering decisions and trade-offs | [docs/engineering-decisions.md](docs/engineering-decisions.md) |
| AWS teardown record (28 Oct 2026) | [evidence/aws-teardown-2026-10-28/](evidence/aws-teardown-2026-10-28/README.md) |
| Recording guide (historical) | [docs/demo-walkthrough.md](docs/demo-walkthrough.md) |

## Teardown (completed 28 October 2026)

After the results were recorded, all AWS resources were removed on 28 October 2026 to
stop the costs. Teardown was a **reviewed sequence, not a single `terraform destroy`**,
because:

- the ALB and the DynamoDB table had deletion protection;
- the bundle and state buckets were versioned;
- ECR's `force_delete` was off;
- the state bucket has `prevent_destroy`.

What was run:

1. **Deletion protection disabled** with a reviewed, targeted saved plan (0 added,
   2 changed, 0 destroyed).
2. **Bundle bucket emptied**, deleting its one object version by its exact version ID.
3. **Platform destroyed** by a reviewed saved destroy plan (**0 added, 0 changed,
   70 destroyed**).
4. **Bootstrap resources removed** after the 11 ECR images were deleted: a reviewed,
   targeted plan removed the ECR repository and the three GitHub Actions roles and their
   policies (**0 added, 0 changed, 16 destroyed**).
5. **Container Insights log group deleted.** AWS had created it outside Terraform.

Kept on purpose:

- the private, versioned Terraform state bucket;
- the GitHub OIDC provider, which other projects share.

No project compute, load balancer, NAT, public IPv4, DynamoDB, ECR or CloudWatch
resources remain. The [Deploy workflow](.github/workflows/deploy.yml) is disabled, and its
repository variables and secret have been removed.

The full record, including where it departed from the original runbook, is in
[evidence/aws-teardown-2026-10-28](evidence/aws-teardown-2026-10-28/README.md).

## Demo video

**[Watch the EC2 → ECS Fargate migration walkthrough on YouTube](https://youtu.be/InSQNVuTTlw).**

The walkthrough was recorded on 28 October 2026, before the AWS environment was
intentionally decommissioned. The recording guide is
[docs/demo-walkthrough.md](docs/demo-walkthrough.md); its AWS links are historical and
no longer active.

## License

The original application is a learning project for the CoderCo Community. The migration
work in this repository builds on it for educational and portfolio purposes.
