# Public ALB in front of BOTH the legacy EC2 instance and the ECS service.
# One HTTP listener forwards to two target groups with weights taken from
# var.traffic_stage (0 = EC2 only ... 3 = ECS only; rollback = back to 0).
#
# Note: weights are fixed. If one target group becomes unhealthy the ALB does NOT
# move its share to the other group; roll back by setting traffic_stage = 0.

locals {
  traffic_weights = {
    0 = { ec2 = 100, ecs = 0 } # baseline / rollback
    1 = { ec2 = 90, ecs = 10 } # canary
    2 = { ec2 = 50, ecs = 50 } # half
    3 = { ec2 = 0, ecs = 100 } # ECS only
  }
  weights = local.traffic_weights[var.traffic_stage]
}

# --- Security groups ---------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB for the legacy API"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  #checkov:skip=CKV_AWS_260:The ALB is the public entry point; port 80 from var.alb_ingress_cidr_blocks by design (see docs/security-exceptions.md)
  for_each          = toset(var.alb_ingress_cidr_blocks)
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from clients"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "alb_to_ecs" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To ECS tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5000
  to_port                      = 5000
  referenced_security_group_id = aws_security_group.ecs.id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_ec2" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To legacy EC2 (Nginx)"
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
  referenced_security_group_id = aws_security_group.ec2.id
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "ECS tasks: app port from the ALB only"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name}-ecs"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ecs_from_alb" {
  security_group_id            = aws_security_group.ecs.id
  description                  = "App port from the ALB"
  ip_protocol                  = "tcp"
  from_port                    = 5000
  to_port                      = 5000
  referenced_security_group_id = aws_security_group.alb.id
}

# HTTPS out: ECR and CloudWatch Logs via NAT, DynamoDB and S3 via gateway endpoints.
resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs.id
  description       = "HTTPS to AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# --- Load balancer ------------------------------------------------------------------------

resource "aws_lb" "app" {
  #checkov:skip=CKV2_AWS_20:No domain/ACM certificate yet, so no HTTPS listener to redirect to (see docs/security-exceptions.md)
  #checkov:skip=CKV2_AWS_28:No WAF for the demo (cost); required before real production traffic (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_91:ALB access logs need a log bucket with an ELB bucket policy; CloudWatch ALB metrics/alarms used instead (see docs/security-exceptions.md)
  name                       = local.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [aws_subnet.public.id, aws_subnet.public_b.id]
  drop_invalid_header_fields = true
  idle_timeout               = 60
  enable_deletion_protection = var.alb_deletion_protection

  tags = {
    Name = local.name
  }
}

resource "aws_lb_target_group" "ecs" {
  #checkov:skip=CKV_AWS_378:ALB-to-task traffic stays inside the VPC; the app serves plain HTTP (TLS terminates at the ALB once a certificate exists) (see docs/security-exceptions.md)
  name                 = "${local.name}-ecs"
  port                 = 5000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  # /ready checks DynamoDB connectivity and that the store is seeded
  health_check {
    path                = "/ready"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group" "ec2" {
  #checkov:skip=CKV_AWS_378:ALB-to-Nginx traffic stays inside the VPC; the legacy Nginx serves plain HTTP (see docs/security-exceptions.md)
  name                 = "${local.name}-ec2"
  port                 = 80
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  # Through Nginx; /ready is 200 in memory mode and checks DynamoDB in dynamodb mode
  health_check {
    path                = "/ready"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group_attachment" "ec2" {
  target_group_arn = aws_lb_target_group.ec2.arn
  target_id        = aws_instance.app.id
  port             = 80
}

resource "aws_lb_listener" "http" {
  #checkov:skip=CKV_AWS_2:HTTP-only until a domain and ACM certificate exist (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_103:No TLS listener yet, so no TLS policy applies (see docs/security-exceptions.md)
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "forward"

    forward {
      target_group {
        arn    = aws_lb_target_group.ec2.arn
        weight = local.weights.ec2
      }
      target_group {
        arn    = aws_lb_target_group.ecs.arn
        weight = local.weights.ecs
      }
      # Shared DynamoDB state makes sticky sessions unnecessary
      stickiness {
        enabled  = false
        duration = 1
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.traffic_stage == 0 || local.ecs_service_enabled
      error_message = "traffic_stage > 0 needs the ECS service: set app_image_digest and ecs_service_enabled first."
    }
    precondition {
      condition     = var.traffic_stage == 0 || var.legacy_storage_backend == "dynamodb"
      error_message = "traffic_stage > 0 needs legacy_storage_backend = \"dynamodb\" so EC2 and ECS share state (and EC2 stays a valid rollback target)."
    }
  }
}

# Pre-cutover verification: "X-Migration-Target: ecs" (or "ec2") sends a request to
# that backend regardless of the weights, so operators can compare both sides before
# any weighted traffic reaches ECS.
#
# Restricted to var.verification_cidr_blocks (operator IPs) and only created when that
# list is set. Without the source-IP condition any internet client could bypass the
# staged rollout and, before EC2 is switched to DynamoDB, write orders into the shared
# table. Requests from other IPs with the header follow the normal weights.
resource "aws_lb_listener_rule" "verify_backend" {
  for_each = local.ecs_service_enabled && length(var.verification_cidr_blocks) > 0 ? {
    ecs = { priority = 10, target_group_arn = aws_lb_target_group.ecs.arn }
    ec2 = { priority = 20, target_group_arn = aws_lb_target_group.ec2.arn }
  } : {}

  listener_arn = aws_lb_listener.http.arn
  priority     = each.value.priority

  action {
    type             = "forward"
    target_group_arn = each.value.target_group_arn
  }

  condition {
    http_header {
      http_header_name = "X-Migration-Target"
      values           = [each.key]
    }
  }

  condition {
    source_ip {
      values = var.verification_cidr_blocks
    }
  }
}
