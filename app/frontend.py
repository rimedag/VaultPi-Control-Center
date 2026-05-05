from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, send_from_directory

frontend_bp = Blueprint("frontend", __name__)


def frontend_dist_dir() -> Path:
    return Path(current_app.root_path) / "frontend_dist"


def frontend_available() -> bool:
    dist = frontend_dist_dir()
    return dist.exists() and (dist / "index.html").is_file()


def serve_frontend_index():
    dist = frontend_dist_dir()
    index = dist / "index.html"
    if not index.is_file():
        abort(404)
    return send_from_directory(dist, "index.html")


@frontend_bp.route("/assets/<path:filename>")
def frontend_assets(filename: str):
    assets_dir = frontend_dist_dir() / "assets"
    if not assets_dir.is_dir():
        abort(404)
    return send_from_directory(assets_dir, filename)


@frontend_bp.route("/images/<path:filename>")
def frontend_images(filename: str):
    images_dir = frontend_dist_dir() / "images"
    if not images_dir.is_dir():
        abort(404)
    return send_from_directory(images_dir, filename)


@frontend_bp.route("/favicon.svg")
def frontend_favicon():
    dist = frontend_dist_dir()
    if not (dist / "favicon.svg").is_file():
        abort(404)
    return send_from_directory(dist, "favicon.svg")


@frontend_bp.route("/opengraph.jpg")
def frontend_opengraph():
    dist = frontend_dist_dir()
    if not (dist / "opengraph.jpg").is_file():
        abort(404)
    return send_from_directory(dist, "opengraph.jpg")