# Offline unit tests for the platform configuration logic.
#
#   terraform init -backend=false && terraform test
#
# Providers are MOCKED: nothing is sent to AWS and no real plan is produced.
# These tests check our own logic (weights, guards, conditional ECS, container
# settings, user data), not AWS-side behaviour or IAM evaluation.

mock_provider "aws" {
  override_during = plan

  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-2a", "eu-west-2b", "eu-west-2c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { id = "legacy-api-app-test", arn = "arn:aws:s3:::legacy-api-app-test" }
  }
}

mock_provider "archive" {
  override_during = plan
}

# Distinct subnet IDs so subnet sets can be counted during plan
override_resource {
  target          = aws_subnet.public
  override_during = plan
  values          = { id = "subnet-public-a" }
}
override_resource {
  target          = aws_subnet.public_b
  override_during = plan
  values          = { id = "subnet-public-b" }
}
override_resource {
  target          = aws_subnet.private
  override_during = plan
  values          = { id = "subnet-private-a" }
}
override_resource {
  target          = aws_subnet.private_b
  override_during = plan
  values          = { id = "subnet-private-b" }
}

variables {
  aws_region = "eu-west-2"
}

run "baseline_defaults_send_all_traffic_to_ec2_and_create_no_ecs_service" {
  command = plan

  assert {
    condition     = output.traffic_weights.ec2 == 100 && output.traffic_weights.ecs == 0
    error_message = "Default stage must send 100% to EC2."
  }
  assert {
    condition     = length(aws_ecs_service.app) == 0 && length(aws_ecs_task_definition.app) == 0
    error_message = "No ECS service/task definition may be created without an image digest."
  }
  assert {
    condition     = var.legacy_storage_backend == "memory" && !strcontains(aws_instance.app.user_data, "flask-app.env")
    error_message = "Legacy EC2 must stay in memory mode (no env file) by default."
  }
  assert {
    condition     = aws_dynamodb_table.app.hash_key == "pk" && aws_dynamodb_table.app.range_key == "sk" && aws_dynamodb_table.app.billing_mode == "PAY_PER_REQUEST"
    error_message = "Table key schema/billing must match app/storage.py."
  }
  assert {
    condition     = aws_dynamodb_table.app.point_in_time_recovery[0].enabled && aws_dynamodb_table.app.server_side_encryption[0].enabled && aws_dynamodb_table.app.deletion_protection_enabled
    error_message = "Table must have PITR, encryption and deletion protection."
  }
  assert {
    condition     = toset([for a in aws_dynamodb_table.app.attribute : "${a.name}:${a.type}"]) == toset(["pk:S", "sk:N"])
    error_message = "Key attribute types must be pk=S, sk=N."
  }
  assert {
    condition     = aws_lb.app.internal == false && length(aws_lb.app.subnets) == 2
    error_message = "ALB must be internet-facing in two public subnets."
  }
  assert {
    condition     = aws_subnet.public_b.availability_zone == "eu-west-2b" && aws_subnet.private_b.availability_zone == "eu-west-2b" && aws_subnet.public.availability_zone == "eu-west-2a"
    error_message = "Subnets must span two AZs, with the legacy subnet unchanged in AZ a."
  }
  assert {
    condition     = aws_lb_target_group.ecs.target_type == "ip" && aws_lb_target_group.ecs.port == 5000 && aws_lb_target_group.ecs.health_check[0].path == "/ready"
    error_message = "ECS target group must be ip:5000 with /ready health check."
  }
  assert {
    condition     = aws_lb_target_group.ec2.target_type == "instance" && aws_lb_target_group.ec2.port == 80
    error_message = "EC2 target group must be instance:80 (Nginx)."
  }
}

run "ecs_service_created_once_digest_is_set" {
  command = plan

  variables {
    app_image_digest    = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = length(aws_ecs_service.app) == 1 && aws_ecs_service.app[0].launch_type == "FARGATE"
    error_message = "ECS Fargate service must exist when a digest is set."
  }
  assert {
    condition     = aws_ecs_service.app[0].network_configuration[0].assign_public_ip == false && length(aws_ecs_service.app[0].network_configuration[0].subnets) == 2
    error_message = "Tasks must run in two private subnets without public IPs."
  }
  assert {
    condition     = aws_ecs_service.app[0].deployment_circuit_breaker[0].enable && aws_ecs_service.app[0].deployment_circuit_breaker[0].rollback
    error_message = "Deployment circuit breaker with rollback must be enabled."
  }
  assert {
    condition     = aws_ecs_service.app[0].desired_count == 2 && aws_appautoscaling_target.ecs[0].min_capacity == 2 && aws_appautoscaling_target.ecs[0].max_capacity == 4
    error_message = "Service must start with 2 tasks and autoscale 2..4."
  }
  assert {
    condition     = endswith(jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].image, "@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    error_message = "Image must be referenced by immutable digest."
  }
  assert {
    condition = contains(
      jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment,
      { name = "STORAGE_BACKEND", value = "dynamodb" }
    )
    error_message = "ECS must always use STORAGE_BACKEND=dynamodb."
  }
  assert {
    condition = contains(
      jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].environment,
      { name = "DYNAMODB_TABLE_NAME", value = "legacy-api-production" }
    )
    error_message = "DYNAMODB_TABLE_NAME must come from the Terraform table."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].readonlyRootFilesystem == true && jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].user == "10001:10001"
    error_message = "Container must be read-only and run as UID 10001."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].linuxParameters.capabilities.drop == ["ALL"]
    error_message = "All Linux capabilities must be dropped."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].stopTimeout == 30 && jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].healthCheck.command[0] == "CMD"
    error_message = "Stop timeout and an explicit health check are required."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.app[0].container_definitions)[0].logConfiguration.logDriver == "awslogs"
    error_message = "Logs must go to CloudWatch (awslogs)."
  }
  assert {
    condition     = output.traffic_weights.ecs == 0
    error_message = "Deploying the image alone must not shift traffic."
  }
}

run "traffic_to_ecs_rejected_without_image" {
  command = plan

  variables {
    traffic_stage          = 1
    legacy_storage_backend = "dynamodb"
  }

  expect_failures = [aws_lb_listener.http]
}

run "traffic_to_ecs_rejected_while_ec2_uses_memory" {
  command = plan

  variables {
    traffic_stage       = 2
    app_image_digest    = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  expect_failures = [aws_lb_listener.http]
}

run "image_tag_is_rejected" {
  command = plan

  variables {
    app_image_digest = "latest"
  }

  expect_failures = [var.app_image_digest]
}

run "invalid_stage_is_rejected" {
  command = plan

  variables {
    traffic_stage = 25
  }

  expect_failures = [var.traffic_stage]
}

run "half_split_with_shared_storage" {
  command = plan

  variables {
    traffic_stage          = 2
    legacy_storage_backend = "dynamodb"
    app_image_digest       = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled    = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = output.traffic_weights.ec2 == 50 && output.traffic_weights.ecs == 50
    error_message = "Stage 2 must be 50/50."
  }
  assert {
    condition = alltrue([
      strcontains(aws_instance.app.user_data, "STORAGE_BACKEND=dynamodb"),
      strcontains(aws_instance.app.user_data, "DYNAMODB_TABLE_NAME=legacy-api-production"),
      strcontains(aws_instance.app.user_data, "AWS_REGION=eu-west-2"),
    ])
    error_message = "EC2 user data must configure the same DynamoDB table as ECS."
  }
  assert {
    condition     = terraform_data.legacy_storage_mode.input == "dynamodb"
    error_message = "Storage mode tracker must follow legacy_storage_backend (forces EC2 replacement)."
  }
}

run "full_cutover_and_rollback_weights" {
  command = plan

  variables {
    traffic_stage          = 3
    legacy_storage_backend = "dynamodb"
    app_image_digest       = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled    = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = output.traffic_weights.ec2 == 0 && output.traffic_weights.ecs == 100
    error_message = "Stage 3 must be ECS 100%."
  }
}

run "rollback_to_dynamodb_backed_ec2" {
  command = plan

  variables {
    traffic_stage          = 0
    legacy_storage_backend = "dynamodb"
    app_image_digest       = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled    = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = output.traffic_weights.ec2 == 100 && output.traffic_weights.ecs == 0 && strcontains(aws_instance.app.user_data, "STORAGE_BACKEND=dynamodb")
    error_message = "Rollback must return 100% to the DynamoDB-backed EC2."
  }
}

run "pinned_availability_zones_are_used" {
  command = plan

  variables {
    availability_zones = ["eu-west-2b", "eu-west-2c"]
  }

  assert {
    condition     = aws_subnet.public.availability_zone == "eu-west-2b" && aws_subnet.private.availability_zone == "eu-west-2b" && aws_subnet.public_b.availability_zone == "eu-west-2c" && aws_subnet.private_b.availability_zone == "eu-west-2c"
    error_message = "Subnets must follow var.availability_zones when pinned."
  }
}

run "verification_routes_need_operator_cidrs" {
  command = plan

  variables {
    app_image_digest         = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled      = true
    verification_cidr_blocks = ["203.0.113.10/32"]
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition = length(aws_lb_listener_rule.verify_backend) == 2 && alltrue([
      for k in ["ecs", "ec2"] : anytrue([
        for c in aws_lb_listener_rule.verify_backend[k].condition : anytrue([
          for h in c.http_header : h.http_header_name == "X-Migration-Target" && contains(h.values, k)
        ])
        ]) && anytrue([
        for c in aws_lb_listener_rule.verify_backend[k].condition : anytrue([
          for ip in c.source_ip : ip.values == toset(["203.0.113.10/32"])
        ])
      ])
    ])
    error_message = "Header routes must exist for both backends AND be limited to the operator CIDRs."
  }
}

run "no_public_verification_routes_without_operator_cidrs" {
  command = plan

  variables {
    app_image_digest    = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = length(aws_lb_listener_rule.verify_backend) == 0
    error_message = "Header routes must not exist unless operator CIDRs are set."
  }
}

run "verification_cidrs_reject_the_whole_internet" {
  command = plan

  variables {
    verification_cidr_blocks = ["0.0.0.0/0"]
  }

  expect_failures = [var.verification_cidr_blocks]
}

run "digest_alone_creates_task_definitions_but_no_service" {
  command = plan

  variables {
    app_image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  assert {
    condition     = length(aws_ecs_task_definition.seed) == 1 && length(aws_ecs_task_definition.app) == 1 && length(aws_ecs_service.app) == 0
    error_message = "First deployment: seed + app task definitions, but no service until the table is seeded."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.seed[0].container_definitions)[0].command == ["python", "seed_dynamodb.py"]
    error_message = "Seed task must run the idempotent seed script."
  }
}

run "service_without_image_is_rejected" {
  command = plan

  variables {
    ecs_service_enabled = true
  }

  expect_failures = [var.ecs_service_enabled]
}

run "secure_network_defaults" {
  command = plan

  assert {
    condition     = !anytrue([for r in aws_security_group.ec2.ingress : length(coalesce(r.cidr_blocks, [])) > 0])
    error_message = "Direct internet access to the EC2 instance must be closed by default (only the ALB)."
  }
  assert {
    condition     = toset([for r in aws_security_group.ec2.egress : "${r.protocol}:${r.from_port}"]) == toset(["tcp:80", "tcp:443"])
    error_message = "Legacy EC2 egress must be limited to TCP 80 and 443."
  }
  assert {
    condition     = aws_flow_log.vpc.traffic_type == "REJECT" && aws_lb.app.enable_deletion_protection
    error_message = "VPC flow logs and ALB deletion protection must be on."
  }
}

run "direct_ec2_access_cannot_be_opened_to_the_internet" {
  command = plan

  variables {
    allowed_cidr_blocks = ["0.0.0.0/0"]
  }

  expect_failures = [var.allowed_cidr_blocks]
}

# Task 12: the VPC's default security group is not managed, so nothing may depend on it
run "workloads_never_use_the_default_security_group" {
  command = plan

  variables {
    app_image_digest    = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ecs_service_enabled = true
  }

  override_data {
    target = data.aws_ecr_image.app[0]
    values = { image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
  }

  override_resource {
    target          = aws_security_group.alb
    override_during = plan
    values          = { id = "sg-alb" }
  }
  override_resource {
    target          = aws_security_group.ecs
    override_during = plan
    values          = { id = "sg-ecs" }
  }
  override_resource {
    target          = aws_security_group.ec2
    override_during = plan
    values          = { id = "sg-ec2" }
  }

  assert {
    condition     = toset(aws_lb.app.security_groups) == toset([aws_security_group.alb.id])
    error_message = "The ALB must use its own security group."
  }
  assert {
    condition     = toset(aws_ecs_service.app[0].network_configuration[0].security_groups) == toset([aws_security_group.ecs.id])
    error_message = "ECS tasks must use their own security group (an empty list would fall back to the default SG)."
  }
  assert {
    condition     = toset(aws_instance.app.vpc_security_group_ids) == toset([aws_security_group.ec2.id])
    error_message = "The EC2 instance must use its own security group."
  }
  assert {
    condition     = aws_vpc_endpoint.dynamodb.vpc_endpoint_type == "Gateway" && aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway"
    error_message = "Interface endpoints would need a security group; only gateway endpoints are expected."
  }
}

# Task 21 regression: CloudWatch requires every entry of properties.metrics to be an
# ARRAY ([namespace, metric, dim, value, ..., {options}]). flatten() once collapsed these
# into one flat list and AWS rejected the dashboard (112 validation errors).
run "dashboard_metrics_are_nested_arrays" {
  command = plan

  # The dashboard embeds these dimensions; pin them so the body is known at plan time
  override_resource {
    target          = aws_lb.app
    override_during = plan
    values = {
      arn        = "arn:aws:elasticloadbalancing:eu-west-2:111122223333:loadbalancer/app/legacy-api-production/0123456789abcdef"
      arn_suffix = "app/legacy-api-production/0123456789abcdef"
    }
  }
  override_resource {
    target          = aws_lb_target_group.ecs
    override_during = plan
    values = {
      arn        = "arn:aws:elasticloadbalancing:eu-west-2:111122223333:targetgroup/legacy-api-production-ecs/0123456789abcdef"
      arn_suffix = "targetgroup/legacy-api-production-ecs/0123456789abcdef"
    }
  }
  override_resource {
    target          = aws_lb_target_group.ec2
    override_during = plan
    values = {
      arn        = "arn:aws:elasticloadbalancing:eu-west-2:111122223333:targetgroup/legacy-api-production-ec2/0123456789abcdef"
      arn_suffix = "targetgroup/legacy-api-production-ec2/0123456789abcdef"
    }
  }

  assert {
    condition = alltrue(flatten([
      for w in jsondecode(aws_cloudwatch_dashboard.migration.dashboard_body).widgets : [
        for m in w.properties.metrics : can(m[0]) && can(regex("^[A-Za-z]+/[A-Za-z]+$", m[0])) && can(m[1])
      ]
    ]))
    error_message = "Every dashboard metric must be an array starting with a namespace and a metric name."
  }

  assert {
    condition = [
      for w in jsondecode(aws_cloudwatch_dashboard.migration.dashboard_body).widgets : length(w.properties.metrics)
    ] == [2, 4, 5, 2, 2, 2]
    error_message = "Expected panels: requests (EC2, ECS), healthy+unhealthy x2, 4xx/5xx x2 + ALB 5xx, p95 x2, ECS CPU+memory, desired+running."
  }

  assert {
    condition = toset(flatten([
      for w in jsondecode(aws_cloudwatch_dashboard.migration.dashboard_body).widgets : [for m in w.properties.metrics : m[1]]
      ])) == toset(["RequestCount", "HealthyHostCount", "UnHealthyHostCount", "HTTPCode_Target_5XX_Count", "HTTPCode_Target_4XX_Count",
    "HTTPCode_ELB_5XX_Count", "TargetResponseTime", "CPUUtilization", "MemoryUtilization", "DesiredTaskCount", "RunningTaskCount"])
    error_message = "A dashboard panel lost a metric."
  }
}
