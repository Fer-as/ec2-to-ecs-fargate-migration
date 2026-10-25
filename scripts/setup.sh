#!/bin/bash
# Setup script for the EC2 instance (Ubuntu 24.04 LTS, Python 3.12)
# Installs and configures the Flask application, Gunicorn (systemd) and Nginx.
# Expects the application bundle to be extracted to /opt/flask-app already
# (done by the Terraform user data). Must run as root.

set -euo pipefail

LOG_FILE="/var/log/app-setup.log"
APP_DIR="/opt/flask-app"
APP_USER="ubuntu"
PYTHON="python3.12"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

trap 'log "ERROR: setup failed at line $LINENO (exit code $?)"' ERR

# Wait until a URL answers with HTTP 2xx (up to ~60 seconds)
wait_for_url() {
    local url=$1
    for _ in $(seq 1 30); do
        if curl -fs -o /dev/null --max-time 2 "$url"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

log "Starting application setup..."

if [ "$(id -u)" -ne 0 ]; then
    log "ERROR: setup.sh must run as root"
    exit 1
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
    log "ERROR: user '$APP_USER' does not exist (expected on Ubuntu AMIs)"
    exit 1
fi

for f in app/app.py app/wsgi.py app/requirements.txt nginx/default.conf systemd/app.service; do
    if [ ! -f "$APP_DIR/$f" ]; then
        log "ERROR: missing $APP_DIR/$f - the application bundle is incomplete"
        exit 1
    fi
done

# Install system packages. Wait for the apt lock, because Ubuntu runs
# automatic updates on first boot.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
APT=(apt-get -y -qq
     -o DPkg::Lock::Timeout=600
     -o Dpkg::Options::=--force-confdef
     -o Dpkg::Options::=--force-confold)

log "Updating system packages..."
"${APT[@]}" update
"${APT[@]}" upgrade

log "Installing system packages..."
"${APT[@]}" install "$PYTHON" "${PYTHON}-venv" nginx curl

# Create directories
log "Creating application directories..."
mkdir -p /var/log/flask-app
chown "$APP_USER:$APP_USER" /var/log/flask-app

# Create Python virtual environment
log "Setting up Python virtual environment..."
"$PYTHON" -m venv "$APP_DIR/venv"

log "Installing Python dependencies..."
# Ubuntu's bundled pip (24.0) is affected by CVE-2026-13346; pin a patched pip first.
"$APP_DIR/venv/bin/pip" install --no-cache-dir -q "pip==26.2.1"
"$APP_DIR/venv/bin/pip" install --no-cache-dir -q -r "$APP_DIR/app/requirements.txt"

if [ ! -x "$APP_DIR/venv/bin/gunicorn" ]; then
    log "ERROR: gunicorn was not installed into $APP_DIR/venv"
    exit 1
fi

# Configure Nginx
log "Configuring Nginx..."
install -m 644 "$APP_DIR/nginx/default.conf" /etc/nginx/sites-available/flask-app
ln -sf /etc/nginx/sites-available/flask-app /etc/nginx/sites-enabled/flask-app
rm -f /etc/nginx/sites-enabled/default
nginx -t

# Configure systemd service
log "Configuring systemd service..."
install -m 644 "$APP_DIR/systemd/app.service" /etc/systemd/system/flask-app.service
systemctl daemon-reload

# Set permissions
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Start services (restart, so re-running the script applies new code)
log "Starting services..."
systemctl enable flask-app.service
systemctl restart flask-app.service
systemctl enable nginx
systemctl restart nginx

# Readiness: systemd reports Gunicorn active as soon as it binds the port,
# before workers have loaded the app, so check over HTTP instead.
log "Waiting for Gunicorn to answer on 127.0.0.1:5000..."
if ! wait_for_url "http://127.0.0.1:5000/health"; then
    log "ERROR: Flask application did not become healthy"
    systemctl status flask-app --no-pager || true
    journalctl -u flask-app -n 50 --no-pager || true
    exit 1
fi

log "Waiting for Nginx to proxy requests on port 80..."
if ! wait_for_url "http://127.0.0.1/health" || ! wait_for_url "http://127.0.0.1/api/v1/products"; then
    log "ERROR: Nginx is not proxying requests to the application"
    systemctl status nginx --no-pager || true
    tail -n 50 /var/log/nginx/flask-error.log || true
    exit 1
fi

# Readiness through Nginx: in dynamodb mode this proves the table is reachable,
# seeded and permitted by the instance role (in memory mode it is always ready).
log "Waiting for /ready (storage backend reachable and initialised)..."
if ! wait_for_url "http://127.0.0.1/ready"; then
    log "ERROR: /ready did not return 200 - check /etc/flask-app/flask-app.env, the instance role and the seed step"
    journalctl -u flask-app -n 50 --no-pager || true
    exit 1
fi

log "Setup completed successfully!"
log "Application is available on port 80"
log "Logs are available at:"
log "  - Application: /var/log/flask-app/"
log "  - Nginx: /var/log/nginx/"
