# Shared application state (see app/storage.py and docs/migration-state-runbook.md).
# Key schema must match app/storage.py and app/seed_dynamodb.py:
#   pk (String) = PRODUCT | ORDER | SEQUENCE,  sk (Number) = id
#
# The table is NOT seeded by Terraform or by the app; run the seed task once.
# Teardown: set dynamodb_deletion_protection = false and apply before destroying.

resource "aws_dynamodb_table" "app" {
  #checkov:skip=CKV_AWS_119:Encrypted at rest with the AWS-managed key (aws/dynamodb); a customer-managed key adds cost and key-policy work (see docs/security-exceptions.md)
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "N"
  }

  point_in_time_recovery {
    enabled = true
  }

  # Encryption at rest with the AWS managed key (aws/dynamodb)
  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.dynamodb_deletion_protection

  tags = {
    Name = local.name
  }
}
