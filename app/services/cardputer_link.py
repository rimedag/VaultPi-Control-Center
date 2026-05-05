from __future__ import annotations

import os
import socket
from typing import Any

from ..db import setting
from .metrics import system_metrics


def _setting_port(key: str, default: int) -> int:
    raw = setting(key, str(default)).strip()
    return int(raw) if raw.isdigit() else default


def cardputer_host_override() -> str:
    return setting("cardputer_host", os.getenv("CARDPUTER_HOST", "")).strip()


def cardputer_api_port() -> int:
    return _setting_port("cardputer_api_port", 8001)


def cardputer_browser_port() -> int:
    raw = setting("bind_port", os.getenv("CC_PORT", "8000")).strip()
    return int(raw) if raw.isdigit() else 8000


def cardputer_password() -> str:
    return setting("cardputer_password", os.getenv("CARDPUTER_PASSWORD", "password")).strip() or "password"


def cardputer_effective_hosts() -> list[str]:
    override = cardputer_host_override()
    hosts: list[str] = []
    if override:
        hosts.append(override)

    metrics = system_metrics()
    for ip in metrics.get("ip_addresses", []) or []:
        text = str(ip).strip()
        if text and text not in hosts:
            hosts.append(text)

    hostname = setting("hostname_label", "").strip() or socket.gethostname()
    if hostname and hostname not in hosts:
        hosts.append(hostname)

    if "127.0.0.1" not in hosts:
        hosts.append("127.0.0.1")
    return hosts


def cardputer_effective_host() -> str:
    hosts = cardputer_effective_hosts()
    return hosts[0] if hosts else "127.0.0.1"


def cardputer_api_url() -> str:
    return f"http://{cardputer_effective_host()}:{cardputer_api_port()}"


def cardputer_browser_url() -> str:
    return f"http://{cardputer_effective_host()}:{cardputer_browser_port()}/cardputer"


def cardputer_payload(include_password: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hostOverride": cardputer_host_override(),
        "effectiveHost": cardputer_effective_host(),
        "candidateHosts": cardputer_effective_hosts(),
        "apiPort": cardputer_api_port(),
        "browserPort": cardputer_browser_port(),
        "apiUrl": cardputer_api_url(),
        "browserUrl": cardputer_browser_url(),
        "hostname": setting("hostname_label", "").strip() or socket.gethostname(),
        "app": setting("app_name", "VaultPi Control Center"),
    }
    if include_password:
        payload["password"] = cardputer_password()
    return payload
