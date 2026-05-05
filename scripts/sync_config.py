from __future__ import annotations

import argparse
import os

from app import create_app
from app.services.config_sync import sync_from_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync VaultPi DB from JSON config")
    parser.add_argument(
        "--path",
        default="config/control_center.json",
        help="Path to config JSON file (default: config/control_center.json)",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        root = os.path.abspath(os.path.join(app.root_path, ".."))
        cfg_path = args.path if os.path.isabs(args.path) else os.path.join(root, args.path)
        stats = sync_from_config(cfg_path)
        print(f"Synced from: {cfg_path}")
        print(stats)


if __name__ == "__main__":
    main()
