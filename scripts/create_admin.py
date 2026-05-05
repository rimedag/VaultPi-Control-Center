from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file(PROJECT_ROOT / ".env")

from app import create_app
from app.db import ensure_admin_user


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update the VaultPi admin user")
    parser.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "").strip(), help="Admin username")
    parser.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", "").strip(), help="Admin password")
    parser.add_argument(
        "--allow-weak-password",
        action="store_true",
        help="Allow short passwords for first-boot/default local installs",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        username = args.username or input("Admin username [admin]: ").strip() or "admin"
        password = args.password or getpass.getpass("Admin password: ").strip()
        if not password:
            password = "admin" if args.allow_weak_password else ""
        if not password:
            raise SystemExit("Password is required")
        if len(password) < 8 and not args.allow_weak_password:
            raise SystemExit("Password must be at least 8 characters")
        ensure_admin_user(username, password)
        print(f"Admin user '{username}' created/updated.")


if __name__ == "__main__":
    main()
