from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app, g
from werkzeug.security import generate_password_hash


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DEFAULT_SETTINGS: dict[str, str] = {
    "app_name": _env("APP_NAME", "VaultPi Control Center"),
    "dashboard_title": _env("DASHBOARD_TITLE", "VaultPi Control Center"),
    "refresh_interval": _env("REFRESH_INTERVAL", "60"),
    "filebrowser_url": _env("FILEBROWSER_URL", "http://localhost:8080"),
    "gitea_url": _env("GITEA_URL", "http://localhost:3000"),
    "gitea_config_path": _env("GITEA_CONFIG_PATH", "/etc/gitea/app.ini"),
    "n8n_url": _env("N8N_URL", "http://localhost:5678"),
    "android_host": "phone.lan",
    "android_termux_ssh_port": "8022",
    "android_ssh_user": "git",
    "android_gitea_url": "http://phone.lan:3000",
    "android_backup_path": "/data/data/com.termux/files/home/gitea-backups",
    "android_mirror_path": "/data/data/com.termux/files/home/gitea-mirrors",
    "hostname_label": _env("HOSTNAME_LABEL", ""),
    "theme": _env("THEME", "auto"),
    "module_projects": _env("MODULE_PROJECTS", "1"),
    "module_local_services": _env("MODULE_LOCAL_SERVICES", "1"),
    "module_remote_services": _env("MODULE_REMOTE_SERVICES", "1"),
    "module_actions": _env("MODULE_ACTIONS", "1"),
    "module_logs": _env("MODULE_LOGS", "1"),
    "auth_enabled": _env("AUTH_ENABLED", "1"),
    "command_execution_enabled": _env("COMMAND_EXECUTION_ENABLED", "1"),
    "bind_host": _env("CC_HOST", "0.0.0.0"),
    "bind_port": _env("CC_PORT", "8000"),
    "monitor_interval": _env("MONITOR_INTERVAL", "60"),
    "history_retention": _env("HISTORY_RETENTION", "500"),
    "config_file_path": _env("CONTROL_CENTER_CONFIG_PATH", "config/control_center.json"),
    "ops_min_disk_free_percent": _env("OPS_MIN_DISK_FREE_PERCENT", "15"),
    "ops_min_disk_free_gb": _env("OPS_MIN_DISK_FREE_GB", "10"),
    "ops_backup_max_age_hours": _env("OPS_BACKUP_MAX_AGE_HOURS", "36"),
    "ops_android_sync_max_age_days": "14",
    "ops_backup_dir_warn_gb": _env("OPS_BACKUP_DIR_WARN_GB", "30"),
    "ops_healthcheck_max_age_days": _env("OPS_HEALTHCHECK_MAX_AGE_DAYS", "7"),
    "brand_link_text": _env("BRAND_LINK_TEXT", "Built with VaultPi"),
    "brand_link_url": _env("BRAND_LINK_URL", ""),
    "help_mode": _env("HELP_MODE", "0"),
    "cardputer_host": _env("CARDPUTER_HOST", ""),
    "cardputer_api_port": _env("CARDPUTER_PORT", "8001"),
    "cardputer_password": _env("CARDPUTER_PASSWORD", "password"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_: Any = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    schema_path = Path(current_app.root_path) / "schema.sql"
    db.executescript(schema_path.read_text(encoding="utf-8"))
    db.commit()
    seed_defaults(db)


def seed_defaults(db: sqlite3.Connection) -> None:
    now = _utc_now()
    for key, value in DEFAULT_SETTINGS.items():
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )

    db.commit()


def init_app(app: Any) -> None:
    db_dir = Path(app.instance_path)
    db_dir.mkdir(parents=True, exist_ok=True)
    app.config.setdefault("DATABASE", str(db_dir / "vaultpi.db"))

    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command() -> None:
        init_db()
        print("Initialized the database.")


def ensure_admin_user(username: str, password: str) -> None:
    db = get_db()
    now = _utc_now()
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    password_hash = generate_password_hash(password)
    if row:
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, row["id"]),
        )
    else:
        db.execute(
            "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, now, now),
        )
    db.commit()


def setting(key: str, default: str = "") -> str:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db = get_db()
    now = _utc_now()
    db.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )
    db.commit()


def app_data_path(*parts: str) -> str:
    base = Path(current_app.instance_path)
    base.mkdir(parents=True, exist_ok=True)
    return str(base.joinpath(*parts))


def env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}
