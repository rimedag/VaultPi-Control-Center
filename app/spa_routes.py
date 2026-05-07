from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash

from .auth import login_required
from .db import get_db, set_setting, setting
from .knowledge_center import get_articles
from .services.activity import is_command_execution_enabled, log_event
from .services.cardputer_link import cardputer_api_port, cardputer_payload, cardputer_password

CARDPUTER_DEFAULT_PASSWORD = "password"
CARDPUTER_LEGACY_PASSWORD = "your-bridge-psk"
from .services.checks import check_http, check_tcp
from .services.commands import run_command
from .services.config_sync import sync_from_config
from .services.gitea_ops import all_job_statuses, get_job, latest_local_backup_info, start_job
from .services.logs import read_log_tail
from .services.metrics import system_metrics
from .services.monitor import HealthChecker
from .services.terminal import terminal_manager
from .services.wifi import apply_network_add, apply_network_remove, scan_nearby, tailscale_status
from .services.web_browser import build_search_url, ensure_bookmarks, fetch_text, normalize_url, w3m_installed

spa_api_bp = Blueprint("spa_api", __name__)

SETTINGS_KEYS = [
    "app_name", "dashboard_title", "refresh_interval", "filebrowser_url", "gitea_url", "gitea_config_path",
    "n8n_url", "android_host", "android_termux_ssh_port", "android_ssh_user", "android_gitea_url",
    "android_backup_path", "android_mirror_path", "hostname_label", "theme", "module_projects",
    "module_local_services", "module_remote_services", "module_actions", "module_logs", "auth_enabled",
    "command_execution_enabled", "bind_host", "bind_port", "monitor_interval", "history_retention",
    "config_file_path", "ops_min_disk_free_percent", "ops_min_disk_free_gb", "ops_backup_max_age_hours",
    "ops_android_sync_max_age_days", "ops_backup_dir_warn_gb", "ops_healthcheck_max_age_days",
    "brand_link_text", "brand_link_url", "help_mode",
    "cardputer_host", "cardputer_api_port", "cardputer_password",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor() -> str:
    return g.user["username"] if getattr(g, "user", None) else "system"


def _parse_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [part.strip() for part in raw.split(",") if part.strip()]


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
        row["project_id"]: {
            "status": row["status"],
            "status_code": row["status_code"],
            "response_ms": row["response_ms"],
            "checked_at": row["checked_at"],
        }
        for row in rows
    }


def _service_rows(kind: str):
    db = get_db()
    if kind == "local":
        return db.execute(
            "SELECT * FROM projects WHERE enabled = 1 AND archived = 0 AND type IN ('local app', 'tool', 'service') ORDER BY display_order ASC, name ASC"
        ).fetchall()
    if kind == "remote":
        return db.execute(
            "SELECT * FROM projects WHERE enabled = 1 AND archived = 0 AND type = 'remote app' ORDER BY display_order ASC, name ASC"
        ).fetchall()
    return db.execute("SELECT * FROM projects ORDER BY archived ASC, display_order ASC, name ASC").fetchall()


def _project_to_api(project: Any) -> dict[str, Any]:
    return {
        "id": str(project["id"]),
        "name": project["name"],
        "slug": project["slug"] or "",
        "description": project["description"] or "",
        "category": project["category"] or "utility",
        "type": project["type"] or "binary",
        "environment": project["environment"] or "prod",
        "stack": _parse_list(project["stack"]),
        "repoUrl": project["repo_url"] or None,
        "localUrl": project["local_url"] or None,
        "remoteUrl": project["remote_url"] or None,
        "healthcheckUrl": project["healthcheck_url"] or None,
        "host": project["host_machine"] or socket.gethostname(),
        "port": int(project["port"] or 0),
        "runCommand": project["run_command"] or "",
        "stopCommand": project["stop_command"] or "",
        "restartCommand": project["restart_command"] or "",
        "logPath": project["log_path"] or "",
        "workingDirectory": project["working_directory"] or "",
        "notes": project["notes"] or "",
        "tags": _parse_list(project["tags"]),
        "enabled": bool(project["enabled"]),
        "displayOrder": int(project["display_order"] or 100),
        "monitoringEnabled": bool(project["monitoring_enabled"]),
        "actionEnabled": bool(project["action_enabled"]),
        "archived": bool(project["archived"]),
    }


def _local_service_to_api(project: Any, status_map: dict[int, dict[str, Any]]) -> dict[str, Any]:
    status = (status_map.get(project["id"], {}).get("status") or "unknown").lower()
    mapped_status = "running" if status == "up" else "error" if status == "down" else "stopped"
    log_snippet = read_log_tail(project["log_path"], lines=8).strip()[-400:] if project["log_path"] else ""
    return {
        "id": str(project["id"]),
        "name": project["name"],
        "status": mapped_status,
        "url": project["local_url"] or project["healthcheck_url"] or "",
        "workingDir": project["working_directory"] or "",
        "logSnippet": log_snippet or "",
        "memory": "n/a",
        "cpu": "n/a",
    }


def _remote_service_to_api(project: Any, status_map: dict[int, dict[str, Any]]) -> dict[str, Any]:
    db = get_db()
    latest_status = (status_map.get(project["id"], {}).get("status") or "unknown").lower()
    success = db.execute("SELECT checked_at FROM service_checks WHERE project_id = ? AND status = 'up' ORDER BY checked_at DESC LIMIT 1", (project["id"],)).fetchone()
    failure = db.execute("SELECT checked_at FROM service_checks WHERE project_id = ? AND status = 'down' ORDER BY checked_at DESC LIMIT 1", (project["id"],)).fetchone()
    counts = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up_count FROM service_checks WHERE project_id = ?", (project["id"],)).fetchone()
    total = int(counts["total"] or 0)
    up_count = int(counts["up_count"] or 0)
    uptime = round((up_count / total) * 100.0, 2) if total else None
    mapped_status = "online" if latest_status == "up" else "offline" if latest_status == "down" else "degraded"
    return {
        "id": str(project["id"]),
        "name": project["name"],
        "url": project["remote_url"] or project["healthcheck_url"] or "",
        "status": mapped_status,
        "uptime": uptime,
        "lastSuccess": success["checked_at"] if success else "",
        "lastFailure": failure["checked_at"] if failure else "",
    }


def _activity_type(event_type: str) -> str:
    raw = (event_type or "").lower()
    if "error" in raw or "fail" in raw:
        return "error"
    if "warn" in raw:
        return "warning"
    if any(token in raw for token in ["success", "update", "create", "run", "sync", "login"]):
        return "success"
    return "info"


def _gitea_base_url() -> str:
    url = setting("gitea_url", os.getenv("GITEA_URL", "http://localhost:3000")).strip()
    return url.rstrip("/") or "http://localhost:3000"


def _gitea_json(path: str, timeout: float = 1.8) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{_gitea_base_url()}{path}", timeout=timeout) as response:
            payload = response.read(262_144)
        data = json.loads(payload.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _local_gitea_repo_root() -> Path:
    return Path(os.getenv("LOCAL_REPO_ROOT", "/var/lib/gitea/data/gitea-repositories")).expanduser()


def _local_gitea_repos(repo_root: Path) -> list[Path]:
    if not repo_root.exists():
        return []
    return sorted(path for path in repo_root.rglob("*.git") if path.is_dir())[:250]


def _owner_from_repos(repos: list[Path], repo_root: Path) -> str:
    owners: dict[str, int] = {}
    for repo in repos:
        try:
            owner = repo.relative_to(repo_root).parts[0]
        except Exception:
            continue
        if owner:
            owners[owner] = owners.get(owner, 0) + 1
    return max(owners.items(), key=lambda item: item[1])[0] if owners else "vaultpi"


def _gitea_heatmap_from_repos(repos: list[Path]) -> dict[str, Any]:
    today = date.today()
    start = today - timedelta(days=83)
    counts = {start + timedelta(days=idx): 0 for idx in range(84)}
    for repo in repos:
        try:
            result = subprocess.run(
                [
                    "git",
                    f"--git-dir={repo}",
                    "log",
                    "--all",
                    "--since",
                    start.isoformat(),
                    "--date=short",
                    "--pretty=format:%ad",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            try:
                day = date.fromisoformat(line.strip())
            except ValueError:
                continue
            if day in counts:
                counts[day] += 1
    values = [counts[start + timedelta(days=idx)] for idx in range(84)]
    peak = max(values) if values else 0
    levels: list[int] = []
    for value in values:
        if value <= 0 or peak <= 0:
            levels.append(0)
        elif value >= peak:
            levels.append(4)
        elif value >= max(1, peak * 2 // 3):
            levels.append(3)
        elif value >= max(1, peak // 3):
            levels.append(2)
        else:
            levels.append(1)
    return {
        "counts": values,
        "levels": levels,
        "from": start.isoformat(),
        "to": today.isoformat(),
        "total": sum(values),
        "active_days": sum(1 for value in values if value > 0),
        "peak": peak,
    }


def _run_named_command_by_name(name: str) -> tuple[bool, dict[str, Any]]:
    db = get_db()
    command = db.execute("SELECT * FROM commands WHERE name = ? AND enabled = 1", (name,)).fetchone()
    if not command:
        return False, {"error": f"Command not found: {name}"}
    timeout = int(command["timeout_sec"] or 30)
    result = run_command(command["command"], timeout_sec=timeout, working_directory=command["working_directory"] or "")
    details = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exitCode": result.exit_code,
        "command": command["command"],
        "name": command["name"],
    }
    log_event("quick_action", None, _actor(), f"Executed: {command['name']}", json.dumps(details)[:4000])
    return result.exit_code == 0, details


def _safe_script_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip(".-")
    return slug or "custom-action"


def _wiki_article_to_api(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": article["slug"],
        "title": article["title"],
        "category": article["category"],
        "excerpt": article.get("shortDescription", ""),
        "tags": article.get("tags", []),
        "updatedAt": article.get("lastUpdated", ""),
        "difficulty": article.get("difficulty", ""),
        "badges": article.get("badges", []),
        "prerequisites": article.get("prerequisites", []),
        "warnings": article.get("warnings", []),
        "limitations": article.get("limitations", []),
        "relatedTools": article.get("relatedTools", []),
        "whenToUseIt": article.get("whenToUseIt", ""),
        "generalSections": article.get("generalSections", []),
        "nethunter": article.get("nethunter"),
        "kaliLinux": article.get("kaliLinux"),
        "differences": article.get("differences"),
    }


@spa_api_bp.route("/api/auth/session")
def api_auth_session() -> Response:
    return jsonify({"authenticated": bool(getattr(g, "user", None)), "user": getattr(g, "user", None)["username"] if getattr(g, "user", None) else None})


@spa_api_bp.route("/api/auth/login", methods=["POST"])
def api_auth_login() -> Response:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        log_event("login_failure", None, username or "anonymous", "Failed login attempt")
        return jsonify({"ok": False, "message": "Invalid username or password"}), 401
    session.clear()
    session["user_id"] = user["id"]
    log_event("login_success", None, username, "User logged in")
    return jsonify({"ok": True, "user": username})


@spa_api_bp.route("/api/auth/logout", methods=["POST"])
@login_required
def api_auth_logout() -> Response:
    actor = _actor()
    session.clear()
    log_event("logout", None, actor, "User logged out")
    return jsonify({"ok": True})


@spa_api_bp.route("/api/stats")
@login_required
def api_stats() -> Response:
    metrics = system_metrics()
    disk_total = float(metrics.get("disk_total_gb", 0.0) or 0.0)
    mounted_paths = [{"path": "/", "usage": int(round(float(metrics.get("disk_percent", 0.0) or 0.0))), "total": f"{disk_total:.1f} GB" if disk_total else "n/a"}]
    ips = [{"iface": f"net{index + 1}", "ip": ip} for index, ip in enumerate(metrics.get("ip_addresses", []))]
    return jsonify({
        "hostname": setting("hostname_label", "").strip() or metrics.get("hostname", socket.gethostname()),
        "uptime": metrics.get("uptime", "n/a"),
        "cpuPercent": int(round(float(metrics.get("cpu_percent", 0.0) or 0.0))),
        "ramPercent": int(round(float(metrics.get("ram_percent", 0.0) or 0.0))),
        "diskPercent": int(round(float(metrics.get("disk_percent", 0.0) or 0.0))),
        "cpuTemp": metrics.get("temperature_c") or 0,
        "loadAvg": str(metrics.get("load_avg", "n/a")).replace(",", ""),
        "ips": ips,
        "mountedPaths": mounted_paths,
    })


@spa_api_bp.route("/api/activity")
@login_required
def api_activity() -> Response:
    rows = get_db().execute("SELECT id, event_type, actor, message, created_at FROM activity_log ORDER BY created_at DESC LIMIT 12").fetchall()
    return jsonify([
        {"id": f"a{row['id']}", "timestamp": row["created_at"], "message": row["message"], "type": _activity_type(row["event_type"]), "source": row["actor"] or row["event_type"]}
        for row in rows
    ])


@spa_api_bp.route("/api/projects", methods=["GET", "POST"])
@login_required
def api_projects() -> Response:
    db = get_db()
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        now = _utc_now()
        name = str(payload.get("name", "")).strip()
        if not name:
            return jsonify({"error": "Project name is required"}), 400
        base_slug = str(payload.get("slug", "")).strip() or name.lower().replace(" ", "-")
        slug = base_slug
        suffix = 2
        while db.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        db.execute(
            """
            INSERT INTO projects (
                name, slug, description, category, type, environment, stack, repo_url, local_url, remote_url,
                healthcheck_url, host_machine, port, run_command, stop_command, restart_command, log_path,
                working_directory, notes, tags, enabled, display_order, monitoring_enabled, action_enabled,
                archived, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                slug,
                str(payload.get("description", "")).strip(),
                str(payload.get("category", "utility")).strip() or "utility",
                str(payload.get("type", "binary")).strip() or "binary",
                str(payload.get("environment", "prod")).strip() or "prod",
                json.dumps(_parse_list(payload.get("stack"))),
                str(payload.get("repoUrl", "")).strip(),
                str(payload.get("localUrl", "")).strip(),
                str(payload.get("remoteUrl", "")).strip(),
                str(payload.get("healthcheckUrl", "")).strip(),
                str(payload.get("host", socket.gethostname())).strip(),
                int(payload.get("port") or 0),
                str(payload.get("runCommand", "")).strip(),
                str(payload.get("stopCommand", "")).strip(),
                str(payload.get("restartCommand", "")).strip(),
                str(payload.get("logPath", "")).strip(),
                str(payload.get("workingDirectory", "")).strip(),
                str(payload.get("notes", "")).strip(),
                json.dumps(_parse_list(payload.get("tags"))),
                1 if payload.get("enabled", True) else 0,
                int(payload.get("displayOrder") or 100),
                1 if payload.get("monitoringEnabled", True) else 0,
                1 if payload.get("actionEnabled", False) else 0,
                1 if payload.get("archived", False) else 0,
                now,
                now,
            ),
        )
        db.commit()
        project = db.execute("SELECT * FROM projects WHERE id = last_insert_rowid()").fetchone()
        log_event("project_create", project["id"], _actor(), f"Created project {project['name']}")
        return jsonify(_project_to_api(project)), 201
    rows = db.execute("SELECT * FROM projects ORDER BY archived ASC, display_order ASC, name ASC").fetchall()
    return jsonify([_project_to_api(row) for row in rows])


@spa_api_bp.route("/api/projects/<int:project_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def api_project_detail(project_id: int) -> Response:
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return jsonify({"error": "Project not found"}), 404
    if request.method == "GET":
        return jsonify(_project_to_api(project))
    if request.method == "DELETE":
        db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        db.commit()
        log_event("project_delete", project_id, _actor(), f"Deleted project {project['name']}")
        return jsonify({"ok": True})
    payload = request.get_json(silent=True) or {}
    now = _utc_now()
    db.execute(
        """
        UPDATE projects SET
            name = ?, slug = ?, description = ?, category = ?, type = ?, environment = ?, stack = ?, repo_url = ?,
            local_url = ?, remote_url = ?, healthcheck_url = ?, host_machine = ?, port = ?, run_command = ?, stop_command = ?,
            restart_command = ?, log_path = ?, working_directory = ?, notes = ?, tags = ?, enabled = ?, display_order = ?,
            monitoring_enabled = ?, action_enabled = ?, archived = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(payload.get("name", project["name"])).strip(),
            str(payload.get("slug", project["slug"])).strip() or project["slug"],
            str(payload.get("description", project["description"] or "")).strip(),
            str(payload.get("category", project["category"] or "utility")).strip() or "utility",
            str(payload.get("type", project["type"] or "binary")).strip() or "binary",
            str(payload.get("environment", project["environment"] or "prod")).strip() or "prod",
            json.dumps(_parse_list(payload.get("stack", project["stack"]))),
            str(payload.get("repoUrl", project["repo_url"] or "")).strip(),
            str(payload.get("localUrl", project["local_url"] or "")).strip(),
            str(payload.get("remoteUrl", project["remote_url"] or "")).strip(),
            str(payload.get("healthcheckUrl", project["healthcheck_url"] or "")).strip(),
            str(payload.get("host", project["host_machine"] or socket.gethostname())).strip(),
            int(payload.get("port") or project["port"] or 0),
            str(payload.get("runCommand", project["run_command"] or "")).strip(),
            str(payload.get("stopCommand", project["stop_command"] or "")).strip(),
            str(payload.get("restartCommand", project["restart_command"] or "")).strip(),
            str(payload.get("logPath", project["log_path"] or "")).strip(),
            str(payload.get("workingDirectory", project["working_directory"] or "")).strip(),
            str(payload.get("notes", project["notes"] or "")).strip(),
            json.dumps(_parse_list(payload.get("tags", project["tags"]))),
            1 if payload.get("enabled", bool(project["enabled"])) else 0,
            int(payload.get("displayOrder") or project["display_order"] or 100),
            1 if payload.get("monitoringEnabled", bool(project["monitoring_enabled"])) else 0,
            1 if payload.get("actionEnabled", bool(project["action_enabled"])) else 0,
            1 if payload.get("archived", bool(project["archived"])) else 0,
            now,
            project_id,
        ),
    )
    db.commit()
    updated = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    log_event("project_update", project_id, _actor(), f"Updated project {updated['name']}")
    return jsonify(_project_to_api(updated))

@spa_api_bp.route("/api/services/local")
@login_required
def api_local_services() -> Response:
    status_map = _latest_status_map()
    return jsonify([_local_service_to_api(project, status_map) for project in _service_rows("local")])


@spa_api_bp.route("/api/services/local/<int:project_id>/<action>", methods=["POST"])
@login_required
def api_local_service_action(project_id: int, action: str) -> Response:
    if not is_command_execution_enabled():
        return jsonify({"error": "Command execution is disabled"}), 403
    project = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return jsonify({"error": "Service not found"}), 404
    if project["action_enabled"] != 1:
        return jsonify({"error": "Actions are disabled for this service"}), 403
    cmd_map = {"start": project["run_command"], "stop": project["stop_command"], "restart": project["restart_command"]}
    cmd = cmd_map.get(action)
    if not cmd:
        return jsonify({"error": "Action command is not configured"}), 400
    result = run_command(cmd, timeout_sec=45, working_directory=project["working_directory"] or "")
    details = {"stdout": result.stdout, "stderr": result.stderr, "exitCode": result.exit_code}
    log_event("service_action", project_id, _actor(), f"{action} {project['name']} exit={result.exit_code}", json.dumps(details)[:2000])
    return jsonify({"ok": result.exit_code == 0, **details})


@spa_api_bp.route("/api/services/remote")
@login_required
def api_remote_services() -> Response:
    status_map = _latest_status_map()
    return jsonify([_remote_service_to_api(project, status_map) for project in _service_rows("remote")])


@spa_api_bp.route("/api/monitoring")
@login_required
def api_monitoring() -> Response:
    db = get_db()
    rows = db.execute("SELECT project_id, status, response_ms, checked_at FROM service_checks ORDER BY checked_at DESC LIMIT 400").fetchall()
    history_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        history_map.setdefault(row["project_id"], []).append({
            "time": row["checked_at"] or "",
            "timestamp": row["checked_at"] or "",
            "checked_at": row["checked_at"] or "",
            "status": (row["status"] or "unknown").lower(),
            "responseMs": int(row["response_ms"] or 0),
            "response_ms": int(row["response_ms"] or 0),
        })
    status_map = _latest_status_map()
    services_payload = []
    services = db.execute(
        """
        SELECT *
        FROM projects
        WHERE enabled = 1 AND archived = 0 AND monitoring_enabled = 1
        ORDER BY display_order ASC, name ASC
        """
    ).fetchall()
    for service in services:
        history = list(reversed(history_map.get(service["id"], [])))
        total = len(history)
        up = sum(1 for item in history if item["status"] == "up")
        latest = status_map.get(service["id"], {})
        status = (latest.get("status") or (history[-1]["status"] if history else "unknown")).lower()
        services_payload.append({
            "id": str(service["id"]),
            "name": service["name"],
            "url": service["healthcheck_url"] or service["remote_url"] or service["local_url"] or "",
            "status": status,
            "uptime": round((up / total) * 100.0, 1) if total else 0,
            "lastChecked": latest.get("checked_at") or (history[-1]["time"] if history else ""),
            "responseMs": int(latest.get("response_ms") or (history[-1]["responseMs"] if history else 0)),
            "history": history[-60:],
        })
    android_host = setting("android_host", "").strip()
    android_ssh_port = int(setting("android_termux_ssh_port", "8022") or "8022")
    android_gitea_url = setting("android_gitea_url", "").strip()
    termux_check = check_tcp(android_host, android_ssh_port, timeout=1.8) if android_host else None
    gitea_check = check_http(android_gitea_url, timeout=1.8) if android_gitea_url else None
    return jsonify({
        "services": services_payload,
        "android": {
            "host": android_host,
            "sshPort": android_ssh_port,
            "giteaUrl": android_gitea_url,
            "termuxStatus": termux_check.status if termux_check else "unknown",
            "androidGiteaStatus": gitea_check.status if gitea_check else "unknown",
        },
    })


@spa_api_bp.route("/api/monitoring/check-now", methods=["POST"])
@login_required
def api_monitoring_check_now() -> Response:
    checker = current_app.extensions.get("health_checker")
    if checker:
        checker.run_once()
    else:
        HealthChecker(current_app).run_once()
    log_event("monitor_now", None, _actor(), "Triggered immediate health checks")
    return jsonify({"ok": True})


@spa_api_bp.route("/api/actions/gitea")
@login_required
def api_actions_gitea() -> Response:
    jobs = all_job_statuses()
    payload = []
    for key, job in jobs.items():
        status = "running" if job.get("running") else str(job.get("last_status") or "pending")
        normalized = "success" if status == "success" else "failed" if status == "failed" else "running" if status == "running" else "pending"
        payload.append({
            "id": key,
            "label": job.get("label") or key,
            "status": normalized,
            "startedAt": job.get("last_started_at") or "",
            "finishedAt": job.get("last_finished_at") or "",
            "duration": job.get("last_duration") or "",
            "message": job.get("last_message") or "",
            "artifactPath": job.get("artifact_path") or "",
            "tail": job.get("tail") or "",
            "logPath": job.get("log_path") or "",
        })
    return jsonify(payload)


@spa_api_bp.route("/api/actions/gitea/<job_key>/run", methods=["POST"])
@login_required
def api_actions_gitea_run(job_key: str) -> Response:
    if not is_command_execution_enabled():
        return jsonify({"error": "Command execution is disabled"}), 403
    job = get_job(job_key)
    if not job:
        return jsonify({"error": "Unknown Gitea job"}), 404
    state, message = start_job(job, actor=_actor())
    log_event("gitea_operation_start", None, _actor(), f"{job.key} -> {state}", message)
    return jsonify({"ok": state == "started", "state": state, "message": message}), 200 if state == "started" else 409 if state == "already_running" else 400


@spa_api_bp.route("/api/actions/commands")
@login_required
def api_actions_commands() -> Response:
    rows = get_db().execute("SELECT id, name, description, command, working_directory, timeout_sec FROM commands WHERE enabled = 1 ORDER BY name ASC").fetchall()
    return jsonify([{"id": row["id"], "name": row["name"], "description": row["description"] or "", "command": row["command"], "workingDirectory": row["working_directory"] or "", "timeoutSec": int(row["timeout_sec"] or 30)} for row in rows])


@spa_api_bp.route("/api/actions/commands", methods=["POST"])
@login_required
def api_actions_command_create() -> Response:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    command = str(payload.get("command", "")).strip()
    script_content = str(payload.get("scriptContent", "")).replace("\r\n", "\n")
    working_directory = str(payload.get("workingDirectory", "")).strip()
    timeout_sec = max(1, min(int(payload.get("timeoutSec") or 60), 3600))

    if not name:
        return jsonify({"error": "Action name is required"}), 400
    if not command and not script_content.strip():
        return jsonify({"error": "Enter a command or script content"}), 400

    if script_content.strip():
        scripts_dir = Path(current_app.instance_path) / "custom-actions"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_path = scripts_dir / f"{_safe_script_name(name)}.sh"
        script_path.write_text(script_content if script_content.endswith("\n") else f"{script_content}\n", encoding="utf-8")
        try:
            script_path.chmod(0o700)
        except OSError:
            pass
        command = f"/bin/bash {shlex.quote(str(script_path))}"

    db = get_db()
    now = _utc_now()
    try:
        db.execute(
            """
            INSERT INTO commands(name, description, command, working_directory, timeout_sec, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (name, description, command, working_directory, timeout_sec, now),
        )
        db.commit()
    except Exception as exc:
        return jsonify({"error": f"Unable to create action: {exc}"}), 400

    row = db.execute("SELECT id, name, description, command, working_directory, timeout_sec FROM commands WHERE id = last_insert_rowid()").fetchone()
    log_event("quick_action_create", None, _actor(), f"Created quick action {name}")
    return jsonify({
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "command": row["command"],
        "workingDirectory": row["working_directory"] or "",
        "timeoutSec": int(row["timeout_sec"] or 60),
    }), 201


@spa_api_bp.route("/api/actions/command/<int:command_id>/run", methods=["POST"])
@login_required
def api_actions_command_run(command_id: int) -> Response:
    if not is_command_execution_enabled():
        return jsonify({"error": "Command execution is disabled"}), 403
    command = get_db().execute("SELECT * FROM commands WHERE id = ? AND enabled = 1", (command_id,)).fetchone()
    if not command:
        return jsonify({"error": "Command not found"}), 404
    result = run_command(command["command"], timeout_sec=int(command["timeout_sec"] or 30), working_directory=command["working_directory"] or "")
    details = {"stdout": result.stdout, "stderr": result.stderr, "exitCode": result.exit_code, "command": command["command"], "name": command["name"]}
    log_event("quick_action", None, _actor(), f"Executed: {command['name']}", json.dumps(details)[:4000])
    return jsonify({"ok": result.exit_code == 0, **details})


@spa_api_bp.route("/api/actions/sync-config", methods=["POST"])
@login_required
def api_actions_sync_config() -> Response:
    raw_path = setting("config_file_path", os.getenv("CONTROL_CENTER_CONFIG_PATH", "config/control_center.json")).strip() or "config/control_center.json"
    base_dir = os.path.abspath(os.path.join(current_app.root_path, ".."))
    config_path = raw_path if os.path.isabs(raw_path) else os.path.join(base_dir, raw_path)
    try:
        stats = sync_from_config(config_path)
        log_event("config_sync", None, _actor(), "Synchronized config file", json.dumps({"path": config_path, **stats}))
        return jsonify({"ok": True, **stats, "path": config_path})
    except Exception as exc:
        log_event("config_sync_error", None, _actor(), "Config sync failed", str(exc))
        return jsonify({"error": str(exc)}), 500


@spa_api_bp.route("/api/actions/shutdown", methods=["POST"])
@login_required
def api_actions_shutdown() -> Response:
    if not is_command_execution_enabled():
        return jsonify({"error": "Command execution is disabled"}), 403
    # Try named command first (user-configurable)
    command = get_db().execute("SELECT * FROM commands WHERE name = ? AND enabled = 1", ("Safe Shutdown Pi",)).fetchone()
    if command:
        ok, details = _run_named_command_by_name("Safe Shutdown Pi")
        return jsonify({"ok": ok, "message": "System is shutting down safely." if ok else "Safe shutdown command failed.", **details}), 200 if ok else 500
    # Fallback: run shutdown directly — Pi systemd user should have sudo rights for this
    result = run_command("sudo shutdown -h now", timeout_sec=10)
    log_event("system_shutdown", None, _actor(), "System shutdown initiated (direct fallback)")
    # shutdown -h now exits 0 immediately while the kernel shuts down; any exit is a success here
    return jsonify({"ok": True, "message": "System is shutting down safely."})

@spa_api_bp.route("/api/console/run", methods=["POST"])
@login_required
def api_console_run() -> Response:
    if not is_command_execution_enabled():
        return jsonify({"error": "Command execution is disabled"}), 403
    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()
    working_directory = str(payload.get("workingDirectory", "")).strip()
    timeout_sec = max(1, min(int(payload.get("timeoutSec") or 20), 120))
    if not command:
        return jsonify({"error": "Command is required"}), 400
    result = run_command(command, timeout_sec=timeout_sec, working_directory=working_directory)
    log_event("console_command", None, _actor(), f"Executed console command: {command}", json.dumps({"exit": result.exit_code})[:500])
    return jsonify({"stdout": result.stdout, "stderr": result.stderr, "exitCode": result.exit_code})


@spa_api_bp.route("/api/logs/sources")
@login_required
def api_logs_sources() -> Response:
    rows = get_db().execute("SELECT id, name, log_path FROM projects WHERE archived = 0 AND log_path IS NOT NULL AND log_path != '' ORDER BY name ASC").fetchall()
    return jsonify([{"id": str(row["id"]), "name": row["name"], "logPath": row["log_path"]} for row in rows])


@spa_api_bp.route("/api/logs")
@login_required
def api_logs() -> Response:
    project_id = request.args.get("project_id", "").strip()
    lines_raw = request.args.get("lines", "120").strip()
    lines = max(20, min(500, int(lines_raw) if lines_raw.isdigit() else 120))
    if not project_id:
        return jsonify({"content": "", "source": None})
    row = get_db().execute("SELECT id, name, log_path FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        return jsonify({"error": "Log source not found"}), 404
    return jsonify({"source": {"id": str(row["id"]), "name": row["name"], "logPath": row["log_path"]}, "content": read_log_tail(row["log_path"], lines=lines)})


@spa_api_bp.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings() -> Response:
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        for key in SETTINGS_KEYS:
            if key not in payload:
                continue
            value = payload.get(key)
            if key.startswith("module_") or key in {"auth_enabled", "command_execution_enabled", "help_mode"}:
                set_setting(key, "1" if value else "0")
            else:
                set_setting(key, str(value or "").strip())
        log_event("settings_update", None, _actor(), "Updated settings")
    settings_map = {key: setting(key, "") for key in SETTINGS_KEYS}
    if not settings_map.get("hostname_label"):
        settings_map["hostname_label"] = socket.gethostname()
    return jsonify(settings_map)


@spa_api_bp.route("/api/restore-playbook")
@login_required
def api_restore_playbook() -> Response:
    jobs = all_job_statuses()
    backup_info = latest_local_backup_info()
    try:
        repo_count = len(list(Path("/var/lib/gitea/data/gitea-repositories").rglob("*.git")))
    except Exception:
        repo_count = 0
    android_sync = jobs.get("gitea-sync-android", {})
    return jsonify({
        "localBackupDir": os.getenv("LOCAL_BACKUP_DIR", "/backup/gitea"),
        "androidBackupDir": setting("android_backup_path", "/data/data/com.termux/files/home/gitea-backups"),
        "androidMirrorDir": setting("android_mirror_path", "/data/data/com.termux/files/home/gitea-mirrors"),
        "localRepoDir": "/var/lib/gitea/data/gitea-repositories",
        "latestBackupFile": backup_info.get("filename", ""),
        "latestBackupTime": backup_info.get("mtime", ""),
        "repoCount": repo_count,
        "androidSyncStatus": android_sync.get("last_status", "never"),
        "androidSyncFinishedAt": android_sync.get("last_finished_at", ""),
    })


@spa_api_bp.route("/api/wiki")
@login_required
def api_wiki() -> Response:
    articles = get_articles()
    return jsonify([_wiki_article_to_api(article) for article in articles])


@spa_api_bp.route("/api/network/wifi")
@login_required
def api_network_wifi_list() -> Response:
    db = get_db()
    rows = db.execute(
        "SELECT id, ssid, priority, created_at FROM known_networks ORDER BY priority DESC, ssid ASC"
    ).fetchall()
    return jsonify({
        "known": [{"id": r["id"], "ssid": r["ssid"], "priority": r["priority"], "created_at": r["created_at"]} for r in rows],
        "nearby": scan_nearby(),
        "tailscale": tailscale_status(),
    })


@spa_api_bp.route("/api/network/wifi", methods=["POST"])
@login_required
def api_network_wifi_add() -> Response:
    payload = request.get_json(silent=True) or {}
    ssid = str(payload.get("ssid", "")).strip()
    psk = str(payload.get("psk", "")).strip()
    if not ssid or not psk:
        return jsonify({"ok": False, "message": "SSID and password required"}), 400
    db = get_db()
    now = _utc_now()
    db.execute(
        "INSERT INTO known_networks(ssid, psk, priority, created_at) VALUES (?, ?, 10, ?)"
        " ON CONFLICT(ssid) DO UPDATE SET psk=excluded.psk",
        (ssid, psk, now),
    )
    db.commit()
    ok, msg = apply_network_add(ssid, psk)
    log_event("network_add", None, _actor(), f"Added known network: {ssid}", msg)
    return jsonify({"ok": True, "nmcli_ok": ok, "message": msg})


@spa_api_bp.route("/api/network/wifi/<int:network_id>", methods=["DELETE"])
@login_required
def api_network_wifi_delete(network_id: int) -> Response:
    db = get_db()
    row = db.execute("SELECT ssid FROM known_networks WHERE id = ?", (network_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "message": "Not found"}), 404
    ssid = row["ssid"]
    db.execute("DELETE FROM known_networks WHERE id = ?", (network_id,))
    db.commit()
    ok, msg = apply_network_remove(ssid)
    log_event("network_remove", None, _actor(), f"Removed known network: {ssid}", msg)
    return jsonify({"ok": True, "nmcli_ok": ok, "message": msg})


@spa_api_bp.route("/api/network/tailscale")
@login_required
def api_network_tailscale() -> Response:
    return jsonify(tailscale_status())


# ── Web Browser ────────────────────────────────────────────────────────────────

@spa_api_bp.route("/api/web-browser/status")
@login_required
def api_web_browser_status() -> Response:
    return jsonify({"w3m_installed": w3m_installed()})


@spa_api_bp.route("/api/web-browser/bookmarks")
@login_required
def api_web_browser_bookmarks() -> Response:
    return jsonify(ensure_bookmarks())


@spa_api_bp.route("/api/web-browser/fetch", methods=["POST"])
@login_required
def api_web_browser_fetch() -> Response:
    payload = request.get_json(silent=True) or {}
    url = normalize_url(str(payload.get("url", "")))
    result = fetch_text(url, cols=payload.get("cols") or 96)
    if result.get("ok"):
        log_event("web_browser", None, _actor(), f"Fetched page: {result.get('url', url)}")
    return jsonify(result), 200 if result.get("ok") else 400


@spa_api_bp.route("/api/web-browser/search", methods=["POST"])
@login_required
def api_web_browser_search() -> Response:
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    if not query:
        return jsonify({"ok": False, "error": "Search query is required", "lines": []}), 400
    url = build_search_url(query)
    result = fetch_text(url, cols=payload.get("cols") or 96)
    if result.get("ok"):
        log_event("web_browser", None, _actor(), f"Searched web: {query}")
    return jsonify({**result, "query": query}), 200 if result.get("ok") else 400


@spa_api_bp.route("/api/cardputer/monitor")
@login_required
def api_cardputer_monitor() -> Response:
    status = check_tcp("127.0.0.1", cardputer_api_port(), timeout=1.2)
    payload = cardputer_payload(include_password=True)
    payload.update(
        {
            "online": status.status == "up",
            "status": status.status,
            "responseMs": status.response_ms,
            "error": "" if status.status == "up" else f"Cardputer listener not reachable on port {cardputer_api_port()}",
        }
    )
    return jsonify(payload)


def _cardputer_request_password() -> str:
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if request.headers.get("X-Cardputer-Password", "").strip():
        return request.headers.get("X-Cardputer-Password", "").strip()
    if request.args.get("password", "").strip():
        return request.args.get("password", "").strip()
    payload = request.get_json(silent=True) or {}
    return str(payload.get("password", "")).strip()


def _cardputer_authorized() -> bool:
    expected = cardputer_password()
    provided = _cardputer_request_password()
    if not (expected and provided):
        return False
    allowed = {expected}
    if expected in {CARDPUTER_DEFAULT_PASSWORD, CARDPUTER_LEGACY_PASSWORD}:
        allowed.update({CARDPUTER_DEFAULT_PASSWORD, CARDPUTER_LEGACY_PASSWORD})
    return provided in allowed


def _cardputer_require_auth() -> Response | None:
    if _cardputer_authorized():
        return None
    return jsonify({"ok": False, "error": "Invalid Cardputer password"}), 401


def _cardputer_service_status(project: Any, status_map: dict[int, dict[str, Any]]) -> str:
    status = (status_map.get(project["id"], {}).get("status") or "unknown").lower()
    if status == "up":
        return "running"
    if status == "down":
        return "error"
    return "unknown"


def _cardputer_action_payload(row: Any) -> dict[str, Any]:
    return {
        "id": f"cmd-{row['id']}",
        "label": row["name"],
        "kind": "command",
        "status": "ready",
        "run_path": f"/api/v1/actions/cmd-{row['id']}/run",
    }


def _cardputer_output_lines(text: str, width: int = 80, limit: int = 100) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line:
            lines.append("")
        while len(line) > width:
            lines.append(line[:width])
            line = line[width:]
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines[:limit]


@spa_api_bp.route("/api/cardputer/connect", methods=["GET", "POST"])
def api_cardputer_connect() -> Response:
    if not _cardputer_authorized():
        return jsonify({"ok": False, "error": "Invalid Cardputer password"}), 401
    return jsonify({"ok": True, "online": True, **cardputer_payload(include_password=False)})


@spa_api_bp.route("/api/cardputer/status", methods=["GET", "POST"])
def api_cardputer_status() -> Response:
    if not _cardputer_authorized():
        return jsonify({"ok": False, "error": "Invalid Cardputer password"}), 401
    metrics = system_metrics()
    return jsonify(
        {
            "ok": True,
            "online": True,
            **cardputer_payload(include_password=False),
            "metrics": {
                "uptime": metrics.get("uptime", "n/a"),
                "cpuPercent": int(round(float(metrics.get("cpu_percent", 0.0) or 0.0))),
                "ramPercent": int(round(float(metrics.get("ram_percent", 0.0) or 0.0))),
                "diskPercent": int(round(float(metrics.get("disk_percent", 0.0) or 0.0))),
                "temperatureC": metrics.get("temperature_c"),
            },
        }
    )


@spa_api_bp.route("/api/v1/dashboard")
def api_v1_cardputer_dashboard() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    metrics = system_metrics()
    db = get_db()
    status_map = _latest_status_map()
    service_rows = _service_rows("local") + _service_rows("remote")
    services_total = len(service_rows)
    services_up = sum(1 for row in service_rows if _cardputer_service_status(row, status_map) in {"running", "online"})
    services_down = sum(1 for row in service_rows if _cardputer_service_status(row, status_map) == "error")
    last_event = db.execute(
        "SELECT event_type, message FROM activity_log ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return jsonify(
        {
            "hostname": setting("hostname_label", "").strip() or metrics.get("hostname", socket.gethostname()),
            "uptime": metrics.get("uptime", "n/a"),
            "cpuPercent": int(round(float(metrics.get("cpu_percent", 0.0) or 0.0))),
            "ramPercent": int(round(float(metrics.get("ram_percent", 0.0) or 0.0))),
            "diskPercent": int(round(float(metrics.get("disk_percent", 0.0) or 0.0))),
            "cpuTemp": int(round(float(metrics.get("temperature_c") or 0))),
            "loadAvg": str(metrics.get("load_avg", "n/a")).replace(",", ""),
            "servicesTotal": services_total,
            "servicesUp": services_up,
            "servicesDown": services_down,
            "sdErrors": 0,
            "lastEvent": {
                "message": last_event["message"] if last_event else "Control center online",
                "type": _activity_type(last_event["event_type"]) if last_event else "info",
            },
        }
    )


@spa_api_bp.route("/api/v1/services")
def api_v1_cardputer_services() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    status_map = _latest_status_map()
    payload = []
    for row in _service_rows("local"):
        payload.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "kind": "local",
                "status": _cardputer_service_status(row, status_map),
            }
        )
    for row in _service_rows("remote"):
        payload.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "kind": "remote",
                "status": _cardputer_service_status(row, status_map),
            }
        )
    return jsonify(payload)


@spa_api_bp.route("/api/v1/services/<int:project_id>/<action>", methods=["POST"])
def api_v1_cardputer_service_action(project_id: int, action: str) -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    if not is_command_execution_enabled():
        return jsonify({"ok": False, "error": "Command execution is disabled"}), 403
    project = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return jsonify({"ok": False, "error": "Service not found"}), 404
    command = {
        "start": project["run_command"] or "",
        "stop": project["stop_command"] or "",
        "restart": project["restart_command"] or "",
    }.get(action.lower(), "")
    if not command:
        return jsonify({"ok": False, "error": f"No {action} command configured"}), 400
    result = run_command(command, timeout_sec=30, working_directory=project["working_directory"] or "")
    ok = result.exit_code == 0
    log_event("cardputer_service_action", project_id, "cardputer", f"{action}: {project['name']}")
    combined = (result.stdout + result.stderr).strip()
    return jsonify(
        {
            "ok": ok,
            "message": combined or ("done" if ok else "failed"),
            "lines": _cardputer_output_lines(combined),
            "exit_code": result.exit_code,
        }
    ), 200 if ok else 500


@spa_api_bp.route("/api/v1/actions")
def api_v1_cardputer_actions() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    rows = get_db().execute(
        "SELECT id, name, description, command, working_directory, timeout_sec FROM commands WHERE enabled = 1 ORDER BY name ASC"
    ).fetchall()
    payload = [_cardputer_action_payload(row) for row in rows]
    for key, job in all_job_statuses().items():
        payload.append(
            {
                "id": f"gitea-{key}",
                "label": job.get("label") or key,
                "kind": "gitea",
                "status": "running" if job.get("running") else str(job.get("last_status") or "ready"),
                "run_path": f"/api/v1/actions/gitea-{key}/run",
            }
        )
    return jsonify(payload)


@spa_api_bp.route("/api/v1/actions/<action_id>/run", methods=["POST"])
def api_v1_cardputer_action_run(action_id: str) -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    if not is_command_execution_enabled():
        return jsonify({"ok": False, "error": "Command execution is disabled"}), 403
    if action_id == "sys-shutdown":
        command = get_db().execute("SELECT * FROM commands WHERE name = ? AND enabled = 1", ("Safe Shutdown Pi",)).fetchone()
        if command:
            result = run_command(command["command"], timeout_sec=int(command["timeout_sec"] or 10), working_directory=command["working_directory"] or "")
        else:
            result = run_command("sudo shutdown -h now", timeout_sec=10)
        log_event("cardputer_shutdown", None, "cardputer", "System shutdown requested from Cardputer")
        combined = (result.stdout + result.stderr).strip()
        return jsonify(
            {
                "ok": result.exit_code == 0,
                "message": "System is shutting down safely." if result.exit_code == 0 else (combined or "Shutdown command failed"),
                "lines": _cardputer_output_lines(combined),
                "exit_code": result.exit_code,
            }
        ), 200 if result.exit_code == 0 else 500
    if action_id.startswith("cmd-"):
        raw_id = action_id[4:]
        if not raw_id.isdigit():
            return jsonify({"ok": False, "error": "Invalid action id"}), 400
        command = get_db().execute("SELECT * FROM commands WHERE id = ? AND enabled = 1", (int(raw_id),)).fetchone()
        if not command:
            return jsonify({"ok": False, "error": "Action not found"}), 404
        result = run_command(command["command"], timeout_sec=int(command["timeout_sec"] or 30), working_directory=command["working_directory"] or "")
        ok = result.exit_code == 0
        log_event("cardputer_action", None, "cardputer", f"Executed: {command['name']}")
        combined = (result.stdout + result.stderr).strip()
        return jsonify(
            {
                "ok": ok,
                "message": combined or ("done" if ok else "failed"),
                "lines": _cardputer_output_lines(combined),
                "exit_code": result.exit_code,
            }
        ), 200 if ok else 500
    if action_id.startswith("gitea-"):
        job = get_job(action_id[6:])
        if not job:
            return jsonify({"ok": False, "error": "Action not found"}), 404
        state, message = start_job(job, actor="cardputer")
        return jsonify({"ok": state == "started", "message": message}), 200 if state == "started" else 409
    return jsonify({"ok": False, "error": "Action not found"}), 404


@spa_api_bp.route("/api/v1/activity")
def api_v1_cardputer_activity() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    rows = get_db().execute(
        "SELECT event_type, message FROM activity_log ORDER BY created_at DESC LIMIT 12"
    ).fetchall()
    return jsonify([{"message": row["message"], "type": _activity_type(row["event_type"])} for row in rows])


@spa_api_bp.route("/api/v1/cardputer/alerts")
def api_v1_cardputer_alerts() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    status_map = _latest_status_map()
    alerts = []
    for row in _service_rows("local") + _service_rows("remote"):
        if _cardputer_service_status(row, status_map) == "error":
            alerts.append({"message": f"{row['name']} is down", "level": "warn", "read": False})
    return jsonify(alerts[:16])


@spa_api_bp.route("/api/v1/cardputer/alerts", methods=["DELETE"])
def api_v1_cardputer_alerts_clear() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    return jsonify({"ok": True})


@spa_api_bp.route("/api/v1/gitea")
def api_v1_cardputer_gitea() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    repo_root = _local_gitea_repo_root()
    repos = _local_gitea_repos(repo_root)
    heatmap = _gitea_heatmap_from_repos(repos)
    username = _owner_from_repos(repos, repo_root)
    version = _gitea_json("/api/v1/version")
    user_data = _gitea_json(f"/api/v1/users/{username}") if username else {}

    return jsonify(
        {
            "ok": True,
            "user": {
                "username": user_data.get("login") or user_data.get("username") or username,
                "full_name": user_data.get("full_name") or user_data.get("fullName") or "",
                "html_url": user_data.get("html_url") or f"{_gitea_base_url()}/{username}",
                "followers": int(user_data.get("followers_count") or 0),
                "following": int(user_data.get("following_count") or 0),
                "stars": int(user_data.get("starred_repos_count") or 0),
                "repos": len(repos),
            },
            "version": {"server": str(version.get("version") or version.get("server_version") or "Gitea")},
            "summary": {
                "total": heatmap["total"],
                "active_days": heatmap["active_days"],
                "peak": heatmap["peak"],
            },
            "heatmap": {
                "from": heatmap["from"],
                "to": heatmap["to"],
                "levels": heatmap["levels"],
                "counts": heatmap["counts"],
            },
        }
    )


@spa_api_bp.route("/api/v1/web/fetch", methods=["POST"])
def api_v1_cardputer_web_fetch() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    payload = request.get_json(silent=True) or {}
    if payload.get("query"):
        url = build_search_url(str(payload.get("query", "")).strip())
    else:
        url = normalize_url(str(payload.get("url", "")))
    result = fetch_text(url, cols=39)
    return jsonify(result), 200 if result.get("ok") else 503 if not w3m_installed() else 400


@spa_api_bp.route("/api/v1/terminal/exec", methods=["POST"])
def api_v1_cardputer_terminal_exec() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    payload = request.get_json(silent=True) or {}
    cmd = str(payload.get("cmd", "")).strip()
    if not cmd:
        return jsonify({"ok": False, "error": "no command"})
    result = run_command(cmd, timeout_sec=30)
    combined = (result.stdout + result.stderr).rstrip()
    return jsonify(
        {
            "ok": result.exit_code == 0,
            "lines": _cardputer_output_lines(combined),
            "exit_code": result.exit_code,
            "error": "" if result.exit_code == 0 else (combined or "command failed"),
        }
    )


@spa_api_bp.route("/api/v1/terminal/live/poll")
def api_v1_cardputer_terminal_live_poll() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    raw_cursor = request.args.get("cursor", "0").strip()
    cursor = int(raw_cursor) if raw_cursor.isdigit() else 0
    snapshot = terminal_manager.read("cardputer", cursor=cursor)
    return jsonify(
        {
            "ok": True,
            "output": snapshot.output,
            "lines": _cardputer_output_lines(snapshot.output),
            "cursor": snapshot.cursor,
            "alive": snapshot.alive,
            "reset": snapshot.reset,
        }
    )


@spa_api_bp.route("/api/v1/terminal/live/input", methods=["POST"])
def api_v1_cardputer_terminal_live_input() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    payload = request.get_json(silent=True) or {}
    data = str(payload.get("data", ""))
    if len(data) > 2048:
        data = data[:2048]
    return jsonify({"ok": terminal_manager.write("cardputer", data)})


@spa_api_bp.route("/api/v1/terminal/live/reset", methods=["POST"])
def api_v1_cardputer_terminal_live_reset() -> Response:
    if auth_response := _cardputer_require_auth():
        return auth_response
    terminal_manager.reset("cardputer")
    log_event("cardputer_terminal_reset", None, "cardputer", "Reset Cardputer terminal session")
    return jsonify({"ok": True})
