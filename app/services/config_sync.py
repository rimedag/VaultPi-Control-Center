from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import get_db, set_setting


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Any, default: bool = False) -> int:
    if value is None:
        return 1 if default else 0
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def load_config(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return json.loads(p.read_text(encoding="utf-8-sig"))


def sync_from_config(path: str) -> dict[str, int]:
    payload = load_config(path)
    db = get_db()
    now = _utc_now()

    settings = payload.get("settings", {})
    projects = payload.get("projects", [])
    commands = payload.get("commands", [])

    for key, value in settings.items():
        set_setting(str(key), str(value))

    projects_created = 0
    projects_updated = 0
    for item in projects:
        slug = str(item.get("slug", "")).strip()
        name = str(item.get("name", "")).strip()
        if not slug or not name:
            continue

        existing = db.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        fields = {
            "name": name,
            "slug": slug,
            "description": str(item.get("description", "")),
            "category": str(item.get("category", "")),
            "type": str(item.get("type", "")),
            "environment": str(item.get("environment", "")),
            "stack": str(item.get("stack", "")),
            "repo_url": str(item.get("repo_url", "")),
            "local_url": str(item.get("local_url", "")),
            "remote_url": str(item.get("remote_url", "")),
            "healthcheck_url": str(item.get("healthcheck_url", "")),
            "host_machine": str(item.get("host_machine", "")),
            "port": int(item["port"]) if str(item.get("port", "")).isdigit() else None,
            "run_command": str(item.get("run_command", "")),
            "stop_command": str(item.get("stop_command", "")),
            "restart_command": str(item.get("restart_command", "")),
            "log_path": str(item.get("log_path", "")),
            "working_directory": str(item.get("working_directory", "")),
            "notes": str(item.get("notes", "")),
            "tags": str(item.get("tags", "")),
            "enabled": _as_bool(item.get("enabled", 1), default=True),
            "display_order": int(item.get("display_order", 100)),
            "monitoring_enabled": _as_bool(item.get("monitoring_enabled", 1), default=True),
            "action_enabled": _as_bool(item.get("action_enabled", 0), default=False),
            "archived": _as_bool(item.get("archived", 0), default=False),
        }

        if existing:
            db.execute(
                """
                UPDATE projects SET
                    name=:name, description=:description, category=:category, type=:type,
                    environment=:environment, stack=:stack, repo_url=:repo_url,
                    local_url=:local_url, remote_url=:remote_url, healthcheck_url=:healthcheck_url,
                    host_machine=:host_machine, port=:port, run_command=:run_command,
                    stop_command=:stop_command, restart_command=:restart_command, log_path=:log_path,
                    working_directory=:working_directory, notes=:notes, tags=:tags,
                    enabled=:enabled, display_order=:display_order, monitoring_enabled=:monitoring_enabled,
                    action_enabled=:action_enabled, archived=:archived, updated_at=:updated_at
                WHERE slug=:slug
                """,
                {**fields, "updated_at": now},
            )
            projects_updated += 1
        else:
            db.execute(
                """
                INSERT INTO projects (
                    name, slug, description, category, type, environment, stack,
                    repo_url, local_url, remote_url, healthcheck_url, host_machine,
                    port, run_command, stop_command, restart_command, log_path,
                    working_directory, notes, tags, enabled, display_order,
                    monitoring_enabled, action_enabled, archived, created_at, updated_at
                ) VALUES (
                    :name, :slug, :description, :category, :type, :environment, :stack,
                    :repo_url, :local_url, :remote_url, :healthcheck_url, :host_machine,
                    :port, :run_command, :stop_command, :restart_command, :log_path,
                    :working_directory, :notes, :tags, :enabled, :display_order,
                    :monitoring_enabled, :action_enabled, :archived, :created_at, :updated_at
                )
                """,
                {**fields, "created_at": now, "updated_at": now},
            )
            projects_created += 1

    commands_created = 0
    commands_updated = 0
    for item in commands:
        name = str(item.get("name", "")).strip()
        command = str(item.get("command", "")).strip()
        if not name or not command:
            continue

        existing = db.execute("SELECT id FROM commands WHERE name = ?", (name,)).fetchone()
        fields = {
            "name": name,
            "description": str(item.get("description", "")),
            "command": command,
            "working_directory": str(item.get("working_directory", "")),
            "timeout_sec": int(item.get("timeout_sec", 30)),
            "enabled": _as_bool(item.get("enabled", 1), default=True),
        }

        if existing:
            db.execute(
                """
                UPDATE commands SET
                    description=:description,
                    command=:command,
                    working_directory=:working_directory,
                    timeout_sec=:timeout_sec,
                    enabled=:enabled
                WHERE name=:name
                """,
                fields,
            )
            commands_updated += 1
        else:
            db.execute(
                """
                INSERT INTO commands(name, description, command, working_directory, timeout_sec, enabled, created_at)
                VALUES(:name, :description, :command, :working_directory, :timeout_sec, :enabled, :created_at)
                """,
                {**fields, "created_at": now},
            )
            commands_created += 1

    db.commit()
    return {
        "settings_synced": len(settings),
        "projects_created": projects_created,
        "projects_updated": projects_updated,
        "commands_created": commands_created,
        "commands_updated": commands_updated,
    }

