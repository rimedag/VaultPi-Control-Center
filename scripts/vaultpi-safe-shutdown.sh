#!/bin/bash
set -eu

LOG_FILE="/var/log/vaultpi/safe-shutdown.log"
TS=$(date --iso-8601=seconds)

mkdir -p /var/log/vaultpi
{
  echo "[$TS] Requested safe shutdown"
  systemctl stop gitea || true
  systemctl stop vaultintel-backend || true
  sync
  shutdown -h now "VaultPi safe shutdown requested from web console"
} >> "$LOG_FILE" 2>&1 &

echo "Safe shutdown queued. The Raspberry Pi should power off in a few seconds."
exit 0
