# Task 7 evidence: CI/CD, OIDC and security gates (local validation only)

Captured on 2026-10-24. **The workflows have not run on GitHub Actions yet** and no AWS
account was used. "Job" files below executed the workflow's own `run:` scripts
(extracted from the YAML) locally with the same pinned tool images.

| File | What it shows |
|---|---|
| 01-lint-and-secrets.txt | actionlint (+ embedded shellcheck), yamllint --strict, shellcheck, gitleaks history scan: all exit 0 |
| 02-terraform-validate-test.txt | fmt, init -backend=false, validate (both roots); terraform test 17/17 (mocked providers) |
| 03-checkov.txt | Checkov gate: 334 passed, 0 failed, 30 reviewed inline exceptions (docs/security-exceptions.md) |
| 04-ci-test-job-linux.txt | ci.yml test job: 84 passed + 2 strict XFAIL (documented defect), 47 CI/policy tests, pip-audit clean |
| 05-ci-container-job.txt | ci.yml container job: hardened container healthy, UID 10001, 17/17 smoke checks, Trivy summary, gate passes |
| 06-negative-tests.txt | each gate fails on a deliberately bad input |

Not verifiable locally (requires GitHub + AWS, after approval): OIDC role assumption,
ECR push, remote-state plan/apply, environment approval, rollout verification.
