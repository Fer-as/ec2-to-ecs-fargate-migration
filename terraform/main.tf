terraform {
  required_version = ">= 1.11.0" # S3 native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project     = var.project_name
        Environment = var.environment
        ManagedBy   = "Terraform"
      },
      var.tags
    )
  }
}

# Latest official Canonical Ubuntu 24.04 LTS (Noble) AMI.
# The scripts (apt, ubuntu user, python3.12, sites-available) target Ubuntu.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

# VPC
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

# Public Subnet (for EC2 with public IP)
resource "aws_subnet" "public" {
  #checkov:skip=CKV_AWS_130:Legacy baseline: the EC2 bootstrap needs internet access at first boot, before the Elastic IP is attached (see docs/security-exceptions.md)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = local.azs[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-public-subnet"
    Type = "Public"
  }
}

# Private Subnet (for future ECS tasks)
resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = local.azs[0]

  tags = {
    Name = "${var.project_name}-private-subnet"
    Type = "Private"
  }
}

data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:Only a fallback; var.availability_zones pins the AZs (set in every real deployment) (see docs/security-exceptions.md)
  state = "available"
}

# Route Table for Public Subnet
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project_name}-public-rt"
  }
}

# Route Table Association for Public Subnet
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Security Group for EC2 Instance
resource "aws_security_group" "ec2" {
  #checkov:skip=CKV_AWS_260:False positive: Checkov renders the empty dynamic HTTP block as a rule without a source; by default no direct rule exists (terraform test secure_network_defaults) and validation rejects 0.0.0.0/0 (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_24:False positive: SSH is only created from var.ssh_allowed_cidr_blocks, whose validation rejects 0.0.0.0/0 (default: no SSH rule) (see docs/security-exceptions.md)
  name_prefix = "${var.project_name}-ec2-"
  description = "Security group for EC2 instance running Flask app"
  vpc_id      = aws_vpc.main.id

  # Direct HTTP for baseline validation; set allowed_cidr_blocks = [] to close it
  # once all traffic goes through the ALB.
  dynamic "ingress" {
    for_each = length(var.allowed_cidr_blocks) > 0 ? [1] : []
    content {
      description = "HTTP from allowed CIDR"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = var.allowed_cidr_blocks
    }
  }

  # Migration path: Nginx on port 80 from the ALB only
  ingress {
    description     = "HTTP from the ALB"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # No HTTPS rule: nothing on the instance listens on 443.

  # SSH stays closed unless ssh_allowed_cidr_blocks is set explicitly.
  dynamic "ingress" {
    for_each = length(var.ssh_allowed_cidr_blocks) > 0 ? [1] : []
    content {
      description = "SSH from explicitly allowed CIDRs"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.ssh_allowed_cidr_blocks
    }
  }

  # Outbound limited to HTTP/HTTPS: apt mirrors (80); pip, AWS CLI, S3, DynamoDB (443).
  # DNS and NTP use link-local AWS endpoints, which security groups do not filter.
  egress {
    description = "HTTP out (apt mirrors)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTPS out (pip, AWS APIs)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ec2-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# IAM Role for EC2 Instance
resource "aws_iam_role" "ec2" {
  name_prefix = "${var.project_name}-ec2-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-ec2-role"
  }
}

# IAM Instance Profile
resource "aws_iam_instance_profile" "ec2" {
  name_prefix = "${var.project_name}-ec2-"
  role        = aws_iam_role.ec2.name
}

# Application bundle for the EC2 instance.
# Built from an allowlist, so nothing else in the repository (venv, .git, tests,
# caches, Terraform state/plans/providers, .env files, keys) can be packaged.
locals {
  repo_root = "${path.module}/.."

  app_bundle_files = setunion(
    fileset(local.repo_root, "app/*.py"),
    fileset(local.repo_root, "app/requirements.txt"),
    fileset(local.repo_root, "nginx/default.conf"),
    fileset(local.repo_root, "systemd/app.service"),
    fileset(local.repo_root, "scripts/*.sh"),
  )
}

data "archive_file" "app" {
  type        = "zip"
  output_path = "${path.module}/app.zip"

  dynamic "source" {
    for_each = local.app_bundle_files
    content {
      content  = file("${local.repo_root}/${source.value}")
      filename = source.value
    }
  }
}

# Upload application to S3 bucket (for EC2 to download)
resource "aws_s3_bucket" "app" {
  #checkov:skip=CKV_AWS_18:Bundle bucket readable only by the EC2 role; CloudTrail covers API access, a log bucket is out of scope (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_144:Bundle is rebuilt from Git on every apply; replication adds no recovery value (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) at rest; a customer-managed KMS key adds cost and key-policy work (see docs/security-exceptions.md)
  #checkov:skip=CKV2_AWS_62:No consumer for event notifications on the bundle bucket (see docs/security-exceptions.md)
  bucket_prefix = "${var.project_name}-app-"

  tags = {
    Name = "${var.project_name}-app-bucket"
  }
}

resource "aws_s3_bucket_versioning" "app" {
  bucket = aws_s3_bucket.app.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  bucket = aws_s3_bucket.app.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Old bundle versions are only needed for a short rollback window
resource "aws_s3_bucket_lifecycle_configuration" "app" {
  bucket = aws_s3_bucket.app.id

  rule {
    id     = "expire-old-bundles"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "app" {
  bucket = aws_s3_bucket.app.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "app" {
  bucket = aws_s3_bucket.app.id
  key    = "app.zip"
  source = data.archive_file.app.output_path
  # filemd5() cannot read files created during the same run; use the archive's own hash.
  etag = data.archive_file.app.output_md5
}

# S3 bucket policy for EC2 access
resource "aws_iam_role_policy" "ec2_s3_access" {
  name_prefix = "${var.project_name}-ec2-s3-"
  role        = aws_iam_role.ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.app.arn}/*"
      }
    ]
  })
}

# User data script for EC2 instance (plain UTF-8 shell script).
# Output is captured by cloud-init in /var/log/cloud-init-output.log.
# With legacy_storage_backend = "memory" the rendered script is byte-identical to
# the previous inline version; "dynamodb" adds /etc/flask-app/flask-app.env.
locals {
  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    app_bucket          = aws_s3_bucket.app.id
    aws_region          = var.aws_region
    storage_backend     = var.legacy_storage_backend
    dynamodb_table_name = aws_dynamodb_table.app.name
  })
}

resource "terraform_data" "legacy_storage_mode" {
  input = var.legacy_storage_backend
}

# EC2 Instance
resource "aws_instance" "app" {
  #checkov:skip=CKV_AWS_126:Legacy baseline kept unchanged; 5-minute basic monitoring plus ALB target-health alarms (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_135:False positive: t3 instance types are EBS-optimised by default (see docs/security-exceptions.md)
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null

  # Plain script: the provider base64-encodes it for the EC2 API.
  # user_data_replace_on_change is left at false on purpose: changing user data
  # then updates the stored script (stop/start) but does not re-run it or replace
  # the instance. Redeploying needs an explicit `terraform apply -replace`.
  user_data = local.user_data

  # Ensure S3 object exists before instance starts
  depends_on = [aws_s3_object.app]

  lifecycle {
    # A newer Canonical AMI must not silently replace the rollback target mid-migration.
    ignore_changes = [ami]
    # Switching legacy_storage_backend must re-run user data, which needs a new
    # instance; the plan then shows the replacement explicitly.
    replace_triggered_by = [terraform_data.legacy_storage_mode]
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  tags = {
    Name = "${var.project_name}-ec2-instance"
  }
}

# Elastic IP for EC2 (allows easy DNS cutover later)
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = "${var.project_name}-eip"
  }
}

# Route53 Record (optional - for easy cutover to ALB later)
resource "aws_route53_record" "app" {
  count   = var.domain_name != "" && var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  records = [aws_eip.app.public_ip]
  ttl     = 300 # 5 minutes TTL for easy cutover
}
