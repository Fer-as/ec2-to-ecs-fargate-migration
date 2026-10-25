# Legacy App Start Guide

Get the legacy application running in 5 minutes.

## Prerequisites

```bash
# Check AWS CLI
aws --version

# Check Terraform
terraform version

# Configure AWS (if not already done)
aws configure
```

## Deploy

```bash
# 1. Navigate to terraform directory
cd terraform

# 2. Copy example variables
cp terraform.tfvars.example terraform.tfvars

# 3. Edit variables (minimum required)
# Edit terraform.tfvars and set:
#   - aws_region
#   - allowed_cidr_blocks (your IP)

# 4. Initialize and deploy
terraform init
terraform apply

# Wait ~3-5 minutes for deployment
```

## Test

```bash
# Get application URL
APP_URL=$(terraform output -raw application_url)

# Health check
curl $APP_URL/health

# Test API
curl $APP_URL/api/v1/products
```

## Clean Up

```bash
terraform destroy
```

## Next Steps

1. Read `README.md` for full documentation
2. Review `ARCHITECTURE.md` for current setup
3. Read `DEPLOYMENT.md` for detailed deployment guide
4. Plan your ECS migration!

## Troubleshooting

**Instance not responding?**
- Wait 3-5 minutes after `terraform apply`
- Check security groups allow your IP
- SSH into instance (requires `key_pair_name` and `ssh_allowed_cidr_blocks`): `ssh -i ~/.ssh/key.pem ubuntu@<ip>`

**Application not working?**
- Check logs: `sudo journalctl -u flask-app -n 50`
- Validate setup: `bash /opt/flask-app/scripts/validate-setup.sh`
- Test locally: `curl http://localhost/health`

See `DEPLOYMENT.md` for detailed troubleshooting.
