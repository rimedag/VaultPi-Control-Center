#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/vaultpi-control-center

sudo systemctl stop vaultpi-control-center || true
sudo systemctl stop vaultpi-cardputer-api || true
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.claude' \
  --exclude 'instance' \
  --exclude 'venv' \
  --exclude '.env' \
  --exclude '.env.*' \
  --include '.env.example' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '*.db' \
  --exclude '*.sqlite' \
  --exclude '*.zip' \
  --exclude '*.bundle' \
  --exclude '/_*' \
  --exclude 'backups' \
  ./ "$APP_DIR"/

cd "$APP_DIR"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt

if [ -f scripts/gitea-backup.sh ]; then
  sudo install -m 0755 scripts/gitea-backup.sh /usr/local/bin/gitea-backup.sh
fi
if [ -f scripts/gitea-sync-android.sh ]; then
  sudo install -m 0755 scripts/gitea-sync-android.sh /usr/local/bin/gitea-sync-android.sh
fi
if [ -f scripts/vaultpi-safe-shutdown.sh ]; then
  sudo install -m 0755 scripts/vaultpi-safe-shutdown.sh /usr/local/bin/vaultpi-safe-shutdown.sh
fi
if [ -f deploy/vaultpi-control-center.service ]; then
  sudo install -m 0644 deploy/vaultpi-control-center.service /etc/systemd/system/vaultpi-control-center.service
fi
if [ -f deploy/vaultpi-cardputer-api.service ]; then
  sudo install -m 0644 deploy/vaultpi-cardputer-api.service /etc/systemd/system/vaultpi-cardputer-api.service
fi
sudo systemctl daemon-reload

sudo systemctl restart vaultpi-control-center
sudo systemctl enable vaultpi-cardputer-api >/dev/null 2>&1 || true
sudo systemctl restart vaultpi-cardputer-api
sudo systemctl status vaultpi-control-center --no-pager
sudo systemctl status vaultpi-cardputer-api --no-pager
