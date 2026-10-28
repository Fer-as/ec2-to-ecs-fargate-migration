# Demo walkthrough: Loom recording guide (7–9 minutes)

A recording plan for the EC2 → ECS Fargate migration. It shows the code, the
approval-gated pipeline, the live AWS platform and the verified results.

> **Historical guide.** The recording was made on 28 October 2026, and the AWS platform
> was decommissioned the same day
> ([teardown record](../evidence/aws-teardown-2026-10-28/README.md)). The AWS console
> links and the ALB endpoints below are **inactive** and are kept only as a record of
> what was shown. The GitHub links still work.

**Read-only recording.** Use the browser and `GET` requests only. Do **not**:

- place orders or send `POST` requests;
- approve or re-run deployments;
- change ALB weights;
- edit anything in the AWS console.

The production write freeze stays in effect, and the data you show (orders [1, 2],
stock 98) must stay unchanged.

## Links

| What | Link |
|---|---|
| Repository | https://github.com/Fer-as/ec2-to-ecs-fargate-migration |
| Platform diagram | [docs/architecture/ec2-to-ecs-overview.png](architecture/ec2-to-ecs-overview.png) |
| Migration diagram | [docs/architecture/migration-traffic-flow.png](architecture/migration-traffic-flow.png) |
| Deploy workflow | [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) |
| Traffic weights (Terraform) | [terraform/alb.tf](../terraform/alb.tf) (`traffic_weights`), [terraform/environments/production.tfvars](../terraform/environments/production.tfvars) (`traffic_stage = 3`) |
| Stage 1 run (90/10) | https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40713560064 |
| Stage 2 run (50/50) | https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40719498237 |
| Stage 3 run (0/100) | https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40727166757 |
| Rollback run (100/0) | https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690 |
| Final restoration run (0/100) | https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980 |
| Evidence summary | [evidence/live-migration-2026-10-28/README.md](../evidence/live-migration-2026-10-28/README.md) |

AWS console, region **eu-west-2** (historical, inactive since teardown):

| What | Console link |
|---|---|
| ECS service (tasks, health, events) | https://eu-west-2.console.aws.amazon.com/ecs/v2/clusters/legacy-api-production/services/legacy-api-production-app/health?region=eu-west-2 |
| Load balancer and listener rules | https://eu-west-2.console.aws.amazon.com/ec2/home?region=eu-west-2#LoadBalancers:search=legacy-api-production |
| Target groups (EC2 + ECS health) | https://eu-west-2.console.aws.amazon.com/ec2/home?region=eu-west-2#TargetGroups:search=legacy-api-production |
| DynamoDB table | https://eu-west-2.console.aws.amazon.com/dynamodbv2/home?region=eu-west-2#table?name=legacy-api-production |
| CloudWatch dashboard | https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#dashboards/dashboard/legacy-api-production-migration |
| CloudWatch alarms | https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#alarmsV2:?search=legacy-api-production |
| ECS logs | https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#logsV2:log-groups/log-group/$252Fecs$252Flegacy-api-production |

Read-only API endpoints, all `GET` (historical, inactive since teardown; HTTP only):

- http://legacy-api-production-1605859241.eu-west-2.elb.amazonaws.com/health
- http://legacy-api-production-1605859241.eu-west-2.elb.amazonaws.com/ready
- http://legacy-api-production-1605859241.eu-west-2.elb.amazonaws.com/api/v1/products
- http://legacy-api-production-1605859241.eu-west-2.elb.amazonaws.com/api/v1/orders
- http://legacy-api-production-1605859241.eu-west-2.elb.amazonaws.com/api/v1/stats

## Recording sequence

| # | Time | Show on screen | Narration (short) | Highlight |
|---|---|---|---|---|
| 1 | 0:00–0:45 | Repository README (top table) | "This project takes a legacy Flask app on one EC2 instance and migrates it to ECS Fargate without losing shared data, using a staged ALB cutover, a tested rollback and a manual approval gate. The starting app is CoderCo's; the platform, pipeline and migration are my work." | Final state 0/100, all stages PASS |
| 2 | 0:45–1:45 | Platform diagram | "An ALB sends traffic to two target groups. The legacy EC2 instance stays registered as the rollback target; two Fargate tasks run in private subnets across two AZs. Both backends share one DynamoDB table, which is what makes rollback safe." | ALB weights, private subnets, shared DynamoDB, OIDC roles |
| 3 | 1:45–2:45 | `deploy.yml` (job list), then a stage run graph | "Every change goes through the same pipeline: CI and security gates, image published by digest, Terraform plan saved privately, then a reviewer approves the production environment and Terraform applies exactly that plan, followed by a rollout verifier." | Approval pause, plan job, `verify_rollout` |
| 4 | 2:45–3:30 | `alb.tf` `traffic_weights`, then the one-line `traffic_stage` commit diff | "A cutover stage is one line: `traffic_stage`. Terraform maps it to listener weights, and preconditions refuse any stage above 0 unless ECS exists and EC2 uses DynamoDB." | 0/1/2/3 → 100/0, 90/10, 50/50, 0/100 |
| 5 | 3:30–5:00 | Migration diagram, then the evidence results table; open the Stage 1, 2 and 3 runs | "I moved 10 %, then 50 %, then 100 % of traffic. Each stage held for at least 15 minutes with read-only checks: all sampled requests returned 200, the ECS share matched the weights, there were no 5xx errors or unhealthy targets, and both backends returned identical data." | 600/600, 390/390, 350/350; ECS 9.3 %, 48.2 %, 100 % |
| 6 | 5:00–6:00 | Rollback run, then restoration run | "Then I rolled back on purpose: one reviewed deploy back to 100 % EC2, about three minutes, with no data divergence because both sides share the table. Then I restored 100 % ECS the same way." | 300/300 via EC2; 350/350 via ECS |
| 7 | 6:00–7:30 | AWS console: ECS service → target groups → listener rules → DynamoDB → dashboard and alarms | "Live: two healthy Fargate tasks, EC2 still healthy at weight zero, listener at 0/100, the table with deletion protection and point-in-time recovery, and the alarms all OK." | 2/2 tasks, both target groups healthy, 6 alarms OK |
| 8 | 7:30–8:15 | Browser: `/health`, `/api/v1/orders`, `/api/v1/stats` | "The two test orders: one written through ECS, one through EC2, each visible on the other side. Stock is 98, revenue 59.98." | Orders [1, 2], total_orders 2 |
| 9 | 8:15–9:00 | README "Known limitations" and "Cleanup" | "It's a portfolio deployment: HTTP only, documented base-image exceptions, a single NAT, and fixed weights don't fail over automatically. It's torn down after recording through a reviewed sequence, because of deletion protection and versioned buckets." | Honest limits, reviewed teardown |

## Screenshot checklist

- [ ] README top table and the Migration results table
- [ ] Platform and migration diagrams
- [ ] A deploy run graph paused at `production` approval (any stage run's history), and a
      completed run with every job green
- [ ] Listener default action: EC2 0 / ECS 100, plus the two `X-Migration-Target` rules
      (crop or blur the source-IP value)
- [ ] Both target groups healthy (EC2 1/1, ECS 2/2)
- [ ] ECS service: 2/2 running, deployment completed
- [ ] DynamoDB table overview: deletion protection and point-in-time recovery on
- [ ] CloudWatch dashboard and the alarm list (6 × OK)
- [ ] Browser: `/api/v1/orders` and `/api/v1/stats`

## Privacy and security checklist (before and after recording)

- [ ] Close terminals and tabs that show credentials, `aws configure`, tokens or local
      `.aws` files.
- [ ] Don't show the operator's public IP. On the listener rules, the source-IP
      condition shows it: blur or crop it, or don't open the rule detail.
- [ ] Don't show GitHub secrets or variables pages, or the AWS account menu (account ID),
      billing or IAM users.
- [ ] Don't show the private Terraform state bucket objects or saved plans.
- [ ] Hide browser bookmarks, other tabs, notifications and personal email.
- [ ] Use `GET` only: no orders, no approvals, no deploys, no console edits.
- [ ] After recording, check that the stats still show `total_orders: 2`. Then follow the
      reviewed teardown in [first-deployment-checklist.md, Phase 11](first-deployment-checklist.md#phase-11-teardown-and-cost-check).
