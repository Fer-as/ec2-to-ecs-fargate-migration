# Migration controls for the production deployment (committed, reviewed in PRs).
# Used by .github/workflows/deploy.yml together with repository variables/secrets
# for account-specific values (region, AZs, operator CIDRs, alarm email).
#
# Changing anything here requires deployment_kind = "platform"; an "application"
# deployment refuses any plan that changes traffic weights or the storage mode.
#
# First deployment: ecs_service_enabled = false (image + seed task definition only).
# After the seed task has run once: ecs_service_enabled = true.
ecs_service_enabled = true

# "memory" until the table is seeded and the write freeze starts, then "dynamodb"
# (replaces the EC2 instance)
legacy_storage_backend = "memory"

# 0 = EC2 100% | 1 = EC2 90 / ECS 10 | 2 = 50 / 50 | 3 = ECS 100%   (rollback = 0)
traffic_stage = 0

web_concurrency = 2
