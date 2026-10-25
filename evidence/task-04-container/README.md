# Task 4 evidence: container image `legacy-api:local`

Real command output captured on 2026-10-24 (Docker Desktop 29.8.0, Linux containers).
Personal paths are replaced with `<scan-dir>` / `<repo>` placeholders.

| File | What it shows |
|---|---|
| 00-docker-version.txt | Docker client/server version |
| 01-docker-build.txt | `docker build --no-cache` output, image size, layer history |
| 02-container-run-and-health.txt | `docker run`, Docker health status reaching `healthy`, health-check config |
| 03-api-smoke-test.txt | 17 endpoint checks against the running container (all PASS) |
| 04-non-root-verification.txt | Process UID/GID 10001, zero capabilities, read-only app code |
| 05-logs-and-graceful-stop.txt | Access log on stdout, error/app log on stderr, SIGTERM shutdown (exit 0) |
| 06-fresh-restart.txt | New container healthy again; previous orders gone (in-memory storage) |
| 07-security-validation.txt | Read-only rootfs + `--cap-drop ALL` + no-new-privileges run; no secrets/Git/Terraform/pip in image; packages vs pins |
| 08-trivy-scan.txt | Full Trivy 0.75.0 vulnerability, secret and Dockerfile scan |
| 08b-trivy-summary.txt | Trivy findings grouped by CVE, severity and fix status |
| 09-pytest-results.txt | Pytest in the Dockerfile `test` stage (Linux) and on Windows |
| 10-reproducibility.txt | Second `--no-cache` build compared layer by layer and file by file |
| smoke-test.sh | Script used for 03 and 07 |

Known limitation (not hidden): the app keeps orders and stock in process memory.
The image runs one Gunicorn worker so a single container is consistent, but several
containers/ECS tasks would each hold different data, and a restart loses all data.
This image is not safe for horizontally scaled, writable production traffic yet.
