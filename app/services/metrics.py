from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime
from functools import lru_cache
from typing import Any

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


def _safe_read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def system_metrics() -> dict[str, Any]:
    hostname = socket.gethostname()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    metrics: dict[str, Any] = {
        "hostname": hostname,
        "current_time": now,
        "uptime": "n/a",
        "cpu_percent": 0,
        "ram_percent": 0,
        "ram_used_mb": 0,
        "ram_total_mb": 0,
        "disk_percent": 0,
        "disk_used_gb": 0,
        "disk_total_gb": 0,
        "temperature_c": None,
        "load_avg": "n/a",
        "ip_addresses": [],
        "platform": platform.platform(),
    }

    if psutil:
        try:
            boot = psutil.boot_time()
            uptime_sec = int(time.time() - boot)
            hours, rem = divmod(uptime_sec, 3600)
            minutes, seconds = divmod(rem, 60)
            metrics["uptime"] = f"{hours}h {minutes}m {seconds}s"

            metrics["cpu_percent"] = psutil.cpu_percent(interval=0.0)
            vm = psutil.virtual_memory()
            metrics["ram_percent"] = vm.percent
            metrics["ram_used_mb"] = int(vm.used / 1024 / 1024)
            metrics["ram_total_mb"] = int(vm.total / 1024 / 1024)

            du = psutil.disk_usage("/")
            metrics["disk_percent"] = du.percent
            metrics["disk_used_gb"] = round(du.used / 1024 / 1024 / 1024, 2)
            metrics["disk_total_gb"] = round(du.total / 1024 / 1024 / 1024, 2)

            load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
            metrics["load_avg"] = f"{load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}"

            addrs = []
            for _, iface_addrs in psutil.net_if_addrs().items():
                for addr in iface_addrs:
                    if getattr(addr, "family", None) == socket.AF_INET and not addr.address.startswith("127."):
                        addrs.append(addr.address)
            metrics["ip_addresses"] = sorted(set(addrs))

            sensors = psutil.sensors_temperatures(fahrenheit=False) if hasattr(psutil, "sensors_temperatures") else {}
            if sensors:
                for readings in sensors.values():
                    if readings:
                        metrics["temperature_c"] = round(readings[0].current, 1)
                        break
        except Exception:
            pass

    if metrics["temperature_c"] is None:
        temp_raw = _safe_read("/sys/class/thermal/thermal_zone0/temp")
        if temp_raw.isdigit():
            metrics["temperature_c"] = round(int(temp_raw) / 1000.0, 1)

    if not metrics["ip_addresses"]:
        try:
            metrics["ip_addresses"] = [socket.gethostbyname(hostname)]
        except Exception:
            metrics["ip_addresses"] = []

    return metrics
