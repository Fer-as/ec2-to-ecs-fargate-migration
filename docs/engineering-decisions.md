# Engineering decisions

Why the migration was built the way it was, and what was traded away. Each entry covers:

- **Requirement:** what had to be true.
- **Decision:** what was built.
- **Trade-off:** cost or complexity accepted.
- **Verified:** what the live run on 28 October 2026 showed, with a link.
- **Production change:** what would differ for a long-lived system.

The platform was decommissioned after verification
([teardown record](../evidence/aws-teardown-2026-10-28/README.md)). Everything below
describes what was deployed then.

## 1. ECS Fargate instead of continuing on EC2

- **Requirement:** remove the single-instance service and make releases repeatable.
- **Decision:** run the app as an immutable container image (pulled by digest) on ECS
  Fargate. There are no hosts to patch, and scaling is set per task.
- **Trade-off:** Fargate costs more per vCPU than a t3 instance, and it adds a NAT or VPC
  endpoints, because the tasks have no public IP.
- **Verified:** 2 tasks in 2 AZs; every rollout reached `COMPLETED`; ECS served 100 % of
  traffic in stage 3 and after the restoration
  ([evidence](../evidence/live-migration-2026-10-28/README.md)).

## 2. Shared DynamoDB before any traffic split

- **Requirement:** with two backends behind one ALB, any request can reach either one.
  With in-memory storage, an order placed on ECS would not exist on EC2.
- **Decision:**
  - add a storage layer ([app/storage.py](../app/storage.py)) and switch EC2 to DynamoDB
    first;
  - Terraform preconditions refuse `traffic_stage > 0` unless EC2 uses DynamoDB;
  - each order and its stock decrement are written in one `TransactWriteItems` call.
- **Trade-off:**
  - converting EC2 meant replacing the instance once, before the cutover;
  - a write freeze was kept during the migration, so only two controlled orders were
    written.
- **Verified:** an order through ECS was visible through EC2, and an order through EC2
  through ECS. Stock 98 and revenue 59.98 stayed the same through every stage
  ([evidence](../evidence/live-migration-2026-10-28/README.md#shared-dynamodb-test)).
- **Production change:** add an idempotency key to `POST /api/v1/orders`. Today a client
  retry after a timeout can create a duplicate order.

## 3. Keeping the legacy EC2 backend during the cutover

- **Requirement:** a rollback target that is already running and already has the same
  data.
- **Decision:** keep EC2 registered in its own target group, healthy at weight 0 in
  stage 3. Terraform ignores AMI changes, so a newer image can't quietly replace it.
- **Trade-off:** the EC2 instance and its Elastic IP cost money for the whole migration.
- **Verified:** the deliberate rollback served all 300 sampled requests from EC2, with no
  data divergence ([run 40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690)).

## 4. Weighted ALB routing

- **Requirement:** move traffic gradually, without DNS caching delays, and be able to
  reverse it.
- **Decision:**
  - one listener with weighted forwarding to both target groups;
  - `traffic_stage` 0–3 maps to 100/0, 90/10, 50/50 and 0/100
    ([terraform/alb.tf](../terraform/alb.tf));
  - two `X-Migration-Target` header rules, limited to one operator `/32`, could reach
    each backend directly for side-by-side checks.
- **Trade-off:**
  - the weights are fixed, so nothing shifts traffic back automatically;
  - stickiness was off, so a client could see either backend from one request to the
    next. Shared storage made that safe.
- **Verified:** the sampled ECS shares were 9.3 % at 90/10 and 48.2 % at 50/50.
- **Production change:** alarm-driven automatic rollback, for example ECS blue/green with
  CodeDeploy, or a controller that resets the weights.

## 5. Rollback through Terraform, not the console

- **Requirement:** the rollback has to be reviewable, recorded and repeatable, and must
  not drift from the code.
- **Decision:** a rollback is a commit setting `traffic_stage = 0`, deployed through the
  same saved-plan and approval path as any other stage.
- **Trade-off:** it's slower than a console click. The apply took about 3 minutes,
  because the pipeline also rebuilt the image (see the limitations below).
- **Verified:** rollback run
  [40736488690](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40736488690),
  then restoration run
  [40739889980](https://github.com/Fer-as/ec2-to-ecs-fargate-migration/actions/runs/40739889980).
  Both passed.

## 6. GitHub OIDC instead of long-lived AWS keys

- **Requirement:** no static credentials in GitHub, and least privilege for each job.
- **Decision:**
  - three roles (plan, image-push and apply), each trusting one exact OIDC subject;
  - PR and push workflows get no AWS access at all;
  - the apply role is limited by region, tags and names, and can't change its own
    identity ([IAM matrix](iam-permission-matrix.md)).
- **Trade-off:** more IAM to design and test. Two real permission gaps showed up only in
  live runs (an AMI owner alias, and EIP disassociation), and each was fixed narrowly.
- **Verified:** every deploy authenticated through OIDC; the policy gates live in
  [ci/tests](../ci/tests/).

## 7. Saved, approved Terraform plans

- **Requirement:** what a reviewer approves must be exactly what gets applied.
- **Decision:**
  - the plan job saves a binary plan, with its SHA-256, to a private bucket;
  - [plan_guard.py](../ci/plan_guard.py) refuses `application` deploys that change
    traffic or storage;
  - the `production` environment requires approval;
  - the apply job checks the hash and applies exactly that file.
- **Trade-off:** with one maintainer, the approval is an operator checkpoint rather than an
  independent review. The docs say so.
- **Verified:** the gate was tested with no AWS involved, first rejected and then
  approved ([approval-gate-test.md](approval-gate-test.md)). After that, every migration
  deploy paused for approval.

## 8. Separate bootstrap and platform Terraform roots

- **Requirement:** long-lived identity and state must not be destroyed or changed by an
  application deploy.
- **Decision:**
  - [terraform/bootstrap](../terraform/bootstrap/) holds the state bucket (versioned, TLS
    only, `prevent_destroy`), ECR and the OIDC roles. It is applied rarely and by hand,
    with local state;
  - [terraform/](../terraform/) holds the platform, with an S3 backend using native lock
    files (`use_lockfile`, no DynamoDB lock table) ([backend.tf](../terraform/backend.tf));
  - the shared GitHub OIDC provider is only read, through a data source, because other
    projects use it.
- **Trade-off:** two roots to run, and the bootstrap state is kept locally, with backups.
- **Verified:** at teardown, a targeted bootstrap plan removed only the 16 project
  resources, and the state bucket and OIDC provider stayed.

## 9. Two Availability Zones for ECS

- **Requirement:** the service must survive one AZ failing, and an ALB needs two subnets.
- **Decision:** ECS tasks run in private subnets in AZ A and AZ B, and the ALB spans both
  public subnets.
- **Trade-off:** the subnets are written as separate resources (`public`, `public_b`,
  `private`, `private_b`) rather than a `for_each`. The original single-AZ addresses come
  from the starter, and keeping them avoided replacing the deployed network mid-migration.
  For a new project, an iterated map would be cleaner.

## 10. One NAT gateway

- **Requirement:** private tasks need outbound access to ECR and CloudWatch Logs. S3 and
  DynamoDB already go through free gateway endpoints.
- **Decision:** a single NAT gateway in AZ A. It's the largest fixed cost, at about
  $37 a month ([aws-platform.md §7](aws-platform.md#7-indicative-cost-eu-west-2-london-on-demand-730-hmonth)).
- **Trade-off:** if AZ A loses outbound access, AZ B tasks can't pull images or ship
  logs. That was accepted for a short demonstration.
- **Production change:** one NAT gateway per AZ, or interface endpoints for ECR, logs and
  STS.

## 11. HTTP only

- **Requirement:** a temporary demonstration without a registered domain.
- **Decision:** an ALB listener on HTTP :80 only. There was no ACM certificate, no
  Route 53 record and no redirect to HTTPS. No credentials or personal data passed
  through the API.
- **Production change:** an ACM certificate, an HTTPS listener with a modern TLS policy,
  an HTTP → HTTPS redirect, a Route 53 alias record, and AWS WAF if the service were
  exposed publicly.

## Known limitations (stated, not hidden)

- **Sampled measurements:** 1,990 sampled requests returned HTTP 200 during the
  monitored holds. That isn't continuous monitoring and doesn't guarantee zero downtime.
- **Unfixed base-image CVEs:** the Debian base image has HIGH findings with no fix
  available yet. They are time-limited exceptions; the gate blocks fixable findings
  ([security-exceptions.md](security-exceptions.md)).
- **Builds aren't bit-reproducible:** every deploy rebuilt the image, so each
  `traffic_stage` change also rolled ECS to a new task definition
  ([evidence](../evidence/task-04-container/10-reproducibility.txt)). A `platform` deploy
  that reuses a verified digest would separate the two changes.
- **Image size:** the production image measured **188 MB** locally
  ([build log](../evidence/task-04-container/01-docker-build.txt)). The ECR image metadata
  recorded before teardown showed about 64 MB compressed for each pushed image. The image is multi-stage, runs as non-root UID
  10001, uses a read-only root filesystem and has a health check.
- **Single-instance rollback target** and **local bootstrap state**, as described above.

## For a long-lived production service

Beyond the items above:

- HTTPS and a domain;
- a NAT gateway per AZ or interface endpoints;
- automatic rollback;
- order idempotency;
- reproducible builds or digest promotion between environments;
- an independent second approver;
- alarm notifications to a monitored channel;
- retiring the EC2 path entirely once the rollback window closes.
