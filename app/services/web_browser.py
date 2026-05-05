from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context

W3M_WIDTH = 96
W3M_TIMEOUT_SECONDS = 25
MAX_LINES = 240
MAX_LINKS = 40

_DEFAULT_BOOKMARKS: list[dict] = [
    {"title": "DuckDuckGo Lite", "url": "https://lite.duckduckgo.com/lite/"},
    {"title": "Hacker News",     "url": "https://news.ycombinator.com/"},
    {"title": "Wikipedia",       "url": "https://en.wikipedia.org/"},
]


def bookmarks_path() -> Path:
    if has_app_context():
        return Path(current_app.instance_path) / "web-browser-bookmarks.json"
    return Path(__file__).resolve().parents[1] / "data" / "bookmarks.json"


def w3m_installed() -> bool:
    return shutil.which("w3m") is not None


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def build_search_url(query: str) -> str:
    encoded = urllib.parse.quote_plus(query.strip())
    return f"https://duckduckgo.com/html/?q={encoded}"


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value for key, value in attrs}
        href = (attrs_dict.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            return
        self._href = href
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        title = " ".join(" ".join(self._text).split())
        url = urllib.parse.urljoin(self.base_url, self._href)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.netloc.endswith("duckduckgo.com") and query.get("uddg"):
            url = query["uddg"][0]
        if title and url.startswith(("http://", "https://")):
            self.links.append({"title": title[:90], "url": url[:240]})
        self._href = None
        self._text = []


def extract_links(url: str, timeout: int = 12) -> list[dict[str, str]]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "VaultPi-Cardputer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return []
            html = response.read(524_288).decode("utf-8", errors="replace")
    except Exception:
        return []
    parser = _LinkParser(url)
    try:
        parser.feed(html)
    except Exception:
        return []
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for link in parser.links:
        link_url = link["url"]
        if link_url in seen:
            continue
        seen.add(link_url)
        links.append(link)
        if len(links) >= MAX_LINKS:
            break
    return links


def w3m_command(url: str) -> str:
    """Return the shell command string to launch w3m for the given URL."""
    return "w3m " + shlex.quote(url)


def load_bookmarks() -> list[dict]:
    path = bookmarks_path()
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                bookmarks = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    url = normalize_url(str(item.get("url", "")))
                    if not url:
                        continue
                    title = str(item.get("title") or url).strip()[:80]
                    bookmarks.append({"title": title, "url": url})
                if bookmarks:
                    return bookmarks
        except (json.JSONDecodeError, OSError):
            pass
    return _DEFAULT_BOOKMARKS.copy()


def ensure_bookmarks() -> list[dict]:
    """Load bookmarks, writing defaults to disk if the file does not exist yet."""
    path = bookmarks_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(_DEFAULT_BOOKMARKS, f, indent=2)
    return load_bookmarks()


def fetch_text(url: str, cols: int = W3M_WIDTH, timeout: int = W3M_TIMEOUT_SECONDS) -> dict[str, Any]:
    url = normalize_url(url)
    if not url:
        return {"ok": False, "error": "URL is required", "url": "", "lines": []}
    if not w3m_installed():
        return {
            "ok": False,
            "error": "w3m is not installed. Install with: sudo apt install w3m",
            "url": url,
            "lines": [],
        }

    try:
        safe_cols = max(40, min(int(cols or W3M_WIDTH), 140))
    except (TypeError, ValueError):
        safe_cols = W3M_WIDTH
    try:
        safe_timeout = max(5, min(int(timeout or W3M_TIMEOUT_SECONDS), 60))
    except (TypeError, ValueError):
        safe_timeout = W3M_TIMEOUT_SECONDS
    try:
        result = subprocess.run(
            ["w3m", "-dump", "-cols", str(safe_cols), url],
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out fetching page", "url": url, "lines": []}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "url": url, "lines": []}

    raw = result.stdout if result.stdout.strip() else result.stderr
    lines = [line.rstrip()[:safe_cols] for line in raw.splitlines()]
    lines = [line for line in lines if line.strip()][:MAX_LINES]
    ok = result.returncode == 0 or bool(lines)
    return {
        "ok": ok,
        "url": url,
        "title": url,
        "exitCode": result.returncode,
        "lines": lines,
        "links": extract_links(url) if ok else [],
        "total": len(lines),
        "error": "" if ok else (result.stderr.strip() or f"w3m exited with {result.returncode}"),
    }
