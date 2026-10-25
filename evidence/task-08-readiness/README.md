# Task 8 evidence: publication readiness (local only)

Captured on 2026-10-24. Nothing was pushed; no GitHub settings or AWS resources changed.

| File | What it shows |
|---|---|
| 01-readiness-checks.txt | First-push trigger analysis (only CI and Terraform CI run; deploy is manual-only), pinned action SHAs resolve, actionlint / yamllint / shellcheck, gitleaks on history and on all publication candidates, Trivy secret scan |
| 02-terraform-checkov-tests.txt | fmt, validate and mocked tests for both roots (5 + 17), Checkov 334/0/30, CI and policy tests, application tests |
