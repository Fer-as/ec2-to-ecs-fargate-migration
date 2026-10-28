# Live migration evidence: 28 October 2026

The EC2 → ECS Fargate cutover in AWS (`eu-west-2`, account `…7756`), followed by a deliberate
rollback and a restoration. Every step went through the protected `deploy.yml` workflow:

1. CI and security gates.
2. ECR image publish (immutable digest).
3. A Terraform plan saved to a private S3 bucket.
4. Operator review of that plan, then manual approval of the `production` environment.
5. Apply of exactly that saved plan.
6. `verify_rollout.py`.

This page summarises the verified results. The full, checksummed evidence archive (monitor
outputs, saved plan logs, preflights) is kept privately. The final observation of each
stage is reproduced below, sanitised, in [excerpts/](excerpts/).

## Deployments

Each stage commit changed one line, `traffic_stage` in
[production.tfvars](../../terraform/environments/production.tfvars).

| Stage | ALB default weights (EC2 / ECS) | Commit | Deploy run | Apply finished (UTC) |
|---|---|---|---|---|
| 0 baseline (EC2 on shared DynamoDB) | 100 / 0 | `ee63f63` | [40703528943](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40703528943) | 14:56:58 |
| 1 canary | 90 / 10 | `4fe22eb` | [40713560064](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40713560064) | 15:21:50 |
| 2 half | 50 / 50 | `d1c7245` | [40719498237](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40719498237) | 16:01:51 |
| 3 ECS only | 0 / 100 | `683937b` | [40727166757](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40727166757) | 16:43:53 |
| Rollback demo | 100 / 0 | `459470f` | [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690) | 17:39:15 |
| Restoration | 0 / 100 | `560e1e5` | [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980) | 18:01:05 |

Every stage plan had the same shape, `2 to add, 2 to change, 2 to destroy`:

- the listener weights changed in place;
- the ECS app and seed task definitions were replaced, because each commit builds a new image;
- the ECS service rolled to the new revision.

No stage plan touched the EC2 instance, the Elastic IP, DynamoDB, the target groups, the
verification listener rules, IAM or networking.

## Monitoring results

Every check was read-only. Each stage was held for at least 15 minutes, with at least
three observations (two for the rollback).

| Stage | Observations (UTC) | Unheadered `GET /health` | Answered by ECS | Result |
|---|---|---|---|---|
| 1 (90/10) | 15:28:39, 15:33:00, 15:37:39 | 600/600 HTTP 200 | 56/600 (9.3 %) | PASS |
| 2 (50/50) | 16:03:56, 16:10:04, 16:17:31 | 390/390 HTTP 200 | 188/390 (48.2 %) | PASS |
| 3 (0/100) | 16:46:29, 16:52:08, 16:59:31 | 350/350 HTTP 200 | 350/350 (100 %) | PASS |
| Rollback (100/0) | 17:41:25, 17:47:09 | 300/300 HTTP 200 | 0/300 (all EC2) | PASS |
| Restoration (0/100) | 18:03:38, 18:10:01, 18:17:33 | 350/350 HTTP 200 | 350/350 (100 %) | PASS |

The backend was identified from the `Server` response header: EC2 serves through nginx,
ECS directly from gunicorn. These are **sampled requests at the observation times**, not
a measurement of continuous uptime. The percentages estimate the configured weights; they
don't measure them exactly.

At every observation:

- **ALB:** the configured weights matched the stage.
- **EC2:** the target was healthy, the Elastic IP was attached, and the instance was never
  replaced during the cutover.
- **ECS:** 2/2 tasks running, 0 pending, the deployment `COMPLETED`, and both targets
  healthy. There were no restarts outside the deploy's own rolling replacement.
- **Alarms:** all six project alarms were `OK`. The only alarm in ALARM was ECS
  autoscaling's scale-in alarm, which is expected at the 2-task minimum.
- **Errors:** ALB-generated 5xx = 0, target 5xx = 0 and UnHealthyHostCount = 0.
  CloudWatch publishes 1–3 minutes late, so the last minutes before each check weren't
  covered by that observation.
- **Header routes:** both `X-Migration-Target` routes reached the intended backend. They
  are restricted to a single operator `/32`.

## Shared DynamoDB test

The table was seeded once (three products, an order counter at 0). Before Stage 1, one
controlled order was placed through each backend:

| Order | Written through | Created (UTC) | Verified through |
|---|---|---|---|
| 1 (product 1, qty 1, 29.99) | ECS (`X-Migration-Target: ecs`) | 15:07:56 | EC2 |
| 2 (product 1, qty 1, 29.99) | EC2 (`X-Migration-Target: ec2`) | 15:07:57 | ECS |

Both backends then agreed on orders [1, 2], product 1 stock 98 (from 100), total orders 2
and revenue 59.98 (application currency units). A consistent-read DynamoDB scan matched:
exactly two orders, sequence 2 and stock 98.

These values stayed identical at every later observation, including after the rollback
and the restoration. There were no other writes: the general write freeze stayed in
place for the rest of the demonstration.

## Earlier deployments (incidents fixed before the cutover)

The first platform deploys exposed real IAM and verification gaps. Each was fixed in code
and then redeployed:

| Run | Result | Fix |
|---|---|---|
| 40530861020 | partial apply (60/65): app bundle missing on the apply runner; dashboard body invalid | `d072a5e`: the bundle travels with the saved plan (SHA-256 checked), dashboard fixed ([incident notes](../task-21-deploy-failure/)) |
| 40538277000 | `ec2:RunInstances` denied on the Canonical Ubuntu AMI (IAM sees the owner alias `amazon`) | `5a47186` ([investigation](../task-22-runinstances-denial/)) |
| 40556838063 | apply succeeded; verifier read `rolloutState` before ECS marked it `COMPLETED` | `5c8450c` (verifier polls up to 5 min) |
| 40577733449 | EC2 moved to DynamoDB; Elastic IP update denied (released association resolves to resource `*/*`) | `ee63f63` (IAM literal `${*}/${*}` for that one action), recovered by 40703528943 |

## Excerpts (sanitised)

The final observation of each stage, from the private archive:

- [Stage 1, 90/10](excerpts/stage1-90-10-final-observation.txt)
- [Stage 2, 50/50](excerpts/stage2-50-50-final-observation.txt)
- [Stage 3, 0/100](excerpts/stage3-0-100-final-observation.txt)
- [Rollback, 100/0](excerpts/rollback-100-0-final-observation.txt)
- [Restoration, 0/100](excerpts/final-restoration-0-100-final-observation.txt)

The account ID and all IP addresses are masked. No Terraform state, saved plans,
credentials or operator IP are included.
