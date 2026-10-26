# Networking for the ECS platform, reusing the legacy VPC (main.tf).
#
#   AZ a (names[0])                       AZ b (names[1])
#   public  10.0.1.0/24 (legacy, EC2+NAT)  public  10.0.3.0/24 (new)   <- ALB in both
#   private 10.0.2.0/24 (legacy, unused)   private 10.0.4.0/24 (new)   <- ECS tasks in both
#
# Private egress: ONE NAT gateway in AZ a (cost-conscious; see docs/aws-platform.md),
# plus free gateway endpoints so DynamoDB and S3 (ECR image layers) traffic stays
# inside AWS and avoids NAT data-processing charges.

locals {
  name = "${var.project_name}-${var.environment}"
  # Pin with var.availability_zones so a change in the region's AZ list can never
  # move (and therefore replace) the subnets or the legacy EC2 instance.
  azs = var.availability_zones != null ? var.availability_zones : slice(data.aws_availability_zones.available.names, 0, 2)
}

# The VPC's default security group is deliberately NOT managed (Task 12): adopting it
# needs IAM rights on an untagged security group, which IAM cannot scope to this VPC.
# Nothing uses it: the ALB, ECS service and EC2 instance name their own security groups
# (terraform test workloads_never_use_the_default_security_group), the VPC endpoints are
# gateway endpoints, and the apply role cannot launch an instance into an untagged group.

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.3.0/24"
  availability_zone       = local.azs[1]
  map_public_ip_on_launch = false # only the ALB lives here

  tags = {
    Name = "${var.project_name}-public-subnet-b"
    Type = "Public"
  }
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.4.0/24"
  availability_zone = local.azs[1]

  tags = {
    Name = "${var.project_name}-private-subnet-b"
    Type = "Private"
  }
}

# --- NAT gateway (single, AZ a) --------------------------------------------------------

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = {
    Name = "${var.project_name}-nat-eip"
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id

  tags = {
    Name = "${var.project_name}-nat"
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = {
    Name = "${var.project_name}-private-rt"
  }
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private.id
}

# --- Gateway endpoints (no hourly or data charges) -------------------------------------

# DynamoDB: only the application table is reachable through this endpoint.
data "aws_iam_policy_document" "dynamodb_endpoint" {
  statement {
    sid       = "AppTableOnly"
    actions   = ["dynamodb:*"]
    resources = [aws_dynamodb_table.app.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
  policy            = data.aws_iam_policy_document.dynamodb_endpoint.json

  tags = {
    Name = "${var.project_name}-dynamodb-endpoint"
  }
}

# S3: ECR stores image layers in S3, and EC2 downloads the app bundle from S3.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]

  tags = {
    Name = "${var.project_name}-s3-endpoint"
  }
}

# --- VPC flow logs (rejected traffic) -------------------------------------------------
# REJECT only: records blocked connection attempts (scans, mis-routed traffic,
# security-group mistakes during the migration) at a fraction of the cost of ALL.

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  #checkov:skip=CKV_AWS_158:Encrypted at rest by CloudWatch Logs with AWS-managed keys; a customer-managed KMS key adds cost and key-policy work (see docs/security-exceptions.md)
  #checkov:skip=CKV_AWS_338:Retention set by var.log_retention_days (default 30) for cost (see docs/security-exceptions.md)
  name              = "/vpc/${local.name}/flow-logs"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name               = "${local.name}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

data "aws_iam_policy_document" "flow_logs_write" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = [aws_cloudwatch_log_group.vpc_flow_logs.arn, "${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.vpc_flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs_write.json
}

resource "aws_flow_log" "vpc" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "REJECT"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.vpc_flow_logs.arn
  iam_role_arn             = aws_iam_role.vpc_flow_logs.arn
  max_aggregation_interval = 600

  tags = {
    Name = "${local.name}-flow-logs"
  }
}
