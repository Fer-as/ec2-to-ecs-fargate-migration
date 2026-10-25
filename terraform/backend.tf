# Remote state in the S3 bucket created by terraform/bootstrap.
# Locking uses an S3 lock file next to the state (no DynamoDB lock table).
#
#   terraform init -backend-config=backend.hcl
#
# backend.hcl holds the bucket and region (see backend.hcl.example; the values
# come from the bootstrap output "platform_backend_config").
# Offline checks: terraform init -backend=false
terraform {
  backend "s3" {
    key          = "legacy-api/platform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
