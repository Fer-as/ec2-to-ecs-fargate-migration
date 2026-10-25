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
  description = "CIDR blocks allowed to reach the application over HTTP (port 80)"
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = alltrue([for c in var.allowed_cidr_blocks : can(cidrnetmask(c))])
    error_message = "allowed_cidr_blocks must contain valid IPv4 CIDR blocks."
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
