# AWS teardown record: 28 October 2026

This is the record of how the migration platform was taken down once the live results had
been captured. It is sanitised: it contains no Terraform state or plans, no IAM policy
documents, no credentials, no IP or CIDR values, no account ID and no private bucket names
or object keys. The raw state, plans and logs are kept privately by the operator.

- **Region:** `eu-west-2`.
- **Starting point:** commit `2c67e4f`.
- **Last deployment:** run
  [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980),
  at `traffic_stage = 3` (ECS 100 %).

Each destructive step was run only after a separate explicit approval. Each one used a
saved Terraform plan that had been inspected first: the human-readable output, the JSON
resource actions and the address list were all checked.

## Backups taken before teardown

All of these are kept privately and checksummed:

- the platform state (pulled from the remote backend) and the bootstrap state;
- the exact deployment inputs, reconstructed from the last approved saved plan;
- a read-only DynamoDB scan of all 6 items: 3 products, orders 1 and 2, and the order
  counter;
- the migration evidence archive and the walkthrough recording, each with a second copy.

A chargeable DynamoDB on-demand backup was not used.

## Executed teardown: 28 October 2026

| # | Step | Result |
|---|---|---|
| 1 | Disable deletion protection on the ALB and the DynamoDB table. This used a **targeted** saved plan (`aws_lb.app`, `aws_dynamodb_table.app`) built from the exact deployed inputs. | 0 added, **2 changed**, 0 destroyed |
| 2 | Empty the versioned application bundle bucket: its one object version (`app.zip`) was deleted by its exact version ID. There were no delete markers. | Bucket empty, 0 versions |
| 3 | Apply the reviewed saved **platform destroy** plan (`plan -destroy`, no targeting). | 0 added, 0 changed, **70 destroyed** |
| 4 | Delete the 11 images in the ECR repository by explicit digest. `force_delete` was off, so the repository had to be empty first. | 11 deleted, 0 failures |
| 5 | Apply the reviewed, **targeted bootstrap** destroy plan: the ECR repository and its lifecycle policy, the plan, image-push and apply roles, 5 inline policies, 3 managed policies and 3 attachments. | 0 added, 0 changed, **16 destroyed** |
| 6 | Delete the Container Insights performance log group. AWS creates it, so it isn't in Terraform state. | Deleted |
| 7 | GitHub: disable the Deploy workflow, and delete its repository variables and its one secret. | Deploy disabled; history kept |

**Final Terraform state:**

- **Platform root:** 0 managed resources (serial 31).
- **Bootstrap root:** 7 managed resources (serial 50). These are the private,
  versioned, encrypted remote-state bucket and its configuration, which are kept on
  purpose. The bucket still has `prevent_destroy`.

**Kept:**

- the GitHub OIDC provider, which the bootstrap root reads only as a data source;
- the seven IAM roles belonging to other projects that trust it;
- all unrelated resources in the account.

**Read-only checks after teardown** found none of these left for the project:

- ECS clusters or tasks;
- EC2 instances;
- load balancers or target groups;
- NAT gateways;
- Elastic IPs or public IPv4 addresses;
- VPC endpoints or the project VPC;
- DynamoDB tables;
- ECR repository or images;
- CloudWatch log groups, alarms or dashboards;
- the SNS topic;
- the bundle bucket;
- project IAM roles, policies or instance profiles.

Billing data lags by up to a day or more, so Cost Explorer can't yet confirm that the
final charges have settled.

## How the execution differed from the original runbook

The original procedure in
[first-deployment-checklist.md, Phase 11](../../docs/first-deployment-checklist.md#phase-11-teardown-and-cost-check)
and [aws-platform.md §8](../../docs/aws-platform.md#8-teardown-do-not-run-until-migration-acceptance)
was written before deployment. It was not followed exactly:

- **No return to Stage 0.** The whole platform was being decommissioned, so a Stage 0
  deploy would have added a deployment and an image rollout with no safety benefit. The
  data had already been backed up.
- **No on-demand DynamoDB backup.** A read-only scan export was used instead.
- **Protection was disabled locally with a targeted plan, not through a Deploy run.**
  - Every Deploy run rebuilds the image and replaces both ECS task definitions. The last
    approved deploy plan already showed this.
  - An untargeted plan also showed task-definition replacements and four policy updates
    shown as "known after apply", so the runbook's rule that only those two attributes
    should change could not have been met.
  - The targeted plan contained exactly the two attribute changes.
- **The bundle bucket was emptied by exact version ID** before the destroy, rather than
  waiting for a `BucketNotEmpty` error.
- **There was no blanket bootstrap destroy.**
  - The state bucket's `prevent_destroy` would make a full bootstrap destroy fail.
  - A targeted plan removed only the 16 project resources.
  - The ECR images were deleted only after the platform destroy, because the platform
    root reads the deployed image through a data source.
- **The leftover checks went beyond the runbook's list.** They also covered:
  - the Container Insights log group;
  - the two autoscaling alarms, which AWS removed along with the scaling policy;
  - the ALB-owned public IPv4 addresses;
  - VPC endpoints and network interfaces.

## Still available

- Source code, Terraform, `production.tfvars` and every workflow, including the disabled
  [deploy.yml](../../.github/workflows/deploy.yml).
- Every GitHub Actions run, including the stage, rollback and restoration deploys:
  - [40713560064](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40713560064)
  - [40719498237](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40719498237)
  - [40727166757](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40727166757)
  - [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690)
  - [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980)
- The [architecture diagrams](../../docs/architecture/README.md) and the
  [live migration evidence](../live-migration-2026-10-28/README.md).
