from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .auth import auth_bp
from .bridge_routes import bridge_bp
from .db import init_app as init_db_app
from .db import init_db
from .frontend import frontend_bp
from .routes import main_bp
from .spa_routes import spa_api_bp
from .services.monitor import HealthChecker


def create_app(test_config: dict | None = None) -> Flask:
    cc_port = os.getenv("CC_PORT", "8000").strip() or "8000"

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("VAULTPI_SECRET_KEY", "change-me-in-production"),
        BRIDGE_PSK=os.getenv("BRIDGE_PSK", "").strip(),
        CC_PORT=int(cc_port) if cc_port.isdigit() else 8000,
        DATABASE=str(Path(app.instance_path) / "vaultpi.db"),
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    init_db_app(app)

    app.register_blueprint(frontend_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(bridge_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(spa_api_bp)

    with app.app_context():
        init_db()
        monitor_interval = 60
        try:
            from .db import setting

            monitor_interval = int(setting("monitor_interval", "60") or "60")
        except Exception:
            monitor_interval = 60

    should_start_checker = not app.config.get("TESTING", False)
    if should_start_checker and monitor_interval > 0:
        is_reloader_main = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
        if not app.debug or is_reloader_main:
            checker = HealthChecker(app)
            checker.start()
            app.extensions["health_checker"] = checker

    return app
