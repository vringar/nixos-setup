#!/usr/bin/env bash
# Diagnostic script for wg-quick-sect failures
# Run as root or with sudo outside the Claude sandbox

set -euo pipefail

echo "=== wg-quick-sect service status ==="
systemctl status wg-quick-sect.service --no-pager || true

echo ""
echo "=== Recent journal entries ==="
journalctl -u wg-quick-sect.service --no-pager -n 50 || true

echo ""
echo "=== agenix activation scripts ran? ==="
journalctl -b --no-pager -g "agenix" | tail -20 || true

echo ""
echo "=== /run/agenix contents ==="
ls -la /run/agenix/ 2>&1 || echo "(empty or missing)"

echo ""
echo "=== Does sect.conf exist? ==="
if [ -f /run/agenix/sect.conf ]; then
    echo "YES — file exists, size=$(stat -c%s /run/agenix/sect.conf) bytes, mode=$(stat -c%a /run/agenix/sect.conf)"
    echo "--- Content preview (first 5 lines) ---"
    head -5 /run/agenix/sect.conf
else
    echo "NO — /run/agenix/sect.conf does not exist"
fi

echo ""
echo "=== Identity key available? ==="
ls -la /home/vringar/.ssh/github_key 2>&1 || echo "Key not found"

echo ""
echo "=== wg-quick dry run (what would happen) ==="
wg-quick up /run/agenix/sect.conf 2>&1 || true
