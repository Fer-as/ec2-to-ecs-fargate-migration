variable "aws_region" {
  description = "AWS region for all resources (must match the platform root)"
  type        = string
}

variable "project_name" {
  description = "Project name used in resource names"
  type        = string
  default     = "legacy-api"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "ecr_repository_name" {
  description = "ECR repository for the application image (must match the platform root)"
  type        = string
  default     = "legacy-api"
}

variable "ecr_keep_image_count" {
  description = "Number of most recent images kept in ECR; older ones expire"
  type        = number
  default     = 30
}

# GitHub OIDC -----------------------------------------------------------------
# No defaults on purpose: the repository must be stated explicitly.

variable "github_owner" {
  description = "GitHub user or organisation that owns the repository (exact, case-sensitive)"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$", var.github_owner))
    error_message = "github_owner must be a valid GitHub user or organisation name."
  }
}

variable "github_repository" {
  description = "GitHub repository name, without the owner"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,100}$", var.github_repository))
    error_message = "github_repository must be a repository name without the owner (no '/', no wildcards)."
  }
}

variable "github_subject_format" {
  description = <<-EOT
    OIDC "sub" claim format of the repository.
    "immutable": repo:OWNER@OWNER_ID/REPO@REPO_ID:...  (default for repositories created
                 after 15 July 2026, or opted in under Settings > Actions > OIDC)
    "legacy":    repo:OWNER/REPO:...  (older repositories that have not opted in)
  EOT
  type        = string
  default     = "immutable"

  validation {
    condition     = contains(["immutable", "legacy"], var.github_subject_format)
    error_message = "github_subject_format must be \"immutable\" or \"legacy\"."
  }
}

variable "github_owner_id" {
  description = "Numeric ID of the repository owner (gh api repos/OWNER/REPO --jq .owner.id). Required for the immutable format."
  type        = string
  default     = null

  validation {
    condition     = var.github_subject_format == "legacy" || can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "github_owner_id (numeric) is required when github_subject_format = \"immutable\"."
  }
}

variable "github_repository_id" {
  description = "Numeric ID of the repository (gh api repos/OWNER/REPO --jq .id). Required for the immutable format."
  type        = string
  default     = null

  validation {
    condition     = var.github_subject_format == "legacy" || can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id (numeric) is required when github_subject_format = \"immutable\"."
  }
}

variable "plan_retention_days" {
  description = "Days a saved Terraform plan is kept in the state bucket (plans/ prefix)"
  type        = number
  default     = 3
}

variable "github_main_branch" {
  description = "Branch whose pushes may build and push images"
  type        = string
  default     = "main"
}

variable "github_deploy_environment" {
  description = "GitHub Environment (with required reviewers) whose jobs may run terraform apply"
  type        = string
  default     = "production"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if the account already has one."
  type        = bool
  default     = true
}
