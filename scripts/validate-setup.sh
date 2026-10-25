#!/bin/bash
# Validation script to check if the application is properly set up
# Run this on the EC2 instance after deployment

set -euo pipefail

echo "Validating application setup..."
echo "================================"
echo ""

ERRORS=0
WARNINGS=0

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then 
    echo "⚠️  Running as non-root. Some checks may require sudo."
fi

# Check application directory
echo -n "Checking application directory... "
if [ -d "/opt/flask-app" ]; then
    echo "✓ Found"
else
    echo "✗ Not found"
    ERRORS=$((ERRORS + 1))
fi

# Check application files
echo -n "Checking app.py... "
if [ -f "/opt/flask-app/app/app.py" ]; then
    echo "✓ Found"
else
    echo "✗ Not found"
    ERRORS=$((ERRORS + 1))
fi

# Check Python virtual environment
echo -n "Checking Python virtual environment... "
if [ -d "/opt/flask-app/venv" ]; then
    echo "✓ Found"
    if [ -f "/opt/flask-app/venv/bin/gunicorn" ]; then
        echo "  ✓ Gunicorn installed"
    else
        echo "  ✗ Gunicorn not found"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "✗ Not found"
    ERRORS=$((ERRORS + 1))
fi

# Check systemd service
echo -n "Checking systemd service... "
if systemctl list-unit-files | grep -q flask-app.service; then
    echo "✓ Found"
    if systemctl is-active --quiet flask-app; then
        echo "  ✓ Service is running"
    else
        echo "  ✗ Service is not running"
        ERRORS=$((ERRORS + 1))
    fi
    if systemctl is-enabled --quiet flask-app; then
        echo "  ✓ Service is enabled"
    else
        echo "  ⚠️  Service is not enabled"
        WARNINGS=$((WARNINGS + 1))
    fi
else
    echo "✗ Not found"
    ERRORS=$((ERRORS + 1))
fi

# Check Nginx
echo -n "Checking Nginx... "
if command -v nginx &> /dev/null; then
    echo "✓ Installed"
    if systemctl is-active --quiet nginx; then
        echo "  ✓ Service is running"
    else
        echo "  ✗ Service is not running"
        ERRORS=$((ERRORS + 1))
    fi
    if nginx -t &> /dev/null; then
        echo "  ✓ Configuration is valid"
    else
        echo "  ✗ Configuration has errors"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "✗ Not installed"
    ERRORS=$((ERRORS + 1))
fi

# Check port 5000 (Flask app)
echo -n "Checking Flask app on port 5000... "
if netstat -tlnp 2>/dev/null | grep -q ":5000" || ss -tlnp 2>/dev/null | grep -q ":5000"; then
    echo "✓ Listening"
else
    echo "✗ Not listening"
    ERRORS=$((ERRORS + 1))
fi

# Check port 80 (Nginx)
echo -n "Checking Nginx on port 80... "
if netstat -tlnp 2>/dev/null | grep -q ":80" || ss -tlnp 2>/dev/null | grep -q ":80"; then
    echo "✓ Listening"
else
    echo "✗ Not listening"
    ERRORS=$((ERRORS + 1))
fi

# Check log directories
echo -n "Checking log directories... "
if [ -d "/var/log/flask-app" ]; then
    echo "✓ Found"
    if [ -f "/var/log/flask-app/access.log" ]; then
        echo "  ✓ Access log exists"
    fi
    if [ -f "/var/log/flask-app/error.log" ]; then
        echo "  ✓ Error log exists"
    fi
else
    echo "✗ Not found"
    ERRORS=$((ERRORS + 1))
fi

# Test health endpoint
echo -n "Testing health endpoint... "
if curl -f -s http://localhost/health > /dev/null 2>&1; then
    echo "✓ Accessible"
else
    echo "✗ Not accessible"
    ERRORS=$((ERRORS + 1))
fi

# Test API endpoint
echo -n "Testing API endpoint... "
if curl -f -s http://localhost/api/v1/products > /dev/null 2>&1; then
    echo "✓ Accessible"
else
    echo "✗ Not accessible"
    ERRORS=$((ERRORS + 1))
fi

# Check disk space
echo -n "Checking disk space... "
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -lt 80 ]; then
    echo "✓ OK (${DISK_USAGE}% used)"
else
    echo "⚠️  High usage (${DISK_USAGE}% used)"
    WARNINGS=$((WARNINGS + 1))
fi

echo ""
echo "================================"
if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo "✓ All checks passed!"
    exit 0
elif [ $ERRORS -eq 0 ]; then
    echo "✓ All critical checks passed (${WARNINGS} warning(s))"
    exit 0
else
    echo "✗ Found ${ERRORS} error(s) and ${WARNINGS} warning(s)"
    exit 1
fi
