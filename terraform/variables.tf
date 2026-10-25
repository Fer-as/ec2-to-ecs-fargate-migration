variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "legacy-api"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro"
}

variable "key_pair_name" {
  description = "AWS Key Pair name for SSH access"
  type        = string
  default     = ""
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to reach the EC2 instance directly on HTTP (port 80), for baseline validation (e.g. your own /32). Default [] = closed; all traffic goes through the ALB."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.allowed_cidr_blocks : can(cidrnetmask(c)) && c != "0.0.0.0/0"])
    error_message = "allowed_cidr_blocks must contain valid IPv4 CIDR blocks and must not include 0.0.0.0/0 (public traffic goes through the ALB)."
  }
}

variable "ssh_allowed_cidr_blocks" {
  description = "CIDR blocks allowed to SSH (port 22). Empty (default) keeps SSH closed."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for c in var.ssh_allowed_cidr_blocks : can(cidrnetmask(c)) && c != "0.0.0.0/0"
    ])
    error_message = "ssh_allowed_cidr_blocks must contain valid IPv4 CIDR blocks and must not include 0.0.0.0/0."
  }
}

variable "domain_name" {
  description = "Domain name for Route53 record (optional)"
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID (optional)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

# --- Migration platform (ALB, ECS, DynamoDB) ----------------------------------------------

variable "legacy_storage_backend" {
  description = "Storage for the legacy EC2 app: \"memory\" (original) or \"dynamodb\" (shared with ECS). Switching replaces the instance; do it during the write freeze, after seeding."
  type        = string
  default     = "memory"

  validation {
    condition     = contains(["memory", "dynamodb"], var.legacy_storage_backend)
    error_message = "legacy_storage_backend must be \"memory\" or \"dynamodb\"."
  }
}

variable "traffic_stage" {
  description = "ALB traffic split: 0 = EC2 100% (baseline/rollback), 1 = EC2 90/ECS 10, 2 = 50/50, 3 = ECS 100%"
  type        = number
  default     = 0

  validation {
    condition     = contains([0, 1, 2, 3], var.traffic_stage)
    error_message = "traffic_stage must be 0, 1, 2 or 3."
  }
}

variable "app_image_digest" {
  description = "Immutable image digest in ECR (sha256:...). Leave null until the image is pushed; the ECS service is created only when set."
  type        = string
  default     = null

  validation {
    condition     = var.app_image_digest == null || can(regex("^sha256:[a-f0-9]{64}$", var.app_image_digest))
    error_message = "app_image_digest must be an image digest like sha256:<64 hex characters>, not a tag."
  }
}

variable "ecr_repository_name" {
  description = "ECR repository created by terraform/bootstrap"
  type        = string
  default     = "legacy-api"
}

variable "task_cpu" {
  description = "Fargate task CPU units"
  type        = number
  default     = 256

  validation {
    condition     = contains([256, 512, 1024], var.task_cpu)
    error_message = "task_cpu must be 256, 512 or 1024."
  }
}

variable "task_memory" {
  description = "Fargate task memory (MiB); must be valid for task_cpu"
  type        = number
  default     = 512

  validation {
    condition     = contains([512, 1024, 2048, 3072, 4096], var.task_memory)
    error_message = "task_memory must be 512, 1024, 2048, 3072 or 4096."
  }
}

variable "web_concurrency" {
  description = "Gunicorn workers per task (safe above 1 because state is in DynamoDB)"
  type        = number
  default     = 2

  validation {
    condition     = var.web_concurrency >= 1 && var.web_concurrency <= 4
    error_message = "web_concurrency must be between 1 and 4."
  }
}

variable "ecs_min_count" {
  description = "Minimum (and initial) number of ECS tasks; 2 spreads tasks over both AZs"
  type        = number
  default     = 2

  validation {
    condition     = var.ecs_min_count >= 2
    error_message = "ecs_min_count must be at least 2 for two-AZ availability."
  }
}

variable "ecs_max_count" {
  description = "Maximum number of ECS tasks for autoscaling"
  type        = number
  default     = 4

  validation {
    condition     = var.ecs_max_count >= 2 && var.ecs_max_count <= 10
    error_message = "ecs_max_count must be between 2 and 10."
  }
}

variable "ecs_cpu_target_percent" {
  description = "Target average CPU for autoscaling"
  type        = number
  default     = 60
}

variable "ecs_wait_for_steady_state" {
  description = "Make terraform apply wait until the ECS deployment is stable"
  type        = bool
  default     = true
}

variable "enable_container_insights" {
  description = "Container Insights (running vs desired task metrics; adds CloudWatch cost)"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the ECS application logs"
  type        = number
  default     = 30

  validation {
    condition     = contains([7, 14, 30, 60, 90], var.log_retention_days)
    error_message = "log_retention_days must be 7, 14, 30, 60 or 90."
  }
}

variable "alb_ingress_cidr_blocks" {
  description = "CIDR blocks allowed to reach the ALB on HTTP port 80"
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.alb_ingress_cidr_blocks) > 0 && alltrue([for c in var.alb_ingress_cidr_blocks : can(cidrnetmask(c))])
    error_message = "alb_ingress_cidr_blocks must contain at least one valid IPv4 CIDR block."
  }
}

variable "alb_deletion_protection" {
  description = "Protect the ALB from deletion (set false and apply before teardown)"
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Protect the DynamoDB table from deletion (set false and apply before teardown)"
  type        = bool
  default     = true
}

variable "alarm_email" {
  description = "Optional email for CloudWatch alarm notifications (subscription must be confirmed)"
  type        = string
  default     = null
}

variable "availability_zones" {
  description = "Two AZ names to pin (e.g. [\"eu-west-2a\", \"eu-west-2b\"]). Null uses the first two available AZs; pin it before the first apply."
  type        = list(string)
  default     = null

  validation {
    condition     = var.availability_zones == null || length(var.availability_zones) == 2
    error_message = "availability_zones must contain exactly two AZ names."
  }
}

variable "ecs_service_enabled" {
  description = "Create the ECS service. Keep false for the first deployment (image + seed task definition only), run the seed task, then set true."
  type        = bool
  default     = false

  validation {
    condition     = !var.ecs_service_enabled || var.app_image_digest != null
    error_message = "ecs_service_enabled = true requires app_image_digest."
  }
}

variable "verification_cidr_blocks" {
  description = "Operator CIDRs (e.g. your /32) allowed to use the X-Migration-Target header to reach one backend directly. Empty = header routes are not created."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.verification_cidr_blocks : can(cidrnetmask(c)) && c != "0.0.0.0/0"])
    error_message = "verification_cidr_blocks must be valid IPv4 CIDRs and must not include 0.0.0.0/0."
  }
}
