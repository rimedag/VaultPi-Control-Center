from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .db import get_db, setting
from .services.metrics import system_metrics
from .services.wifi import tailscale_status

bridge_bp = Blueprint("bridge", __name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provided_psk() -> str:
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.headers.get("X-Bridge-PSK", "").strip()
        or request.args.get("psk", "").strip()
    )


def _authorized() -> bool:
    expected = str(current_app.config.get("BRIDGE_PSK", "")).strip()
    provided = _provided_psk()
    return bool(expected and provided and provided == expected)


def _service_counts() -> dict[str, int]:
    db = get_db()
    rows = db.execute(
        """
        SELECT p.id, p.type, sc.status
        FROM projects p
        LEFT JOIN (
            SELECT project_id, status
            FROM service_checks
            WHERE checked_at IN (
                SELECT MAX(checked_at)
                FROM service_checks
                GROUP BY project_id
            )
        ) sc ON sc.project_id = p.id
        WHERE p.enabled = 1 AND p.archived = 0
        """
    ).fetchall()

    local_total = 0
    local_up = 0
    remote_total = 0
    remote_up = 0
    for row in rows:
        project_type = row["type"] or ""
        is_up = row["status"] == "up"
        if project_type in {"local app", "tool", "service"}:
            local_total += 1
            local_up += 1 if is_up else 0
        if project_type == "remote app":
            remote_total += 1
            remote_up += 1 if is_up else 0

    return {
        "local_total": local_total,
        "local_up": local_up,
        "local_down": max(0, local_total - local_up),
        "remote_total": remote_total,
        "remote_up": remote_up,
        "remote_down": max(0, remote_total - remote_up),
    }


def _unauthorized_response() -> tuple[Any, int]:
    return jsonify({"ok": False, "error": "Unauthorized bridge request"}), 401


@bridge_bp.route("/api/bridge/ping")
def bridge_ping() -> Any:
    if not _authorized():
        return _unauthorized_response()
    return jsonify({
        "ok": True,
        "app": setting("app_name", "VaultPi Control Center"),
        "hostname": socket.gethostname(),
        "time": _utc_now(),
    })


@bridge_bp.route("/api/bridge/status")
def bridge_status() -> Any:
    if not _authorized():
        return _unauthorized_response()
    metrics = system_metrics()
    return jsonify({
        "ok": True,
        "app": setting("app_name", "VaultPi Control Center"),
        "hostname": setting("hostname_label", "").strip() or metrics.get("hostname", socket.gethostname()),
        "time": _utc_now(),
        "metrics": {
            "uptime": metrics.get("uptime", "n/a"),
            "cpu_percent": metrics.get("cpu_percent", 0),
            "ram_percent": metrics.get("ram_percent", 0),
            "disk_percent": metrics.get("disk_percent", 0),
            "temperature_c": metrics.get("temperature_c"),
            "ip_addresses": metrics.get("ip_addresses", []),
        },
        "services": _service_counts(),
        "tailscale": tailscale_status(),
    })


@bridge_bp.route("/api/bridge/config")
def bridge_config() -> Any:
    if not _authorized():
        return _unauthorized_response()
    return jsonify({
        "ok": True,
        "app": setting("app_name", "VaultPi Control Center"),
        "dashboard_title": setting("dashboard_title", "VaultPi Control Center"),
        "port": setting("bind_port", "8000"),
        "refresh_interval": setting("refresh_interval", "60"),
        "time": _utc_now(),
    })
