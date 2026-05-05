#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="/opt/vaultpi-control-center"
SERVICE_FILE="/etc/systemd/system/vaultpi-control-center.service"
ENV_FILE="${APP_DIR}/.env"
ENV_EXAMPLE="${APP_DIR}/.env.example"
SOURCE_ENV_EXAMPLE="${REPO_ROOT}/.env.example"
APP_USER="${VAULTPI_USER:-${SUDO_USER:-pi}}"
APP_GROUP=""
SUDO=""

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "Install user '${APP_USER}' does not exist. Run with sudo from your normal Pi user, or set VAULTPI_USER." >&2
  exit 1
fi
APP_GROUP="$(id -gn "$APP_USER")"

for cmd in python3 rsync systemctl install; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
done

echo "Installing VaultPi Control Center to ${APP_DIR}"
echo "Service will run as ${APP_USER}:${APP_GROUP}"

$SUDO mkdir -p "$APP_DIR"
$SUDO rsync -a --delete \
  --exclude '.git' \
  --exclude 'instance' \
  --exclude 'venv' \
  --exclude '.env' \
  --include '.env.example' \
  --exclude '.env.*' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '*.db' \
  --exclude '*.sqlite' \
  --exclude '*.zip' \
  "$REPO_ROOT"/ "$APP_DIR"/

cd "$APP_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

$SUDO install -d -m 0755 /usr/local/bin
$SUDO install -m 0755 scripts/gitea-backup.sh /usr/local/bin/gitea-backup.sh
$SUDO install -m 0755 scripts/gitea-sync-android.sh /usr/local/bin/gitea-sync-android.sh
$SUDO install -m 0755 scripts/vaultpi-safe-shutdown.sh /usr/local/bin/vaultpi-safe-shutdown.sh

if [ ! -s "$ENV_EXAMPLE" ] && [ -s "$SOURCE_ENV_EXAMPLE" ]; then
  cp "$SOURCE_ENV_EXAMPLE" "$ENV_EXAMPLE"
fi

if [ ! -s "$ENV_FILE" ]; then
  if [ ! -s "$ENV_EXAMPLE" ]; then
    echo "Missing ${ENV_EXAMPLE}; cannot create ${ENV_FILE}" >&2
    exit 1
  fi
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "Created ${ENV_FILE} from .env.example"
fi

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    tr -dc 'a-f0-9' </dev/urandom | head -c 64
  fi
}

if grep -Eq '^VAULTPI_SECRET_KEY=replace-with-a-long-random-secret$|^VAULTPI_SECRET_KEY=$' "$ENV_FILE"; then
  VAULTPI_SECRET_KEY="$(gen_secret)"
  if grep -q '^VAULTPI_SECRET_KEY=' "$ENV_FILE"; then
    sed -i "s|^VAULTPI_SECRET_KEY=.*|VAULTPI_SECRET_KEY=${VAULTPI_SECRET_KEY}|" "$ENV_FILE"
  else
    printf '\nVAULTPI_SECRET_KEY=%s\n' "$VAULTPI_SECRET_KEY" >> "$ENV_FILE"
  fi
fi

if grep -Eq '^BRIDGE_PSK=replace-with-a-long-random-shared-secret$|^BRIDGE_PSK=$' "$ENV_FILE"; then
  BRIDGE_PSK="$(gen_secret)"
  if grep -q '^BRIDGE_PSK=' "$ENV_FILE"; then
    sed -i "s|^BRIDGE_PSK=.*|BRIDGE_PSK=${BRIDGE_PSK}|" "$ENV_FILE"
  else
    printf '\nBRIDGE_PSK=%s\n' "$BRIDGE_PSK" >> "$ENV_FILE"
  fi
fi

sed \
  -e "s|__VAULTPI_USER__|${APP_USER}|g" \
  -e "s|__VAULTPI_GROUP__|${APP_GROUP}|g" \
  deploy/vaultpi-control-center.service | $SUDO tee "$SERVICE_FILE" > /dev/null
$SUDO systemctl daemon-reload
$SUDO systemctl enable vaultpi-control-center.service

# Allow the service user to manage Wi-Fi profiles via nmcli without a password prompt
SUDOERS_NMCLI="/etc/sudoers.d/vaultpi-nmcli"
if [ ! -f "$SUDOERS_NMCLI" ] && command -v nmcli >/dev/null 2>&1; then
  echo "${APP_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli connection add *, /usr/bin/nmcli connection delete *" \
    | $SUDO tee "$SUDOERS_NMCLI" > /dev/null
  $SUDO chmod 0440 "$SUDOERS_NMCLI"
  echo "Added sudoers rule for nmcli Wi-Fi management"
fi

ADMIN_USERNAME="${ADMIN_USERNAME:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [ -t 0 ]; then
  if [ -z "$ADMIN_USERNAME" ]; then
    read -r -p "Admin username [admin]: " ADMIN_USERNAME
  fi
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"

  if [ -z "$ADMIN_PASSWORD" ]; then
    read -r -s -p "Admin password [admin]: " ADMIN_PASSWORD
    echo
  fi
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
else
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
fi

ADMIN_USERNAME="$ADMIN_USERNAME" ADMIN_PASSWORD="$ADMIN_PASSWORD" python scripts/create_admin.py --username "$ADMIN_USERNAME" --password "$ADMIN_PASSWORD" --allow-weak-password

$SUDO chown -R "${APP_USER}:${APP_GROUP}" "$APP_DIR"

$SUDO systemctl restart vaultpi-control-center

echo
echo "VaultPi Control Center is installed and running."
echo "If you want to change the optional URLs or sync targets later, edit ${ENV_FILE} and restart the service."
echo "You can check status with: sudo systemctl status vaultpi-control-center --no-pager"
