output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "public_subnet_id" {
  description = "Public Subnet ID"
  value       = aws_subnet.public.id
}

output "private_subnet_id" {
  description = "Private Subnet ID (for future ECS deployment)"
  value       = aws_subnet.private.id
}

output "ec2_instance_id" {
  description = "EC2 Instance ID"
  value       = aws_instance.app.id
}

output "ec2_instance_private_ip" {
  description = "EC2 Instance Private IP"
  value       = aws_instance.app.private_ip
}

output "ec2_instance_public_ip" {
  description = "EC2 Instance Public IP (Elastic IP)"
  value       = aws_eip.app.public_ip
}

output "application_url" {
  description = "Application URL"
  value       = var.domain_name != "" ? "http://${var.domain_name}" : "http://${aws_eip.app.public_ip}"
}

output "security_group_id" {
  description = "EC2 Security Group ID"
  value       = aws_security_group.ec2.id
}

output "s3_bucket_name" {
  description = "S3 bucket name for application files"
  value       = aws_s3_bucket.app.id
}

output "route53_record" {
  description = "Route53 record (if configured)"
  value       = var.domain_name != "" && var.route53_zone_id != "" ? aws_route53_record.app[0].fqdn : null
}

# --- Migration platform ---------------------------------------------------------------------

output "alb_url" {
  description = "Public URL of the ALB (EC2 and ECS behind weighted routing)"
  value       = "http://${aws_lb.app.dns_name}"
}

output "traffic_weights" {
  description = "Current ALB weights for the selected traffic_stage"
  value       = { stage = var.traffic_stage, ec2 = local.weights.ec2, ecs = local.weights.ecs }
}

output "legacy_storage_backend" {
  value = var.legacy_storage_backend
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.app.name
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Null until app_image_digest is set"
  value       = local.ecs_service_enabled ? aws_ecs_service.app[0].name : null
}

output "app_image" {
  description = "Image deployed to ECS (repository@digest)"
  value       = local.app_image
}

output "private_subnet_ids" {
  value = [aws_subnet.private.id, aws_subnet.private_b.id]
}

output "ecs_security_group_id" {
  value = aws_security_group.ecs.id
}

output "seed_command" {
  description = "Run ONCE to initialise the table (safe to re-run: never overwrites)"
  value = local.ecs_enabled ? join(" ", [
    "aws ecs run-task --region ${var.aws_region}",
    "--cluster ${aws_ecs_cluster.main.name}",
    "--task-definition ${aws_ecs_task_definition.seed[0].family}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${aws_subnet.private.id},${aws_subnet.private_b.id}],securityGroups=[${aws_security_group.ecs.id}],assignPublicIp=DISABLED}'",
  ]) : null
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.migration.dashboard_name
}

output "ecs_target_group_arn" {
  description = "Used by ci/verify_rollout.py"
  value       = aws_lb_target_group.ecs.arn
}

output "ec2_target_group_arn" {
  description = "Used by ci/verify_rollout.py"
  value       = aws_lb_target_group.ec2.arn
}
