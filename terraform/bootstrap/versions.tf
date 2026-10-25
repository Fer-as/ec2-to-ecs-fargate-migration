terraform {
  # 1.11+ for native S3 state locking (use_lockfile) in the platform root.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Bootstrap starts with LOCAL state, because it creates the state bucket.
  # After the first apply you may move this state into the bucket:
  #   1. uncomment the block below and fill in the bucket name (output state_bucket_name)
  #   2. terraform init -migrate-state
  # backend "s3" {
  #   bucket       = "<state_bucket_name>"
  #   key          = "legacy-api/bootstrap.tfstate"
  #   region       = "<aws_region>"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Stack       = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
