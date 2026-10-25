output "state_bucket_name" {
  description = "Bucket for the platform root's remote state (use in backend.hcl)"
  value       = aws_s3_bucket.state.bucket
}

output "ecr_repository_url" {
  description = "Push images here; deploy the platform with the image digest"
  value       = aws_ecr_repository.app.repository_url
}

output "github_plan_role_arn" {
  value = aws_iam_role.plan.arn
}

output "github_image_push_role_arn" {
  value = aws_iam_role.image_push.arn
}

output "github_apply_role_arn" {
  value = aws_iam_role.apply.arn
}

output "platform_backend_config" {
  description = "Contents for terraform/backend.hcl"
  value       = <<-EOT
    bucket = "${aws_s3_bucket.state.bucket}"
    region = "${var.aws_region}"
  EOT
}

output "trusted_oidc_subjects" {
  description = "Exact token subjects the roles trust; compare with the 'sub' printed by the deploy workflow"
  value = {
    image_push = local.sub_main
    plan       = local.sub_main
    apply      = local.sub_environment
  }
}

output "saved_plans_prefix" {
  description = "S3 prefix (in the state bucket) for saved plans; repository variable TF_PLAN_PREFIX"
  value       = local.plans_prefix
}
