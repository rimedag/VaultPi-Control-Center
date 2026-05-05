from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from .auth import login_required
from .db import get_db, set_setting, setting
from .knowledge_center import get_article_by_slug, get_articles, get_filter_options
from .services.activity import is_command_execution_enabled, log_event
from .services.cardputer_link import cardputer_payload
from .services.checks import check_http, check_tcp
from .services.commands import run_command
from .services.config_sync import sync_from_config
from .services.gitea_ops import (
    all_job_statuses,
    get_job,
    latest_local_backup_info,
    list_script_backups,
    read_full_log,
    read_script,
    save_script,
    start_job,
)
from .services.logs import read_log_tail
from .services.terminal import terminal_manager
from .services.metrics import system_metrics
from .frontend import frontend_available, serve_frontend_index
from .services.monitor import HealthChecker
from .services.wifi import apply_network_add, apply_network_remove, scan_nearby, tailscale_status
from .services.web_browser import (
    build_search_url,
    ensure_bookmarks,
    normalize_url,
    w3m_command,
    w3m_installed,
)

main_bp = Blueprint("main", __name__)


PROJECT_FIELDS = [
    "name",
    "slug",
    "description",
    "category",
    "type",
    "environment",
    "stack",
    "repo_url",
    "local_url",
    "remote_url",
    "healthcheck_url",
    "host_machine",
    "port",
    "run_command",
    "stop_command",
    "restart_command",
    "log_path",
    "working_directory",
    "notes",
    "tags",
    "display_order",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "service"


def _next_unique_slug(base_slug: str) -> str:
    db = get_db()
    slug = base_slug
    idx = 2
    while db.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{idx}"
        idx += 1
    return slug


def _latest_status_map() -> dict[int, dict[str, Any]]:
    rows = get_db().execute(
        """
        SELECT sc.project_id, sc.status, sc.status_code, sc.response_ms, sc.checked_at
        FROM service_checks sc
        INNER JOIN (
            SELECT project_id, MAX(checked_at) AS max_checked
            FROM service_checks
            GROUP BY project_id
        ) latest ON latest.project_id = sc.project_id AND latest.max_checked = sc.checked_at
        """
    ).fetchall()
    return {
        r["project_id"]: {
            "status": r["status"],
            "status_code": r["status_code"],
            "response_ms": r["response_ms"],
            "checked_at": r["checked_at"],
        }
        for r in rows
    }


def _service_counts() -> dict[str, int]:
    db = get_db()
    status = _latest_status_map()
    local = db.execute(
        "SELECT id FROM projects WHERE enabled = 1 AND archived = 0 AND type IN ('local app', 'tool', 'service')"
    ).fetchall()
    remote = db.execute(
        "SELECT id FROM projects WHERE enabled = 1 AND archived = 0 AND type = 'remote app'"
    ).fetchall()

    local_up = sum(1 for row in local if status.get(row["id"], {}).get("status") == "up")
    remote_up = sum(1 for row in remote if status.get(row["id"], {}).get("status") == "up")
    return {
        "local_total": len(local),
        "local_up": local_up,
        "local_down": len(local) - local_up,
        "remote_total": len(remote),
        "remote_up": remote_up,
        "remote_down": len(remote) - remote_up,
    }


def _android_monitor_status() -> dict[str, Any]:
    android_host = setting("android_host", "phone.lan").strip()
    android_ssh_port_raw = setting("android_termux_ssh_port", "8022").strip() or "8022"
    android_gitea_url = setting("android_gitea_url", "").strip()
    android_ssh_port = int(android_ssh_port_raw) if android_ssh_port_raw.isdigit() else 8022
    termux_check = check_tcp(android_host, android_ssh_port, timeout=1.8) if android_host else None
    android_gitea_check = check_http(android_gitea_url, timeout=1.8) if android_gitea_url else None
    return {
        "host": android_host,
        "ssh_port": android_ssh_port,
        "gitea_url": android_gitea_url,
        "termux_status": termux_check.status if termux_check else "unknown",
        "termux_response_ms": termux_check.response_ms if termux_check else None,
        "android_gitea_status": android_gitea_check.status if android_gitea_check else "unknown",
        "android_gitea_response_ms": android_gitea_check.response_ms if android_gitea_check else None,
    }


def _parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _setting_float(key: str, default: float) -> float:
    raw = setting(key, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _setting_int(key: str, default: int) -> int:
    raw = setting(key, str(default)).strip()
    return int(raw) if raw.isdigit() else default


def _module_enabled(key: str, default: str = "1") -> bool:
    return setting(key, default).strip() == "1"


def _require_module(key: str, endpoint: str) -> Any | None:
    if _module_enabled(key):
        return None
    flash("That module is disabled in settings.", "error")
    return redirect(url_for(endpoint))


def _actor() -> str:
    return g.user["username"] if getattr(g, "user", None) else "system"


def _ops_guardrails(gitea_jobs: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    metrics = system_metrics()
    disk_total_gb = float(metrics.get("disk_total_gb", 0.0) or 0.0)
    disk_used_gb = float(metrics.get("disk_used_gb", 0.0) or 0.0)
    disk_percent = float(metrics.get("disk_percent", 0.0) or 0.0)
    disk_free_gb = max(0.0, disk_total_gb - disk_used_gb)
    disk_free_percent = max(0.0, 100.0 - disk_percent)

    min_disk_percent = _setting_float("ops_min_disk_free_percent", 15.0)
    min_disk_gb = _setting_float("ops_min_disk_free_gb", 10.0)
    max_backup_age_hours = _setting_int("ops_backup_max_age_hours", 36)
    max_android_age_days = _setting_int("ops_android_sync_max_age_days", 14)
    max_backup_dir_gb = _setting_float("ops_backup_dir_warn_gb", 30.0)
    max_health_age_days = _setting_int("ops_healthcheck_max_age_days", 7)

    if disk_free_percent < min_disk_percent or disk_free_gb < min_disk_gb:
        warnings.append(
            {
                "level": "error",
                "message": f"Low disk space: {disk_free_gb:.1f} GB free ({disk_free_percent:.1f}%).",
            }
        )

    backup_info = latest_local_backup_info()
    if not backup_info.get("exists"):
        warnings.append({"level": "error", "message": "No local backup found in /backup/gitea."})
    else:
        backup_dt = _parse_iso(str(backup_info.get("mtime", "")))
        if backup_dt:
            age = datetime.now(timezone.utc) - backup_dt
            if age > timedelta(hours=max_backup_age_hours):
                warnings.append(
                    {
                        "level": "warning",
                        "message": f"Latest local backup is stale ({int(age.total_seconds() // 3600)}h old).",
                    }
                )

    backup_dir = Path(os.getenv("LOCAL_BACKUP_DIR", "/backup/gitea"))
    if backup_dir.exists() and backup_dir.is_dir():
        size_bytes = sum(p.stat().st_size for p in backup_dir.glob("*.zip") if p.is_file())
        size_gb = size_bytes / (1024**3)
        if size_gb > max_backup_dir_gb:
            warnings.append(
                {
                    "level": "warning",
                    "message": f"Backup directory is large ({size_gb:.1f} GB in /backup/gitea).",
                }
            )

    android_sync = gitea_jobs.get("gitea-sync-android", {})
    android_ok_dt = _parse_iso(str(android_sync.get("last_finished_at", "")))
    if android_sync.get("last_status") == "success" and android_ok_dt:
        age = datetime.now(timezone.utc) - android_ok_dt
        if age > timedelta(days=max_android_age_days):
            warnings.append(
                {"level": "warning", "message": f"Android sync is stale ({age.days} days since last success)."}
            )
    else:
        warnings.append({"level": "warning", "message": "No successful Android sync recorded yet."})

    verify = gitea_jobs.get("gitea-backup-verify", {})
    if verify.get("last_status") != "success":
        warnings.append({"level": "warning", "message": "Latest backup has not been verified successfully yet."})

    health = gitea_jobs.get("gitea-healthcheck", {})
    health_dt = _parse_iso(str(health.get("last_finished_at", "")))
    if health.get("last_status") == "success" and health_dt:
        if datetime.now(timezone.utc) - health_dt > timedelta(days=max_health_age_days):
            warnings.append({"level": "warning", "message": "Repository health check is stale."})
    else:
        warnings.append({"level": "warning", "message": "No successful repository health check recorded yet."})

    return warnings


def _restore_playbook_context(gitea_jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    backup_info = latest_local_backup_info()
    repo_count = len(list(Path("/var/lib/gitea/data/gitea-repositories").rglob("*.git")))
    android_sync = gitea_jobs.get("gitea-sync-android", {})
    return {
        "local_backup_dir": "/backup/gitea",
        "android_backup_dir": setting("android_backup_path", "/data/data/com.termux/files/home/gitea-backups"),
        "android_mirror_dir": setting("android_mirror_path", "/data/data/com.termux/files/home/gitea-mirrors"),
        "local_repo_dir": "/var/lib/gitea/data/gitea-repositories",
        "latest_backup_file": backup_info.get("filename", ""),
        "latest_backup_time": backup_info.get("mtime", ""),
        "repo_count": repo_count,
        "android_sync_status": android_sync.get("last_status", "never"),
        "android_sync_finished_at": android_sync.get("last_finished_at", ""),
    }


def _project_from_form() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in PROJECT_FIELDS:
        payload[field] = request.form.get(field, "").strip()

    payload["enabled"] = 1 if request.form.get("enabled") == "on" else 0
    payload["monitoring_enabled"] = 1 if request.form.get("monitoring_enabled") == "on" else 0
    payload["action_enabled"] = 1 if request.form.get("action_enabled") == "on" else 0
    payload["port"] = int(payload["port"]) if str(payload["port"]).isdigit() else None
    payload["display_order"] = int(payload["display_order"]) if str(payload["display_order"]).isdigit() else 100
    return payload


@main_bp.app_context_processor
def inject_globals() -> dict[str, Any]:
    safe_shutdown = get_db().execute(
        "SELECT id FROM commands WHERE enabled = 1 AND name = ? LIMIT 1",
        ("Safe Shutdown Pi",),
    ).fetchone()
    safe_shutdown_command_id = safe_shutdown["id"] if safe_shutdown else None
    return {
        "app_name": setting("app_name", "VaultPi Control Center"),
        "dashboard_title": setting("dashboard_title", "VaultPi Control Center"),
        "refresh_interval": int(setting("refresh_interval", "60") or "60"),
        "theme": setting("theme", "auto"),
        "hostname_label": setting("hostname_label", ""),
        "module_flags": {
            "projects": _module_enabled("module_projects"),
            "local_services": _module_enabled("module_local_services"),
            "remote_services": _module_enabled("module_remote_services"),
            "actions": _module_enabled("module_actions"),
            "logs": _module_enabled("module_logs"),
        },
        "app_service_commands": {
            "start": "sudo systemctl start vaultpi-control-center",
            "stop": "sudo systemctl stop vaultpi-control-center",
            "restart": "sudo systemctl restart vaultpi-control-center",
            "status": "sudo systemctl status vaultpi-control-center --no-pager -l",
            "logs": "sudo journalctl -u vaultpi-control-center -f",
        },
        "safe_shutdown_command_id": safe_shutdown_command_id,
        "brand_link_text": setting("brand_link_text", "Built with VaultPi"),
        "brand_link_url": setting("brand_link_url", ""),
    }


@main_bp.route("/")
@login_required
def dashboard() -> str:
    if frontend_available():
        return serve_frontend_index()
    db = get_db()
    metrics = system_metrics()
    counts = _service_counts()

    quick_links = {
        "Gitea": setting("gitea_url", os.getenv("GITEA_URL", "")),
        "n8n": setting("n8n_url", os.getenv("N8N_URL", "")),
        "Filebrowser": setting("filebrowser_url", os.getenv("FILEBROWSER_URL", "")),
    }
    quick_status: dict[str, str] = {}
    for name, link in quick_links.items():
        if link:
            quick_status[name] = check_http(link, timeout=1.5).status
        else:
            quick_status[name] = "unknown"

    android_monitor = _android_monitor_status()

    mount_paths = []
    for base in ("/mnt", "/media"):
        if os.path.isdir(base):
            for item in sorted(os.listdir(base)):
                path = os.path.join(base, item)
                if os.path.isdir(path):
                    mount_paths.append(path)

    recent_activity = db.execute(
        """
        SELECT a.*, p.name AS project_name
        FROM activity_log a
        LEFT JOIN projects p ON p.id = a.project_id
        ORDER BY a.created_at DESC
        LIMIT 12
        """
    ).fetchall()

    return render_template(
        "dashboard.html",
        metrics=metrics,
        counts=counts,
        quick_links=quick_links,
        quick_status=quick_status,
        android_monitor=android_monitor,
        mount_paths=mount_paths,
        recent_activity=recent_activity,
        tailscale=tailscale_status(),
    )


@main_bp.route("/projects")
@login_required
def projects_list() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_projects", "main.dashboard"):
        return redirect_response
    db = get_db()
    projects = db.execute(
        "SELECT * FROM projects WHERE archived = 0 ORDER BY display_order ASC, name ASC"
    ).fetchall()
    status_map = _latest_status_map()
    return render_template("projects_list.html", projects=projects, status_map=status_map)


@main_bp.route("/projects/new", methods=["GET", "POST"])
@login_required
def project_create() -> Any:
    if redirect_response := _require_module("module_projects", "main.dashboard"):
        return redirect_response
    if request.method == "POST":
        data = _project_from_form()
        now = _utc_now()
        try:
            get_db().execute(
                """
                INSERT INTO projects (
                    name, slug, description, category, type, environment, stack,
                    repo_url, local_url, remote_url, healthcheck_url, host_machine,
                    port, run_command, stop_command, restart_command, log_path,
                    working_directory, notes, tags, enabled, display_order,
                    monitoring_enabled, action_enabled, created_at, updated_at
                ) VALUES (
                    :name, :slug, :description, :category, :type, :environment, :stack,
                    :repo_url, :local_url, :remote_url, :healthcheck_url, :host_machine,
                    :port, :run_command, :stop_command, :restart_command, :log_path,
                    :working_directory, :notes, :tags, :enabled, :display_order,
                    :monitoring_enabled, :action_enabled, :created_at, :updated_at
                )
                """,
                {**data, "created_at": now, "updated_at": now},
            )
            get_db().commit()
            flash("Project created", "success")
            log_event("project_create", None, _actor(), f"Created project {data['name']}")
            return redirect(url_for("main.projects_list"))
        except Exception as exc:
            flash(f"Unable to create project: {exc}", "error")

    preset_name = request.args.get("name", "").strip()
    preset_slug = request.args.get("slug", "").strip()
    preset_type = request.args.get("type", "").strip()
    preset_env = request.args.get("environment", "").strip()
    preset_local_url = request.args.get("local_url", "").strip()
    preset_remote_url = request.args.get("remote_url", "").strip()
    preset_health = request.args.get("healthcheck_url", "").strip()
    preset_monitor = request.args.get("monitoring_enabled", "1").strip()
    preset_action = request.args.get("action_enabled", "0").strip()

    draft = {
        "name": preset_name,
        "slug": preset_slug or (_slugify(preset_name) if preset_name else ""),
        "description": "",
        "category": "",
        "type": preset_type or "remote app",
        "environment": preset_env or "prod",
        "stack": "",
        "repo_url": "",
        "local_url": preset_local_url,
        "remote_url": preset_remote_url,
        "healthcheck_url": preset_health,
        "host_machine": "",
        "port": "",
        "run_command": "",
        "stop_command": "",
        "restart_command": "",
        "log_path": "",
        "working_directory": "",
        "notes": "",
        "tags": "",
        "display_order": 100,
        "enabled": 1,
        "monitoring_enabled": 1 if preset_monitor == "1" else 0,
        "action_enabled": 1 if preset_action == "1" else 0,
    }
    return render_template("project_form.html", project=draft)


@main_bp.route("/projects/<int:project_id>")
@login_required
def project_detail(project_id: int) -> str:
    if redirect_response := _require_module("module_projects", "main.dashboard"):
        return redirect_response
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found", "error")
        return redirect(url_for("main.projects_list"))
    history = db.execute(
        "SELECT * FROM service_checks WHERE project_id = ? ORDER BY checked_at DESC LIMIT 30",
        (project_id,),
    ).fetchall()
    status = _latest_status_map().get(project_id)
    return render_template("project_detail.html", project=project, history=history, status=status)


@main_bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id: int) -> Any:
    if redirect_response := _require_module("module_projects", "main.dashboard"):
        return redirect_response
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found", "error")
        return redirect(url_for("main.projects_list"))

    if request.method == "POST":
        data = _project_from_form()
        data["updated_at"] = _utc_now()
        data["id"] = project_id
        try:
            db.execute(
                """
                UPDATE projects SET
                    name=:name, slug=:slug, description=:description, category=:category,
                    type=:type, environment=:environment, stack=:stack, repo_url=:repo_url,
                    local_url=:local_url, remote_url=:remote_url, healthcheck_url=:healthcheck_url,
                    host_machine=:host_machine, port=:port, run_command=:run_command,
                    stop_command=:stop_command, restart_command=:restart_command,
                    log_path=:log_path, working_directory=:working_directory, notes=:notes,
                    tags=:tags, enabled=:enabled, display_order=:display_order,
                    monitoring_enabled=:monitoring_enabled, action_enabled=:action_enabled,
                    updated_at=:updated_at
                WHERE id=:id
                """,
                data,
            )
            db.commit()
            flash("Project updated", "success")
            log_event("project_edit", project_id, _actor(), f"Updated project {data['name']}")
            return redirect(url_for("main.project_detail", project_id=project_id))
        except Exception as exc:
            flash(f"Unable to update project: {exc}", "error")

    return render_template("project_form.html", project=project)


@main_bp.route("/projects/<int:project_id>/archive", methods=["POST"])
@login_required
def project_archive(project_id: int) -> Any:
    if redirect_response := _require_module("module_projects", "main.dashboard"):
        return redirect_response
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project:
        db.execute("UPDATE projects SET archived = 1, updated_at = ? WHERE id = ?", (_utc_now(), project_id))
        db.commit()
        flash("Project archived", "success")
        log_event("project_archive", project_id, _actor(), f"Archived project {project['name']}")
    return redirect(url_for("main.projects_list"))


@main_bp.route("/services/local")
@login_required
def local_services() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_local_services", "main.dashboard"):
        return redirect_response
    db = get_db()
    services = db.execute(
        """
        SELECT * FROM projects
        WHERE enabled = 1 AND archived = 0 AND type IN ('local app', 'tool', 'service')
        ORDER BY display_order ASC, name ASC
        """
    ).fetchall()
    status_map = _latest_status_map()
    log_snippets: dict[int, str] = {}
    for service in services:
        if service["log_path"]:
            snippet = read_log_tail(service["log_path"], lines=8).strip()
            log_snippets[service["id"]] = snippet[-500:]
    return render_template(
        "local_services.html",
        services=services,
        status_map=status_map,
        log_snippets=log_snippets,
    )


@main_bp.route("/services/local/quick-add", methods=["POST"])
@login_required
def local_service_quick_add() -> Any:
    if redirect_response := _require_module("module_local_services", "main.dashboard"):
        return redirect_response
    name = request.form.get("name", "").strip()
    local_url = request.form.get("local_url", "").strip()
    healthcheck_url = request.form.get("healthcheck_url", "").strip() or local_url
    run_command = request.form.get("run_command", "").strip()
    stop_command = request.form.get("stop_command", "").strip()
    restart_command = request.form.get("restart_command", "").strip()
    if not name:
        flash("Local service name is required", "error")
        return redirect(url_for("main.local_services"))

    now = _utc_now()
    slug = _next_unique_slug(_slugify(name))
    try:
        get_db().execute(
            """
            INSERT INTO projects (
                name, slug, description, category, type, environment, stack,
                repo_url, local_url, remote_url, healthcheck_url, host_machine,
                port, run_command, stop_command, restart_command, log_path,
                working_directory, notes, tags, enabled, display_order,
                monitoring_enabled, action_enabled, archived, created_at, updated_at
            ) VALUES (?, ?, '', 'local', 'local app', 'local', '', '', ?, '', ?, 'vaultpi', ?, ?, ?, ?, '', '', '', '', 1, 100, 1, 1, 0, ?, ?)
            """,
            (
                name,
                slug,
                local_url,
                healthcheck_url,
                int(local_url.rsplit(":", 1)[-1]) if ":" in local_url and local_url.rsplit(":", 1)[-1].isdigit() else None,
                run_command,
                stop_command,
                restart_command,
                now,
                now,
            ),
        )
        get_db().commit()
        log_event("project_create", None, _actor(), f"Quick-added local service {name}")
        flash(f"Added local service: {name}", "success")
    except Exception as exc:
        flash(f"Quick add failed: {exc}", "error")
    return redirect(url_for("main.local_services"))


@main_bp.route("/services/local/<int:project_id>/<action>", methods=["POST"])
@login_required
def local_service_action(project_id: int, action: str) -> Any:
    if redirect_response := _require_module("module_local_services", "main.dashboard"):
        return redirect_response
    if not is_command_execution_enabled():
        flash("Command execution is disabled in settings", "error")
        return redirect(url_for("main.local_services"))

    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Service not found", "error")
        return redirect(url_for("main.local_services"))

    if project["action_enabled"] != 1:
        flash("Actions are disabled for this service", "error")
        return redirect(url_for("main.local_services"))

    cmd_map = {
        "start": project["run_command"],
        "stop": project["stop_command"],
        "restart": project["restart_command"],
    }
    cmd = cmd_map.get(action)
    if not cmd:
        flash("Action command is not configured", "error")
        return redirect(url_for("main.local_services"))

    result = run_command(cmd, timeout_sec=45, working_directory=project["working_directory"] or "")
    message = f"{action} {project['name']} exit={result.exit_code}"
    details = (result.stdout + "\n" + result.stderr).strip()[:2000]
    log_event("service_action", project_id, _actor(), message, details)

    if result.exit_code == 0:
        flash(f"Action completed: {action}", "success")
    else:
        error_hint = (result.stderr or result.stdout or "Unknown error").strip().splitlines()[0][:180]
        flash(f"Action failed: {action} - {error_hint}", "error")
    return redirect(url_for("main.local_services"))


@main_bp.route("/services/remote")
@login_required
def remote_services() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_remote_services", "main.dashboard"):
        return redirect_response
    db = get_db()
    services = db.execute(
        "SELECT * FROM projects WHERE enabled = 1 AND archived = 0 AND type = 'remote app' ORDER BY display_order ASC, name ASC"
    ).fetchall()
    status_map = _latest_status_map()
    success_map: dict[int, str] = {}
    failure_map: dict[int, str] = {}
    for service in services:
        success = db.execute(
            "SELECT checked_at FROM service_checks WHERE project_id = ? AND status = 'up' ORDER BY checked_at DESC LIMIT 1",
            (service["id"],),
        ).fetchone()
        failure = db.execute(
            "SELECT checked_at FROM service_checks WHERE project_id = ? AND status = 'down' ORDER BY checked_at DESC LIMIT 1",
            (service["id"],),
        ).fetchone()
        success_map[service["id"]] = success["checked_at"] if success else "-"
        failure_map[service["id"]] = failure["checked_at"] if failure else "-"
    return render_template(
        "remote_services.html",
        services=services,
        status_map=status_map,
        success_map=success_map,
        failure_map=failure_map,
    )


@main_bp.route("/services/remote/quick-add", methods=["POST"])
@login_required
def remote_service_quick_add() -> Any:
    if redirect_response := _require_module("module_remote_services", "main.dashboard"):
        return redirect_response
    name = request.form.get("name", "").strip()
    remote_url = request.form.get("remote_url", "").strip()
    healthcheck_url = request.form.get("healthcheck_url", "").strip() or remote_url
    environment = request.form.get("environment", "prod").strip() or "prod"
    if not name or not remote_url:
        flash("Remote service requires name and URL", "error")
        return redirect(url_for("main.remote_services"))

    slug = _next_unique_slug(_slugify(name))
    now = _utc_now()
    try:
        get_db().execute(
            """
            INSERT INTO projects (
                name, slug, description, category, type, environment, stack,
                repo_url, local_url, remote_url, healthcheck_url, host_machine,
                port, run_command, stop_command, restart_command, log_path,
                working_directory, notes, tags, enabled, display_order,
                monitoring_enabled, action_enabled, archived, created_at, updated_at
            ) VALUES (?, ?, '', 'website', 'remote app', ?, '', '', '', ?, ?, 'internet', ?, '', '', '', '', '', '', '', 1, 100, 1, 0, 0, ?, ?)
            """,
            (
                name,
                slug,
                environment,
                remote_url,
                healthcheck_url,
                443 if remote_url.startswith("https://") else 80,
                now,
                now,
            ),
        )
        get_db().commit()
        log_event("project_create", None, _actor(), f"Quick-added remote service {name}")
        flash(f"Added remote service: {name}", "success")
    except Exception as exc:
        flash(f"Quick add failed: {exc}", "error")
    return redirect(url_for("main.remote_services"))


@main_bp.route("/monitoring")
@login_required
def monitoring() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_remote_services", "main.dashboard"):
        return redirect_response
    db = get_db()
    services = db.execute(
        "SELECT * FROM projects WHERE enabled = 1 AND archived = 0 AND monitoring_enabled = 1 ORDER BY display_order ASC, name ASC"
    ).fetchall()

    rows = db.execute(
        """
        SELECT project_id, status, response_ms, checked_at
        FROM service_checks
        ORDER BY checked_at DESC
        LIMIT 400
        """
    ).fetchall()

    history_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        history_map.setdefault(row["project_id"], []).append(dict(row))

    uptime_map: dict[int, float] = {}
    for project_id, history in history_map.items():
        if not history:
            uptime_map[project_id] = 0.0
            continue
        up = sum(1 for h in history if h["status"] == "up")
        uptime_map[project_id] = round((up / len(history)) * 100.0, 1)

    status_map = _latest_status_map()
    android_monitor = _android_monitor_status()
    return render_template(
        "monitoring.html",
        services=services,
        status_map=status_map,
        history_map=history_map,
        uptime_map=uptime_map,
        android_monitor=android_monitor,
    )


@main_bp.route("/monitoring/quick-add", methods=["POST"])
@login_required
def monitoring_quick_add() -> Any:
    if redirect_response := _require_module("module_remote_services", "main.dashboard"):
        return redirect_response
    name = request.form.get("name", "").strip()
    healthcheck_url = request.form.get("healthcheck_url", "").strip()
    if not name or not healthcheck_url:
        flash("Monitoring entry requires name and healthcheck URL", "error")
        return redirect(url_for("main.monitoring"))

    slug = _next_unique_slug(_slugify(name))
    now = _utc_now()
    try:
        get_db().execute(
            """
            INSERT INTO projects (
                name, slug, description, category, type, environment, stack,
                repo_url, local_url, remote_url, healthcheck_url, host_machine,
                port, run_command, stop_command, restart_command, log_path,
                working_directory, notes, tags, enabled, display_order,
                monitoring_enabled, action_enabled, archived, created_at, updated_at
            ) VALUES (?, ?, 'Added from monitoring page', 'monitoring', 'remote app', 'prod', '', '', '', ?, ?, 'internet', ?, '', '', '', '', '', '', '', 1, 100, 1, 0, 0, ?, ?)
            """,
            (
                name,
                slug,
                healthcheck_url,
                healthcheck_url,
                443 if healthcheck_url.startswith("https://") else 80,
                now,
                now,
            ),
        )
        get_db().commit()
        log_event("project_create", None, _actor(), f"Quick-added monitoring target {name}")
        flash(f"Added monitoring target: {name}", "success")
    except Exception as exc:
        flash(f"Quick add failed: {exc}", "error")
    return redirect(url_for("main.monitoring"))


@main_bp.route("/monitoring/check-now", methods=["POST"])
@login_required
def monitoring_check_now() -> Any:
    if redirect_response := _require_module("module_remote_services", "main.dashboard"):
        return redirect_response
    checker = current_app.extensions.get("health_checker")
    if checker:
        checker.run_once()
        flash("Health checks completed", "success")
        log_event("monitor_now", None, _actor(), "Triggered immediate health checks")
    else:
        HealthChecker(current_app).run_once()
        flash("Health checks completed", "success")
        log_event("monitor_now", None, _actor(), "Triggered one-off health checks")
    return redirect(url_for("main.monitoring"))


@main_bp.route("/actions")
@login_required
def actions() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    db = get_db()
    commands = db.execute("SELECT * FROM commands WHERE enabled = 1 ORDER BY name ASC").fetchall()
    safe_shutdown_command = next((command for command in commands if command["name"] == "Safe Shutdown Pi"), None)
    local_services = db.execute(
        "SELECT * FROM projects WHERE enabled = 1 AND archived = 0 AND action_enabled = 1 ORDER BY display_order ASC, name ASC"
    ).fetchall()
    config_file_path = setting("config_file_path", os.getenv("CONTROL_CENTER_CONFIG_PATH", "config/control_center.json"))
    gitea_jobs = all_job_statuses()
    gitea_running = any(job.get("running") for job in gitea_jobs.values())
    ops_warnings = _ops_guardrails(gitea_jobs)
    return render_template(
        "actions.html",
        commands=commands,
        local_services=local_services,
        config_file_path=config_file_path,
        gitea_jobs=gitea_jobs,
        gitea_running=gitea_running,
        ops_warnings=ops_warnings,
        safe_shutdown_command=safe_shutdown_command,
        command_execution_enabled=is_command_execution_enabled(),
    )


@main_bp.route("/terminal", methods=["GET", "POST"])
@login_required
def terminal_console() -> str:
    if request.method == 'GET' and frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response

    form_data = {
        "command": "",
        "working_directory": "",
        "timeout_sec": "20",
    }
    result: dict[str, Any] | None = None
    suggested_commands = [
        "pwd",
        "systemctl status gitea --no-pager",
        "journalctl -u vaultpi-control-center -n 40 --no-pager",
        "git -C /opt/Obsidian status --short --branch",
        "df -h",
    ]

    if request.method == "POST":
        if not is_command_execution_enabled():
            flash("Command execution is disabled in settings", "error")
            return render_template("terminal.html", form_data=form_data, result=None, suggested_commands=suggested_commands)

        form_data["command"] = request.form.get("command", "").strip()
        form_data["working_directory"] = request.form.get("working_directory", "").strip()
        timeout_raw = request.form.get("timeout_sec", "20").strip()
        timeout = int(timeout_raw) if timeout_raw.isdigit() else 20
        timeout = max(1, min(timeout, 120))
        form_data["timeout_sec"] = str(timeout)

        if not form_data["command"]:
            flash("Command is required", "error")
            return render_template(
                "terminal.html",
                form_data=form_data,
                result=None,
                suggested_commands=suggested_commands,
            )

        command_result = run_command(
            form_data["command"],
            timeout_sec=timeout,
            working_directory=form_data["working_directory"],
        )
        result = {
            "exit_code": command_result.exit_code,
            "stdout": command_result.stdout,
            "stderr": command_result.stderr,
        }
        details = json.dumps(
            {
                "command": form_data["command"],
                "working_directory": form_data["working_directory"],
                "timeout_sec": timeout,
                "exit": command_result.exit_code,
                "stdout": command_result.stdout,
                "stderr": command_result.stderr,
            }
        )[:4000]
        log_event("terminal_command", None, _actor(), f"Executed console command: {form_data['command']}", details)

        if command_result.exit_code == 0:
            flash("Command finished successfully", "success")
        else:
            flash(f"Command exited with code {command_result.exit_code}", "error")

    return render_template(
        "terminal.html",
        form_data=form_data,
        result=result,
        suggested_commands=suggested_commands,
    )




def _terminal_session_key() -> str:
    key = session.get("terminal_session_key", "").strip()
    if not key:
        key = os.urandom(16).hex()
        session["terminal_session_key"] = key
    return key


@main_bp.route("/terminal/live")
@login_required
def terminal_live() -> str:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    return render_template("terminal_live.html")


@main_bp.route("/terminal/live/poll")
@login_required
def terminal_live_poll() -> Response:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return jsonify({"error": "module disabled"}), 403
    if not is_command_execution_enabled():
        return jsonify({"error": "command execution disabled"}), 403

    raw_cursor = request.args.get("cursor", "0").strip()
    cursor = int(raw_cursor) if raw_cursor.isdigit() else 0
    snapshot = terminal_manager.read(_terminal_session_key(), cursor=cursor)
    return jsonify(
        {
            "output": snapshot.output,
            "cursor": snapshot.cursor,
            "alive": snapshot.alive,
            "reset": snapshot.reset,
        }
    )


@main_bp.route("/terminal/live/input", methods=["POST"])
@login_required
def terminal_live_input() -> Response:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return jsonify({"error": "module disabled"}), 403
    if not is_command_execution_enabled():
        return jsonify({"error": "command execution disabled"}), 403

    payload = request.get_json(silent=True) or {}
    data = str(payload.get("data", ""))
    if len(data) > 2048:
        data = data[:2048]
    ok = terminal_manager.write(_terminal_session_key(), data)
    return jsonify({"ok": ok})


@main_bp.route("/terminal/live/reset", methods=["POST"])
@login_required
def terminal_live_reset() -> Response:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return jsonify({"error": "module disabled"}), 403
    if not is_command_execution_enabled():
        return jsonify({"error": "command execution disabled"}), 403

    terminal_manager.reset(_terminal_session_key())
    log_event("terminal_reset", None, _actor(), "Reset live terminal session")
    return jsonify({"ok": True})
@main_bp.route("/restore-playbook")
@login_required
def restore_playbook() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    jobs = all_job_statuses()
    context = _restore_playbook_context(jobs)
    return render_template("restore_playbook.html", **context)


@main_bp.route("/actions/gitea/<job_key>/run", methods=["POST"])
@login_required
def gitea_run(job_key: str) -> Any:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    if not is_command_execution_enabled():
        flash("Command execution is disabled in settings", "error")
        return redirect(url_for("main.actions"))

    job = get_job(job_key)
    if not job:
        flash("Unknown Gitea job", "error")
        return redirect(url_for("main.actions"))

    actor = _actor()
    state, message = start_job(job, actor=actor)
    log_event("gitea_operation_start", None, actor, f"{job.key} -> {state}", message)

    if state == "started":
        flash(message, "success")
    elif state == "already_running":
        flash(message, "error")
    elif state == "missing_script":
        flash(message, "error")
    else:
        flash(message, "error")
    return redirect(url_for("main.actions"))


@main_bp.route("/actions/gitea/<job_key>/status")
@login_required
def gitea_status(job_key: str) -> Response:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    jobs = all_job_statuses()
    job = jobs.get(job_key)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@main_bp.route("/actions/gitea/<job_key>/log")
@login_required
def gitea_log(job_key: str) -> str:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    job = get_job(job_key)
    if not job:
        flash("Unknown Gitea job", "error")
        return redirect(url_for("main.actions"))

    content, truncated = read_full_log(job)
    return render_template(
        "gitea_log.html",
        job=job,
        content=content,
        truncated=truncated,
    )


@main_bp.route("/actions/gitea/<job_key>/script", methods=["GET", "POST"])
@login_required
def gitea_script(job_key: str) -> Any:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    job = get_job(job_key)
    if not job:
        flash("Unknown Gitea job", "error")
        return redirect(url_for("main.actions"))
    if not job.editable_script or job.job_type != "script":
        flash("Script editing is only available for allowlisted operational scripts.", "error")
        return redirect(url_for("main.actions"))

    if request.method == "POST":
        script_content = request.form.get("script_content", "")
        warning_ack = request.form.get("warning_ack") == "on"
        if not warning_ack:
            flash("Please confirm the warning before saving script changes.", "error")
            return redirect(url_for("main.gitea_script", job_key=job.key))

        try:
            _, message = save_script(job, script_content)
            actor = _actor()
            log_event("gitea_script_edit", None, actor, f"Updated script for {job.key}", message)
            flash(message, "success")
        except Exception as exc:
            flash(f"Unable to save script: {exc}", "error")

        return redirect(url_for("main.gitea_script", job_key=job.key))

    exists, content, read_error = read_script(job)
    backups, backups_error = list_script_backups(job, limit=8)
    if read_error:
        flash(read_error, "error")
    if backups_error:
        flash(backups_error, "warning")
    return render_template(
        "gitea_script.html",
        job=job,
        script_exists=exists,
        script_content=content,
        script_backups=backups,
    )


@main_bp.route("/actions/sync-config", methods=["POST"])
@login_required
def sync_config_action() -> Any:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    raw_path = setting("config_file_path", os.getenv("CONTROL_CENTER_CONFIG_PATH", "config/control_center.json")).strip()
    if not raw_path:
        raw_path = "config/control_center.json"

    base_dir = os.path.abspath(os.path.join(current_app.root_path, ".."))
    config_path = raw_path if os.path.isabs(raw_path) else os.path.join(base_dir, raw_path)
    try:
        stats = sync_from_config(config_path)
        log_event(
            "config_sync",
            None,
            _actor(),
            "Synchronized config file",
            json.dumps({"path": config_path, **stats}),
        )
        flash(
            "Config sync complete: "
            f"projects +{stats['projects_created']}/~{stats['projects_updated']}, "
            f"commands +{stats['commands_created']}/~{stats['commands_updated']}",
            "success",
        )
    except Exception as exc:
        log_event("config_sync_error", None, _actor(), "Config sync failed", str(exc))
        flash(f"Config sync failed: {exc}", "error")
    return redirect(url_for("main.actions"))


@main_bp.route("/actions/command/<int:command_id>/run", methods=["POST"])
@login_required
def run_named_command(command_id: int) -> Any:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    if not is_command_execution_enabled():
        flash("Command execution is disabled in settings", "error")
        return redirect(url_for("main.actions"))

    db = get_db()
    command = db.execute("SELECT * FROM commands WHERE id = ? AND enabled = 1", (command_id,)).fetchone()
    if not command:
        flash("Command not found", "error")
        return redirect(url_for("main.actions"))

    timeout = int(command["timeout_sec"] or 30)
    result = run_command(command["command"], timeout_sec=timeout, working_directory=command["working_directory"] or "")
    details = json.dumps(
        {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit": result.exit_code,
            "command": command["command"],
        }
    )[:4000]
    log_event("quick_action", None, _actor(), f"Executed: {command['name']}", details)

    if result.exit_code == 0:
        flash(f"Command succeeded: {command['name']}", "success")
    else:
        error_hint = (result.stderr or result.stdout or "Unknown error").strip().splitlines()[0][:180]
        flash(f"Command failed: {command['name']} - {error_hint}", "error")
    return redirect(url_for("main.actions"))


@main_bp.route("/actions/clear-checks", methods=["POST"])
@login_required
def clear_checks() -> Any:
    if redirect_response := _require_module("module_actions", "main.dashboard"):
        return redirect_response
    db = get_db()
    db.execute("DELETE FROM service_checks")
    db.commit()
    log_event("clear_checks", None, _actor(), "Cleared health check history")
    flash("Health check history cleared", "success")
    return redirect(url_for("main.actions"))


@main_bp.route("/logs")
@login_required
def logs() -> str:
    if frontend_available():
        return serve_frontend_index()
    if redirect_response := _require_module("module_logs", "main.dashboard"):
        return redirect_response
    projects = get_db().execute(
        "SELECT id, name, log_path FROM projects WHERE archived = 0 AND log_path IS NOT NULL AND log_path != '' ORDER BY name ASC"
    ).fetchall()
    return render_template("logs.html", projects=projects, selected_project=None, log_content="")


@main_bp.route("/logs/<int:project_id>")
@login_required
def logs_project(project_id: int) -> str:
    if redirect_response := _require_module("module_logs", "main.dashboard"):
        return redirect_response
    db = get_db()
    projects = db.execute(
        "SELECT id, name, log_path FROM projects WHERE archived = 0 AND log_path IS NOT NULL AND log_path != '' ORDER BY name ASC"
    ).fetchall()
    selected = db.execute("SELECT id, name, log_path FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not selected:
        flash("Log source not found", "error")
        return redirect(url_for("main.logs"))
    lines = int(request.args.get("lines", "120"))
    lines = max(20, min(500, lines))
    log_content = read_log_tail(selected["log_path"] if selected else "", lines=lines)
    return render_template("logs.html", projects=projects, selected_project=selected, log_content=log_content)


@main_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page() -> str:
    if request.method == 'GET' and frontend_available():
        return serve_frontend_index()
    keys = [
        "app_name",
        "dashboard_title",
        "refresh_interval",
        "filebrowser_url",
        "gitea_url",
        "gitea_config_path",
        "n8n_url",
        "android_host",
        "android_termux_ssh_port",
        "android_ssh_user",
        "android_gitea_url",
        "android_backup_path",
        "android_mirror_path",
        "hostname_label",
        "theme",
        "module_projects",
        "module_local_services",
        "module_remote_services",
        "module_actions",
        "module_logs",
        "auth_enabled",
        "command_execution_enabled",
        "bind_host",
        "bind_port",
        "monitor_interval",
        "history_retention",
        "config_file_path",
        "ops_min_disk_free_percent",
        "ops_min_disk_free_gb",
        "ops_backup_max_age_hours",
        "ops_android_sync_max_age_days",
        "ops_backup_dir_warn_gb",
        "ops_healthcheck_max_age_days",
        "brand_link_text",
        "brand_link_url",
        "help_mode",
        "cardputer_host",
        "cardputer_api_port",
        "cardputer_password",
    ]

    if request.method == "POST":
        for key in keys:
            if key.startswith("module_") or key in {"auth_enabled", "command_execution_enabled", "help_mode"}:
                set_setting(key, "1" if request.form.get(key) == "on" else "0")
            else:
                set_setting(key, request.form.get(key, "").strip())

        log_event("settings_update", None, _actor(), "Updated settings")
        flash("Settings saved", "success")

    settings_map = {key: setting(key, "") for key in keys}
    hostname_default = socket.gethostname()
    if not settings_map.get("hostname_label"):
        settings_map["hostname_label"] = hostname_default

    return render_template("settings.html", settings=settings_map)


def _knowledge_sidebar_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "slug": article["slug"],
            "title": article["title"],
            "category": article["category"],
            "difficulty": article["difficulty"],
            "platform": article["platform"],
            "tags": article.get("tags", []),
            "shortDescription": article["shortDescription"],
            "badges": article.get("badges", []),
            "searchText": article["searchText"],
        }
        for article in articles
    ]


@main_bp.route("/nethunter")
@login_required
def nethunter_wiki() -> str:
    articles = get_articles()
    selected = articles[0] if articles else None
    return render_template(
        "nethunter.html",
        articles=articles,
        article_summaries=_knowledge_sidebar_articles(articles),
        selected_article=selected,
        filter_options=get_filter_options(),
        article_slug_set={article["slug"] for article in articles},
    )


@main_bp.route("/nethunter/<slug>")
@login_required
def nethunter_article(slug: str) -> str:
    articles = get_articles()
    selected = get_article_by_slug(slug)
    if not selected:
        flash("Article not found", "error")
        return redirect(url_for("main.nethunter_wiki"))
    return render_template(
        "nethunter.html",
        articles=articles,
        article_summaries=_knowledge_sidebar_articles(articles),
        selected_article=selected,
        filter_options=get_filter_options(),
        article_slug_set={article["slug"] for article in articles},
    )







@main_bp.route("/console")
@login_required
def console_page() -> str:
    if frontend_available():
        return serve_frontend_index()
    return redirect(url_for("main.terminal_console"))


@main_bp.route("/network")
@login_required
def network_page() -> str:
    if frontend_available():
        return serve_frontend_index()
    db = get_db()
    networks = db.execute(
        "SELECT id, ssid, priority, created_at FROM known_networks ORDER BY priority DESC, ssid ASC"
    ).fetchall()
    return render_template(
        "network.html",
        networks=networks,
        nearby=scan_nearby(),
        tailscale=tailscale_status(),
    )


@main_bp.route("/network/add", methods=["POST"])
@login_required
def network_add() -> Any:
    ssid = request.form.get("ssid", "").strip()
    psk = request.form.get("psk", "")
    if not ssid or not psk:
        flash("SSID and password are both required", "error")
        return redirect(url_for("main.network_page"))
    db = get_db()
    now = _utc_now()
    try:
        db.execute(
            "INSERT INTO known_networks(ssid, psk, priority, created_at) VALUES (?, ?, 10, ?)"
            " ON CONFLICT(ssid) DO UPDATE SET psk=excluded.psk",
            (ssid, psk, now),
        )
        db.commit()
    except Exception as exc:
        flash(f"Database error: {exc}", "error")
        return redirect(url_for("main.network_page"))
    ok, msg = apply_network_add(ssid, psk)
    log_event("network_add", None, _actor(), f"Added known network: {ssid}", msg)
    if ok:
        flash(f"Network '{ssid}' added and applied", "success")
    else:
        flash(f"Saved to database, but nmcli failed: {msg}", "warning")
    return redirect(url_for("main.network_page"))


@main_bp.route("/network/<int:network_id>/delete", methods=["POST"])
@login_required
def network_delete(network_id: int) -> Any:
    db = get_db()
    row = db.execute("SELECT ssid FROM known_networks WHERE id = ?", (network_id,)).fetchone()
    if not row:
        flash("Network not found", "error")
        return redirect(url_for("main.network_page"))
    ssid = row["ssid"]
    db.execute("DELETE FROM known_networks WHERE id = ?", (network_id,))
    db.commit()
    ok, msg = apply_network_remove(ssid)
    log_event("network_remove", None, _actor(), f"Removed known network: {ssid}", msg)
    flash(f"Network '{ssid}' removed", "success" if ok else "warning")
    return redirect(url_for("main.network_page"))


@main_bp.route("/wiki")
@login_required
def wiki_page() -> str:
    if frontend_available():
        return serve_frontend_index()
    return redirect(url_for("main.nethunter_wiki"))


# ── Web Browser ───────────────────────────────────────────────────────────────

@main_bp.route("/web-browser")
@login_required
def web_browser() -> str:
    if frontend_available():
        return serve_frontend_index()
    return render_template(
        "web_browser.html",
        w3m_ok=w3m_installed(),
        bookmarks=None,
        show_bookmarks=False,
    )


@main_bp.route("/web-browser/open", methods=["POST"])
@login_required
def web_browser_open() -> Any:
    if not w3m_installed():
        flash("w3m is not installed. Run: sudo apt install w3m", "error")
        return redirect(url_for("main.web_browser"))
    url = normalize_url(request.form.get("url", ""))
    if not url:
        flash("Please enter a URL.", "error")
        return redirect(url_for("main.web_browser"))
    terminal_manager.write(_terminal_session_key(), w3m_command(url) + "\n")
    log_event("web_browser", None, _actor(), f"w3m opened URL: {url}")
    return redirect(url_for("main.terminal_live"))


@main_bp.route("/web-browser/search", methods=["POST"])
@login_required
def web_browser_search() -> Any:
    if not w3m_installed():
        flash("w3m is not installed. Run: sudo apt install w3m", "error")
        return redirect(url_for("main.web_browser"))
    query = request.form.get("query", "").strip()
    if not query:
        flash("Please enter a search query.", "error")
        return redirect(url_for("main.web_browser"))
    url = build_search_url(query)
    terminal_manager.write(_terminal_session_key(), w3m_command(url) + "\n")
    log_event("web_browser", None, _actor(), f"w3m search: {query}")
    return redirect(url_for("main.terminal_live"))


@main_bp.route("/web-browser/bookmarks")
@login_required
def web_browser_bookmarks() -> str:
    if frontend_available():
        return serve_frontend_index()
    return render_template(
        "web_browser.html",
        w3m_ok=w3m_installed(),
        bookmarks=ensure_bookmarks(),
        show_bookmarks=True,
    )


@main_bp.route("/web-browser/bookmarks/open", methods=["POST"])
@login_required
def web_browser_bookmark_open() -> Any:
    if not w3m_installed():
        flash("w3m is not installed. Run: sudo apt install w3m", "error")
        return redirect(url_for("main.web_browser"))
    url = request.form.get("url", "").strip()
    if not url:
        flash("Please select a bookmark.", "error")
        return redirect(url_for("main.web_browser_bookmarks"))
    terminal_manager.write(_terminal_session_key(), w3m_command(url) + "\n")
    log_event("web_browser", None, _actor(), f"w3m opened bookmark: {url}")
    return redirect(url_for("main.terminal_live"))
