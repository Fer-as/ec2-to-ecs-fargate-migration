# CI/CD: GitHub Actions, OIDC and approval-gated deployment

Status: `ci.yml` and `terraform-ci.yml` passed on GitHub for the pushed revision (Task 9).
Changes from Tasks 11-13 (IAM policy gate, pre-commit step) are **not yet verified on
GitHub**; they only count as CI-verified after they are pushed and the runs pass.
`deploy.yml` has never run.
Platform design: [aws-platform.md](aws-platform.md). Data/cutover rules:
[migration-state-runbook.md](migration-state-runbook.md). Accepted risks:
[security-exceptions.md](security-exceptions.md).

## 1. Workflows

| Workflow | Triggers | AWS access | What it does |
|---|---|---|---|
| `ci.yml` | PRs to main, pushes to main, manual, called by deploy | **none** | Linting (actionlint, yamllint, pre-commit hooks incl. Ruff, shellcheck); pytest incl. DynamoDB Local and multi-worker Gunicorn; pip-audit; image build; hardened container smoke test; non-root check; Trivy (full report + blocking gate); gitleaks; `CI gate` job |
| `terraform-ci.yml` | same | **none** | fmt; `init -backend=false` and `validate` for both roots; `terraform test` (mocked); user-data render, shellcheck and byte-identity check; Checkov (blocking); `Terraform gate` job |
| `deploy.yml` | **manual only**, main only | OIDC, 3 roles | gates → publish image → saved plan → **approval** → apply exactly that plan → verify |

All CI runs on every PR. There are no path filters, so required checks can never be
"skipped". Pull requests (including from forks) never get an OIDC token: the CI
workflows have `permissions: contents: read` and no `id-token`.
`pull_request_target` is not used.

Actions are pinned to full commit SHAs and tool images to digests. Python dependencies
are pinned (`app/requirements.txt`, `requirements-dev.txt`).

## 2. Security gates (all blocking)

| Gate | Fails when |
|---|---|
| pytest | any test fails (the 2 strict XFAILs document the original in-memory defect) |
| pip-audit `--strict` | any known vulnerability in runtime or dev dependencies |
| Trivy | a **fixable** HIGH/CRITICAL vulnerability or any secret in the image. Unfixed findings are listed in the summary and `trivy-reports` artifact (see security-exceptions.md) |
| gitleaks | a secret anywhere in Git history (output redacted) |
| Checkov | any finding without a reviewed inline exception |
| actionlint / yamllint / shellcheck | workflow or script errors |
| pre-commit (lint job) | trailing whitespace, missing final newline, CRLF, invalid YAML/JSON (incl. IAM policy templates), Ruff findings, merge markers, private keys, files over 500 KB, Terraform state/plans/local tfvars/key files/caches |
| rendered IAM policy gate (`ci/tests/test_rendered_policy_gate.py`) | pipeline IAM policy regressions: unknown actions, unsupported condition keys, wildcards, untagged-resource access, prohibited actions, IAM size quotas |
| `ci/tests/test_workflow_policy.py` | pipeline invariants broken (unpinned action, OIDC in CI, apply outside the gated job, `-auto-approve`, out-of-band ECS/ALB calls, …) |

No step uses `continue-on-error`, `--soft-fail` or similar.

### Pre-commit hooks (local, before every commit)

[.pre-commit-config.yaml](../.pre-commit-config.yaml) runs fast file-level checks on
each `git commit`: whitespace, final newline, LF line endings, YAML/JSON syntax,
yamllint on workflows, Ruff (rules in [ruff.toml](../ruff.toml)), merge-conflict markers,
private keys, large files, forbidden artefacts (state, plans, `terraform.tfvars`, keys,
caches), gitleaks on staged changes (same `.gitleaks.toml`, no extra allowlist) and
`terraform fmt`. Formatting hooks skip `evidence/` (raw output stays byte-for-byte);
the secret, key, merge-marker and size hooks still scan it.

Kept in CI only (too slow or need Docker/AWS-like services): Trivy, Checkov, image
build and smoke test, DynamoDB integration tests, `terraform validate`/`test`,
actionlint, shellcheck, pip-audit, full-history gitleaks. The CI lint job runs the same
hooks (`SKIP=gitleaks,terraform-fmt`, covered by other jobs), so `--no-verify` does not
bypass them.

PowerShell (Windows), from the repository root:

```powershell
# Once: install pre-commit into the project virtualenv and the Git hook
.\venv\Scripts\python.exe -m pip install "pre-commit==4.6.2"
.\venv\Scripts\pre-commit.exe install

# Before committing: run every hook on all tracked files
.\venv\Scripts\pre-commit.exe run --all-files

# New files are not tracked yet: check them explicitly (or stage them first)
.\venv\Scripts\pre-commit.exe run --files (git ls-files --others --exclude-standard)

# One hook only, e.g. Ruff
.\venv\Scripts\pre-commit.exe run ruff-check --all-files
```

Requirements: `terraform` on `PATH` for the `terraform-fmt` hook. The first run
downloads the hook environments; the gitleaks hook builds gitleaks with a Go toolchain
that pre-commit downloads itself. Hooks that fix files (whitespace, end of file, line
endings, `terraform fmt`) stop the commit; review the change, `git add` it and commit
again.

## 3. Deployment (`deploy.yml`)

```
preflight (main only, variables valid)
  ├─ ci.yml            (reused, blocking)
  └─ terraform-ci.yml  (reused, blocking)
        └─ publish  role gha-…-image-push
              build → Trivy gate on THIS image → push :<commit-sha> → read back sha256 digest
              └─ plan  role gha-…-plan
                    terraform plan -var app_image_digest=<digest> -out=tfplan
                    plan_guard.py (deployment kind rules) + sanitised summary
                    tfplan + plan.log → s3://<state-bucket>/plans/… (SSE, 3-day expiry)
                    └─ apply  environment "production" (APPROVAL) → role gha-…-apply
                          fetch tfplan → sha256 must match → terraform apply tfplan
                          verify_rollout.py: ECS stable, targets healthy, /health + /ready
```

**Deployment kinds** (`deployment_kind` input):
- `application`: only task definitions, the ECS service and the EC2 code bundle may change.
- `platform`: reviewed infrastructure or migration changes.

`ci/plan_guard.py` refuses an `application` plan that changes anything else, including
traffic weights or the storage mode. **A routine release can never move traffic.**
No pipeline run may delete or replace the DynamoDB table.

**Single owner:** Terraform owns task definitions, the ECS service, listener weights and
autoscaling. The pipeline never calls `ecs update-service`, `register-task-definition`
or `elbv2 modify-*`.

**Plan / apply safety:**
- The plan is saved; apply uses exactly that file (`terraform apply tfplan`). There is
  no re-plan after approval and no `-auto-approve`.
- The plan file's SHA-256 is passed from the plan job and checked before apply.
- Terraform also rejects a saved plan if the state changed since it was made (stale plan).
- Plans are stored in the private, TLS-only, versioned state bucket. They are never
  workflow artifacts, which are downloadable by anyone with read access (everyone, for a
  public repository). Plan text is never printed in the public log; reviewers read
  `plan.log` from S3 with their own AWS access.
- One deployment at a time (`concurrency: deploy-production`, no cancellation).
  GitHub keeps only the newest *pending* run in a concurrency group, so an older pending
  run is cancelled automatically.
- The apply job requests AWS credentials only **after** approval (fresh OIDC token, 1 h).

**Image promotion:**
- The image is tagged with the commit SHA. ECR tags are immutable, so on a re-run the
  existing image is reused.
- Terraform receives the **digest**; tags and `latest` are rejected by validation.

## 4. GitHub OIDC

Repositories created after 15 July 2026 use **immutable subjects**:
`repo:OWNER@OWNER_ID/REPO@REPO_ID:<context>`. Older repositories keep
`repo:OWNER/REPO:<context>` unless opted in (Settings → Actions → OIDC).

| Role | Job | Trusted `sub` (exact `StringEquals`, `aud = sts.amazonaws.com`) |
|---|---|---|
| `gha-legacy-api-image-push` | publish | `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/main` |
| `gha-legacy-api-plan` | plan | `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/main` |
| `gha-legacy-api-apply` | apply | `repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:production` |

Values to collect after creating the repository (all public, none secret):
```
gh api repos/OWNER/REPO --jq '{owner: .owner.login, owner_id: .owner.id, repository: .name, repository_id: .id, visibility: .visibility}'
gh api repos/OWNER/REPO/actions/oidc/customization/sub
```
The second call returns `use_immutable_subject` and `sub_claim_prefix`. The prefix must
equal the part of the bootstrap output `trusted_oidc_subjects` before `:ref:` or
`:environment:`. If `use_immutable_subject` is false (only possible for older
repositories), use `github_subject_format = "legacy"`.

**Verify before the first real run:**
- The publish job prints the token's actual `sub` (never the token).
- Compare it with `terraform -chdir=terraform/bootstrap output trusted_oidc_subjects`.
- GitHub documents the immutable format for branch refs. The environment form is
  expected to follow the same prefix, but **confirm it on the first run**.

`allowed-account-ids` on every credentials step blocks assuming a role in an unexpected
account.

## 5. Approval (GitHub Environment `production`)

GitHub facts:
- Environments on a personal **Free** account exist only for **public** repositories.
  Pro allows private ones, but required reviewers on private repositories need Enterprise.
- One approval from the listed reviewers (up to 6) is enough.
- **Prevent self-review** blocks the person who *triggered* the run from approving it.
- Admins can bypass protection rules unless **"Allow administrators to bypass configured
  protection rules"** is deselected.

**Personal portfolio repository (one maintainer): recommended setup**

| Setting | Value | Why |
|---|---|---|
| Visibility | Public | Required reviewers are available on Free |
| Required reviewers | You | The apply job pauses until you approve |
| Prevent self-review | **Off**, unless a second reviewer is listed | With only you as reviewer and this on, every deployment **deadlocks**: you trigger the run and may not approve it |
| Allow administrators to bypass | **Off** (deselect) | Otherwise you, as admin, could skip the gate |
| Deployment branches | Selected branches: `main` | Other branches cannot deploy |

Label this honestly as a **self-approval checkpoint**. It is a deliberate pause, recorded
by GitHub, to read the sanitised plan summary before AWS credentials are issued. It is
**not** an independent review.

To get real **independent approval**:
1. Add a second person (e.g. a mentor) as a required reviewer.
2. Turn **Prevent self-review** on.
3. Only that person can then approve your runs.

A manual `workflow_dispatch` trigger is never equivalent to either. It only proves that
someone with write access clicked "Run".

**Private repository without Enterprise:** no reviewer gate exists. Do not describe the
pipeline as approval-gated. Use the fallback: a separate dispatch-only apply workflow that
takes `run_id`, `commit_sha` and `plan_sha256`, verifies the saved plan and refuses
anything but the current `main`, with branch protection (PR review) as the human control.
The apply role's trust would then need the `ref:refs/heads/main` subject instead of
`environment:production`, which is a reviewed change in `terraform/bootstrap`.

**Current state (Task 12):** the repository is private on Free, so this workflow is
**not** protected by required reviewers. The apply trust stays `environment:production`,
`deploy.yml` is not run, and neither option above is active. The bootstrap
(`terraform/bootstrap`) is a separate, manual apply by the account owner after explicit
approval; it does not enable deployments.
The choice, and the permission matrix for the roles, are in
[iam-permission-matrix.md](iam-permission-matrix.md) section 6.

## 6. Repository settings to create (not secrets unless stated)

| Name | Kind | Value |
|---|---|---|
| `AWS_ACCOUNT_ID` | variable | 12-digit account |
| `AWS_REGION` | variable | e.g. `eu-west-2` |
| `AWS_IMAGE_PUSH_ROLE_ARN`, `AWS_PLAN_ROLE_ARN`, `AWS_APPLY_ROLE_ARN` | variables | bootstrap outputs |
| `ECR_REPOSITORY` | variable | `legacy-api` |
| `TF_STATE_BUCKET` | variable | bootstrap output `state_bucket_name` |
| `TF_PLAN_PREFIX` | variable | bootstrap output `saved_plans_prefix` (`plans/legacy-api`) |
| `TF_AVAILABILITY_ZONES` | variable | JSON, e.g. `["eu-west-2a","eu-west-2b"]` |
| `TF_ALLOWED_CIDR_BLOCKS` | **secret** (optional) | JSON list, e.g. `["<your-ip>/32"]`; direct EC2 access |
| `TF_VERIFICATION_CIDR_BLOCKS` | **secret** (optional) | JSON list of operator IPs for `X-Migration-Target` |
| `TF_ALARM_EMAIL` | **secret** (optional) | alarm email |

Personal values are secrets so they are masked in logs. No AWS keys are stored anywhere.

Also set:
- Branch protection on `main`: PR required, required status checks `CI gate` and
  `Terraform gate`.
- "Read repository contents" as the default workflow permission.

## 7. First deployment (no `-target`)

Every state-changing step below needs your explicit approval at the time.

1. **Bootstrap** (you, locally with admin credentials):
   `terraform -chdir=terraform/bootstrap apply`.
   This creates the state bucket, ECR and the OIDC roles. Then set the repository
   variables from the outputs.
2. **Deploy #1, `platform` kind** (production.tfvars: `ecs_service_enabled = false`,
   `traffic_stage = 0`, `legacy_storage_backend = "memory"`):
   - The image is pushed; Terraform creates the network, ALB (100% → EC2), empty table,
     IAM, task definitions **including the seed task**, alarms, and the legacy EC2.
   - No ECS service yet, because `/ready` would fail on an empty table.
3. **Seed once** (you, approved):
   `aws ecs run-task` with the `seed_command` output. Check the log for `Seed complete`.
   Re-running it is harmless.
4. **PR:** `ecs_service_enabled = true` → **Deploy #2, `platform`**. The service starts
   with 0% traffic; verification requires healthy targets and `/ready`.
5. **Write freeze, PR:** `legacy_storage_backend = "dynamodb"` → **Deploy #3,
   `platform`**. The plan shows the EC2 *replacement*. Then verify both backends via the
   header routes from your IP (runbook section 9).
6. **PRs:** `traffic_stage` 1 → 2 → 3, each a **`platform` deploy with approval**.
   Watch the dashboard between steps.
7. **Later releases:** `application` deploys only.

## 8. Rollback

- **Traffic:** PR setting `traffic_stage = 0` → `platform` deploy. The plan changes only
  the listener; this takes about a minute after approval.
- **Bad image:** the ECS circuit breaker rolls back a failing deployment automatically.
  Otherwise, run deploy from the last good commit (`application` kind).
- **Emergency (pipeline unavailable):** an administrator changes the listener weights in
  the console. Then immediately commit the same `traffic_stage` so Terraform matches
  reality; the next plan otherwise reverts it.

## 9. Negative tests to demonstrate (all verified locally, section 10)

| Demonstration | Expected |
|---|---|
| PR with a failing unit test | `CI gate` red; merge blocked |
| PR pinning `werkzeug==3.0.1` | pip-audit fails |
| Build on an old base image | Trivy gate fails (fixable HIGH/CRITICAL) |
| Committing a fake AWS key | gitleaks fails |
| Adding an SSH-open security group | Checkov fails |
| `application` deploy after changing `traffic_stage` | plan guard refuses |
| Unpinning an action / adding `id-token` to CI | workflow policy test fails |
| Deploy from a non-main branch | preflight fails |
| Approval rejected | apply never runs; no AWS credentials issued |

## 10. Evidence to capture before teardown

- Green `CI gate` and `Terraform gate` on a PR, plus one red run per negative test.
- A deploy run graph showing the approval pause, the plan summary (sanitised) and the
  published digest.
- ECR image list showing commit-SHA tags, immutability and the scan result.
- ECS service events (steady state), target group health for both groups, dashboard
  traffic split at each stage, alarm list.
- A rollback to stage 0 and its timing.
- `terraform output` (no secrets) and the bootstrap `trusted_oidc_subjects` alongside the
  `sub` printed by the run.
