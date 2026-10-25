# syntax=docker/dockerfile:1

# Legacy Flask API: production container image.
#
# Stages:
#   builder - installs the pinned dependencies into a virtualenv (/opt/venv)
#   test    - builder + pytest + tests; run with:  docker build --target test ...
#   runtime - (default, last stage) slim image with only the venv and app code
#
# Base image pinned by digest so rebuilds use the same OS and Python layers.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Patched pip for the build (the base image ships pip 25.0.1, CVE-2026-13346).
# The virtualenv itself is created without pip, so pip never reaches runtime.
RUN python -m pip install --root-user-action=ignore "pip==26.2.1" \
 && python -m venv --without-pip /opt/venv

COPY app/requirements.txt /tmp/requirements.txt
RUN python -m pip --python /opt/venv/bin/python install \
        --root-user-action=ignore -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
FROM builder AS test

COPY requirements-dev.txt /src/requirements-dev.txt
COPY app/requirements.txt /src/app/requirements.txt
RUN python -m pip --python /opt/venv/bin/python install \
        --root-user-action=ignore -r /src/requirements-dev.txt

WORKDIR /src
COPY pytest.ini ./
COPY app/ ./app/
COPY tests/ ./tests/

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "pytest", "-v", "-rxXs", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# Set first so no RUN step writes .pyc files into the image.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_NAME=legacy-api \
    APP_VERSION=1.0.0 \
    ENVIRONMENT=production

# Unprivileged user with a stable numeric UID/GID (no home, no login shell).
# pip is removed from the runtime image: nothing is installed at runtime.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init \
        --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && python -m pip uninstall --yes --root-user-action=ignore pip \
 && rm -rf /root/.cache

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Code is owned by root and read-only for the app user.
COPY --chmod=0644 app/app.py app/wsgi.py ./

USER 10001:10001
EXPOSE 5000

# Calls the real /health endpoint with the standard library (no curl).
# urlopen raises on any non-2xx response, and the status must be exactly 200.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4).status == 200 else 1)"]

# Gunicorn runs as PID 1 (exec form), so it receives SIGTERM directly and
# shuts down gracefully, finishing in-flight requests within --graceful-timeout.
#
# ONE worker on purpose: orders and stock live in process memory, and every
# worker would keep its own copy (proven by tests/test_gunicorn_workers.py).
# This only keeps ONE container consistent. Several containers/ECS tasks still
# each have their own state, and all data is lost on restart. --max-requests is
# not used because recycling the worker would wipe the data.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "sync", \
     "--timeout", "60", \
     "--graceful-timeout", "25", \
     "--worker-tmp-dir", "/dev/shm", \
     "--no-control-socket", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:app"]
