from __future__ import annotations

import shutil
import subprocess
from typing import Any


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def apply_network_add(ssid: str, psk: str, priority: int = 10) -> tuple[bool, str]:
    """Create or replace a Wi-Fi autoconnect profile in NetworkManager."""
    if not nmcli_available():
        return False, "nmcli not available - is NetworkManager installed?"
    # Remove existing profile silently so we can update the password
    _run(["sudo", "nmcli", "connection", "delete", ssid])
    rc, _, err = _run([
        "sudo", "nmcli", "connection", "add",
        "type", "wifi",
        "con-name", ssid,
        "ssid", ssid,
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", psk,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", str(priority),
    ])
    if rc != 0:
        return False, err or "nmcli add failed"
    return True, f"'{ssid}' profile saved to NetworkManager"


def apply_network_remove(ssid: str) -> tuple[bool, str]:
    """Remove a Wi-Fi profile from NetworkManager."""
    if not nmcli_available():
        return False, "nmcli not available"
    rc, _, err = _run(["sudo", "nmcli", "connection", "delete", ssid])
    if rc != 0:
        return False, err or "nmcli delete failed"
    return True, f"'{ssid}' removed from NetworkManager"


def scan_nearby(timeout: int = 4) -> list[str]:
    """Return visible SSIDs. Best-effort; returns empty list on any failure."""
    if not nmcli_available():
        return []
    rc, out, _ = _run(
        ["nmcli", "--terse", "--fields", "SSID", "device", "wifi", "list"],
        timeout=timeout,
    )
    if rc != 0:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for line in out.splitlines():
        ssid = line.strip()
        if ssid and ssid not in seen:
            seen.add(ssid)
            result.append(ssid)
    return result


def tailscale_status() -> dict[str, Any]:
    """Return Tailscale connectivity info."""
    if not shutil.which("tailscale"):
        return {"available": False, "connected": False, "ip": "", "hostname": ""}
    rc, ip, _ = _run(["tailscale", "ip", "--4"], timeout=3)
    ip = ip.strip() if rc == 0 else ""
    connected = bool(ip)
    hostname = ""
    if connected:
        rc2, out2, _ = _run(["tailscale", "status", "--peers=false"], timeout=3)
        for line in out2.splitlines():
            if ip in line:
                parts = line.split()
                if len(parts) >= 2:
                    hostname = parts[1]
                break
    return {"available": True, "connected": connected, "ip": ip, "hostname": hostname}
