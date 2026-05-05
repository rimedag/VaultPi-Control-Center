from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class CheckResult:
    status: str
    status_code: int | None
    response_ms: int | None
    error_text: str


def check_http(url: str, timeout: float = 5.0) -> CheckResult:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            elapsed = int((time.perf_counter() - start) * 1000)
            code = response.getcode()
            status = "up" if 200 <= code < 400 else "down"
            return CheckResult(status, code, elapsed, "")
    except urllib.error.HTTPError as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return CheckResult("down", exc.code, elapsed, str(exc))
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return CheckResult("down", None, elapsed, str(exc))


def check_tcp(host: str, port: int, timeout: float = 2.0) -> CheckResult:
    start = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        elapsed = int((time.perf_counter() - start) * 1000)
        return CheckResult("up", None, elapsed, "")
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return CheckResult("down", None, elapsed, str(exc))
    finally:
        sock.close()
