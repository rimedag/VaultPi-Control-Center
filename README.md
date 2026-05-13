# VaultPi Control Center

<img width="1706" height="1257" alt="Screenshot 2026-05-13 200751" src="https://github.com/user-attachments/assets/08d40186-3317-47cb-af58-24ea49bd3e16" />


Lightweight personal infrastructure dashboard for Raspberry Pi Zero 2 W.

## Stack
- Python 3 + Flask + Jinja2
- SQLite (WAL mode)
- Minimal CSS and server-rendered pages
- Lightweight background health checker thread

## Features
- Overview dashboard with system metrics
- Projects/apps registry with CRUD
- Local services view with safe configured actions
- Remote monitoring with retained check history and uptime approximation
- Quick actions using command allowlist
- Gitea operations cards for manual backup/sync, status JSON, logs, and script editing
- Log viewer for configured service log paths
- Settings page (URLs, intervals, module flags, execution flags)
- Session auth (single-user friendly)
- Activity/event log
- File-based config sync for projects/commands/settings (`config/control_center.json`)

## Pages
- `/`
- `/projects`
- `/projects/{id}`
- `/services/local`
- `/services/remote`
- `/monitoring`
- `/actions`
- `/nethunter`
- `/nethunter/{slug}`
- `/logs`
- `/settings`
- `/login`

## Local development
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python scripts/create_admin.py
python scripts/sync_config.py
python run.py
```
Open: `http://<pi-ip>:8000/login`

## Raspberry Pi OS Lite install
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip rsync
cd /path/to/Vaultpi\ Control\ Center
chmod +x install.sh update.sh uninstall.sh
./install.sh
```

## Update deployment
```bash
cd /path/to/Vaultpi\ Control\ Center
./update.sh
```

Deployment notes:
- `update.sh` preserves `venv/` and `instance/`
- If `venv/` is missing, `update.sh` recreates it automatically
- For manual rsync deploys, also exclude `venv/` and `instance/`

## Uninstall
```bash
cd /path/to/Vaultpi\ Control\ Center
./uninstall.sh
```

## Systemd manual commands
```bash
sudo cp deploy/vaultpi-control-center.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vaultpi-control-center
sudo systemctl status vaultpi-control-center --no-pager
sudo systemctl stop vaultpi-control-center
sudo systemctl start vaultpi-control-center
sudo systemctl restart vaultpi-control-center
```

## Browser access
- Same device: `http://127.0.0.1:8000/login`
- LAN: `http://<raspberry-pi-ip>:8000/login`

## Notes on safety and performance
- No arbitrary shell command input in UI.
- Actions run only from trusted DB-configured commands.
- Command output and exit code are logged to `activity_log`.
- Health checks run in one thread with bounded interval and retention.
- Retention defaults to 500 check rows to limit DB growth.
- Idle footprint is intentionally small: one Gunicorn worker + 2 threads, small templates, no frontend build step.

## Gitea operations integration
Quick Actions now includes two allowlisted operational jobs:
- `gitea-backup` -> `/usr/local/bin/gitea-backup.sh`
- `gitea-sync-android` -> `/usr/local/bin/gitea-sync-android.sh`
- `gitea-backup-verify` -> internal backup integrity check job
- `gitea-healthcheck` -> internal lightweight Gitea/repo health check job

Runtime files:
- Lock files: `/tmp/gitea-backup.lock`, `/tmp/gitea-sync-android.lock`
- Run logs: `/var/log/vaultpi/gitea-backup-run.log`, `/var/log/vaultpi/gitea-sync-android-run.log`, `/var/log/vaultpi/gitea-backup-verify.log`, `/var/log/vaultpi/gitea-healthcheck.log`
- Status JSON: `/var/lib/vaultpi/status/gitea-backup.json`, `/var/lib/vaultpi/status/gitea-sync-android.json`, `/var/lib/vaultpi/status/gitea-backup-verify.json`, `/var/lib/vaultpi/status/gitea-healthcheck.json`
- Script backups (before save): `/usr/local/bin/script-backups/`

Behavior:
- UI starts jobs asynchronously and returns immediately.
- Duplicate runs are blocked per job via lock files.
- Stale locks are auto-cleaned when PID is no longer alive.
- Android sync run button is disabled until `/usr/local/bin/gitea-sync-android.sh` exists.
- Android sync host, SSH port, SSH user, Android Gitea URL, backup path, and mirror path can be changed from Settings without editing the script.
- Android sync prunes phone backup zips down to the newest single archive after a successful copy.
- Repository health check uses the configured Gitea `app.ini` path from Settings, defaulting to `/etc/gitea/app.ini`.
- Script editor is allowlist-only (cannot edit arbitrary paths).
- Script edits always create a timestamped backup before overwrite.
- Restore Playbook is available at `/restore-playbook`.
- Operational guardrails are shown in Quick Actions and can be tuned in Settings (`ops_*` keys).

Permissions and sudo:
- App service user must be able to write:
  - `/var/log/vaultpi/`
  - `/var/lib/vaultpi/status/`
  - `/tmp/` (default available)
- If app user is not `git`, job runner attempts `sudo -n -u git -- <script>`.
- Example sudoers entry (adjust service user as needed):
  - `vaultpi ALL=(git) NOPASSWD: /usr/local/bin/gitea-backup.sh, /usr/local/bin/gitea-sync-android.sh`
- If script editing is enabled from UI, app user also needs write access to `/usr/local/bin/` and `/usr/local/bin/script-backups/`.

## Easy service configuration (single file)
Edit:
- `config/control_center.json`

Then sync into SQLite:
```bash
source venv/bin/activate
python scripts/sync_config.py
```

Or from UI:
- Open `/actions`
- Click `Sync Projects/Commands/Settings from Config`

What to manage in config:
- Add/edit entries under `projects` for `healthcheck_url`, `local_url`/`remote_url`, `monitoring_enabled`, `action_enabled`, `log_path`, run/stop/restart commands
- Add/edit allowlisted quick actions under `commands`
- Set core URLs and behavior in `settings`

## Seeded defaults
- Core settings are seeded automatically
- Projects and commands should be loaded from `config/control_center.json`

## NetHunter Wiki
- Route: `/nethunter`
- Direct article route: `/nethunter/{slug}`
- Data source: `app/knowledge_center.py`
- UI: server-rendered Jinja template with lightweight client-side filtering, favorites, recent history, and command copy buttons

How to add a new article:
1. Add a new entry to `ARTICLES` in `app/knowledge_center.py`
2. Use the existing `_platform_article(...)` or `_cli_article(...)` helper if the article fits those shapes
3. For custom content, build a full article dictionary and wrap it with `_prepare_article(...)`
4. Keep `slug` unique and use ASCII-friendly text
5. Add related article slugs only for articles that actually exist

Supported article fields:
- Core metadata: `id`, `slug`, `title`, `category`, `tags`, `shortDescription`, `difficulty`, `prerequisites`, `warnings`, `limitations`, `relatedTools`, `lastUpdated`, `badges`
- Context blocks: `nethunter`, `kaliLinux`
- Comparison block: `differences`
  Includes `whyUseOnNetHunterInsteadOfDesktopKali`, `whyUseDesktopKaliInsteadOfNetHunter`, `whenMobileIsEnough`, and `whenDesktopIsBetter`
- Flexible sections: `generalSections` with `text`, `callout`, `warning`, `checklist`, and `controls`

## Troubleshooting
- Service logs: `sudo journalctl -u vaultpi-control-center -f`
- Recreate admin user: `source venv/bin/activate && python scripts/create_admin.py`
- Check DB file: `/opt/vaultpi-control-center/instance/vaultpi.db`
- If startup fails, verify `VAULTPI_SECRET_KEY` in `/etc/default/vaultpi-control-center`
