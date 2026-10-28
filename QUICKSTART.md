# Quickstart for reviewers

How to review this project and run it locally, without touching any AWS environment.

> **The AWS demo was decommissioned on 28 October 2026**
> ([teardown record](evidence/aws-teardown-2026-10-28/README.md)). Nothing is running,
> and the approval-gated [deploy.yml](.github/workflows/deploy.yml) workflow is disabled.
> Don't run `terraform init`, `apply` or `destroy` in `terraform/` against the remote
> backend. Use the offline checks below.

## 1. Read the results (2 minutes)

1. [README.md](README.md): overview, architecture and migration results.
2. [evidence/live-migration-2026-10-28](evidence/live-migration-2026-10-28/README.md): runs,
   commits, sampled health checks and the shared-DynamoDB test.
3. [docs/architecture/](docs/architecture/README.md): platform and migration diagrams.

## 2. Run the tests locally

```bash
python -m venv venv
venv/bin/pip install -r requirements-dev.txt     # Windows: venv\Scripts\pip
venv/bin/python -m pytest tests ci/tests -q       # some tests run only on Linux
```

Terraform checks, offline (no credentials or backend needed):

```bash
terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate
terraform -chdir=terraform test               # mocked providers
terraform -chdir=terraform/bootstrap init -backend=false && terraform -chdir=terraform/bootstrap test
```

## 3. Run the container locally

```bash
docker build -t legacy-api:local .
docker run --rm -p 5000:5000 legacy-api:local      # in-memory storage, one worker
curl http://localhost:5000/health
curl http://localhost:5000/api/v1/products
```

Shared DynamoDB storage can be tried against DynamoDB Local. See
[docs/migration-state-runbook.md](docs/migration-state-runbook.md) and
[evidence/task-05-shared-state](evidence/task-05-shared-state/README.md).

## 4. Live demo (retired)

The live demo ran on 28 October 2026 and has been torn down. The endpoints listed in
[docs/demo-walkthrough.md](docs/demo-walkthrough.md) are kept only as a historical record
and no longer respond. The measured results are in the
[evidence](evidence/live-migration-2026-10-28/README.md).

## Original starter instructions

The original single-EC2 quickstart from the CoderCo starter (`terraform init/apply` with a
local `terraform.tfvars`) no longer applies to this repository. It is preserved for
reference in [docs/legacy-starter-readme.md](docs/legacy-starter-readme.md).
