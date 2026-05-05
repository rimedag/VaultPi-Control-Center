#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/vaultpi-control-center
SERVICE_FILE=/etc/systemd/system/vaultpi-control-center.service
ENV_FILE=/opt/vaultpi-control-center/.env
LEGACY_ENV_FILE=/etc/default/vaultpi-control-center

sudo systemctl disable --now vaultpi-control-center || true
sudo rm -f "$SERVICE_FILE"
sudo rm -f "$ENV_FILE" "$LEGACY_ENV_FILE"
sudo systemctl daemon-reload
sudo rm -rf "$APP_DIR"

echo "VaultPi Control Center removed."
