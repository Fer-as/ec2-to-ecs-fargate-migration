# Task 5 evidence: shared state with DynamoDB (local only)

Real command output captured on 2026-10-24 against **DynamoDB Local 3.3.1** in Docker.
No real AWS account, table or credentials were used (dummy `local` credentials only).

| File | What it shows |
|---|---|
| 00-pytest-windows.txt | Full suite on Windows with DynamoDB Local (Gunicorn tests skip on Windows) |
| 01-pytest-linux-dynamodb-local.txt | Full suite in the Dockerfile `test` stage on Linux, incl. multi-worker Gunicorn tests for both backends, plus the original memory-backend defect shown as real failures |
| 02-docker-shared-state.txt | Production image: idempotent seed, two containers x 2 workers sharing one table, smoke test, cross-container consistency, concurrent oversell attempt, restart persistence, DynamoDB outage and recovery, readiness timing |
| 03-docker-build.txt | No-cache build of the final image, `docker build --check`, image size |
| 04-docker-final-image.txt | Final image in default memory mode and in DynamoDB mode; image contents (no credentials) |
| 05-dependency-and-image-scans.txt | pip-audit, pip check, Trivy vulnerability/secret scan of the final image, Trivy Dockerfile scan |

Key results:
- Memory backend (kept for compatibility): two Gunicorn workers still hand out duplicate
  order IDs and disagree on stock. This is the original defect, kept as a strict xfail.
- DynamoDB backend: the same multi-worker tests pass; two containers with 2 workers each
  show identical orders, stock and revenue; 24 concurrent 3-unit orders against 50 units
  produced exactly 16 successes and 8 "Insufficient stock", leaving stock at 2.
