from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db, setting
from .frontend import frontend_available, serve_frontend_index
from .services.activity import log_event

auth_bp = Blueprint("auth", __name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@auth_bp.before_app_request
def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return
    g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str:
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password", "error")
            log_event("login_failure", None, username or "anonymous", "Failed login attempt")
        else:
            session.clear()
            session["user_id"] = user["id"]
            log_event("login_success", None, username, "User logged in")
            return redirect(url_for("main.dashboard"))

    if frontend_available():
        return serve_frontend_index()
    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout() -> Any:
    actor = g.user["username"] if g.user else "anonymous"
    session.clear()
    log_event("logout", None, actor, "User logged out")
    return redirect(url_for("auth.login"))


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped_view(**kwargs: Any) -> Any:
        if setting("auth_enabled", "1") != "1":
            return view(**kwargs)
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login"))
        return view(**kwargs)

    return wrapped_view