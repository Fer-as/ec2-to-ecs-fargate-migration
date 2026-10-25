# ECS Fargate service for the containerised application.
#
# Bootstrap dependency: the image must already be in ECR. The task definitions and
# service are only created once app_image_digest is set (an immutable sha256 digest,
# never a tag). The digest is checked against ECR by a data source, so a mistyped or
# missing image fails the plan instead of creating a broken service.

locals {
  # Task definitions (app + one-off seed) need only the image; the service also
  # needs ecs_service_enabled, so the table can be seeded before the service starts.
  ecs_enabled         = var.app_image_digest != null
  ecs_service_enabled = var.ecs_service_enabled && local.ecs_enabled
  ecr_repository_url  = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.ecr_repository_name}"
  # Digest read back from ECR, so the image is proven to exist before ECS uses it
  app_image      = local.ecs_enabled ? "${local.ecr_repository_url}@${data.aws_ecr_image.app[0].image_digest}" : null
  cluster_name   = local.name
  service_name   = "${local.name}-app"
  container_name = "app"

  # Settings shared by the app and seed containers
  container_env = [
    { name = "STORAGE_BACKEND", value = "dynamodb" }, # never in-memory on ECS
    { name = "DYNAMODB_TABLE_NAME", value = aws_dynamodb_table.app.name },
    { name = "AWS_REGION", value = var.aws_region },
  ]
  container_security = {
    user                   = "10001:10001"
    readonlyRootFilesystem = true
    linuxParameters        = { capabilities = { drop = ["ALL"] } }
  }
}

data "aws_ecr_image" "app" {
  count           = local.ecs_enabled ? 1 : 0
  repository_name = var.ecr_repository_name
  image_digest    = var.app_image_digest
}

resource "aws_cloudwatch_log_group" "app" {
  #checkov:skip=CKV_AWS_158:Encrypted at rest by CloudWatch Logs with AWS-managed keys; a customer-managed KMS key adds cost and key-policy work (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_338:Retention set by var.log_retention_days (default 30) for cost; 1 year is not required for this migration (see docs/security-exceptions.md)
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }
}

resource "aws_ecs_task_definition" "app" {
  count                    = local.ecs_enabled ? 1 : 0
  family                   = "${local.name}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([merge(local.container_security, {
    name      = local.container_name
    image     = local.app_image
    essential = true

    portMappings = [{ containerPort = 5000, protocol = "tcp" }]

    environment = concat(local.container_env, [
      { name = "WEB_CONCURRENCY", value = tostring(var.web_concurrency) },
    ])

    # Liveness only (/health does not touch DynamoDB), so a DynamoDB outage does
    # not make ECS kill healthy tasks. The ALB uses /ready for routing.
    healthCheck = {
      command     = ["CMD", "python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4).status == 200 else 1)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 15
    }

    # Gunicorn --graceful-timeout is 25 s; give it time before SIGKILL
    stopTimeout = 30

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "app"
        mode                  = "non-blocking"
        max-buffer-size       = "4m"
      }
    }
  })])
}

# One-off seed task (not a service): run once with `aws ecs run-task` (see outputs).
resource "aws_ecs_task_definition" "seed" {
  count                    = local.ecs_enabled ? 1 : 0
  family                   = "${local.name}-seed"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_seed.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([merge(local.container_security, {
    name        = "seed"
    image       = local.app_image
    essential   = true
    command     = ["python", "seed_dynamodb.py"]
    environment = local.container_env
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "seed"
      }
    }
  })])
}

resource "aws_ecs_service" "app" {
  #checkov:skip=CKV_AWS_332:Platform version pinned to 1.4.0 (the current latest Linux version) for reproducible deployments (see docs/security-exceptions.md)
  count            = local.ecs_service_enabled ? 1 : 0
  name             = local.service_name
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.app[0].arn
  desired_count    = var.ecs_min_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  # Rolling deployment: keep full capacity, start new tasks before stopping old ones
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = [aws_subnet.private.id, aws_subnet.private_b.id]
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ecs.arn
    container_name   = local.container_name
    container_port   = 5000
  }

  propagate_tags          = "SERVICE"
  enable_ecs_managed_tags = true
  wait_for_steady_state   = var.ecs_wait_for_steady_state

  lifecycle {
    # Application Auto Scaling owns the running count after creation
    ignore_changes = [desired_count]
  }

  # The target group must be attached to the ALB before the service uses it
  depends_on = [aws_lb_listener.http, aws_iam_role_policy.ecs_execution]
}

# --- Autoscaling: 2..4 tasks, track average CPU ----------------------------------------------

resource "aws_appautoscaling_target" "ecs" {
  count              = local.ecs_service_enabled ? 1 : 0
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.ecs_min_count
  max_capacity       = var.ecs_max_count
}

resource "aws_appautoscaling_policy" "ecs_cpu" {
  count              = local.ecs_service_enabled ? 1 : 0
  name               = "${local.name}-cpu-target"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.ecs[0].service_namespace
  resource_id        = aws_appautoscaling_target.ecs[0].resource_id
  scalable_dimension = aws_appautoscaling_target.ecs[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.ecs_cpu_target_percent
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
