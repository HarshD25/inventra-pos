"""Inventra POS - application factory, page routes and error handling."""
import logging
import os
import secrets

from flask import Flask, jsonify, redirect, render_template, request

import auth
import api
import db
from services import get_settings

PAGES = {
    "dashboard": ("dashboard.html", {"admin"}),
    "sales": ("pos.html", None),
    "history": ("history.html", None),
    "products": ("products.html", None),
    "admin": ("admin.html", {"admin"}),
}


def env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_secret_key(instance_path):
    """SECRET_KEY env var wins; otherwise generate once and persist so restarts/workers agree."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(instance_path, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return open(path).read().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "inventra.db")),
        SECRET_KEY=None,
        TOKEN_HOURS=int(os.environ.get("TOKEN_HOURS", "8")),
        COOKIE_SECURE=env_flag("COOKIE_SECURE", "0"),
        DEMO_MODE=env_flag("DEMO_MODE", "1"),
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin123"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = load_secret_key(app.instance_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app.teardown_appcontext(db.close_db)
    app.cli.add_command(db.seed_demo_command)
    db.init_db(app)
    app.register_blueprint(api.bp)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return redirect("/sales.html" if (auth.current_user() or {}).get("role") == "cashier" else "/dashboard.html")

    @app.get("/login")
    @app.get("/login.html")
    def login_page():
        if auth.current_user():
            return redirect("/")
        return render_template("login.html", demo=app.config["DEMO_MODE"])

    @app.get("/healthz")
    def healthz():
        db.get_db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/<page>")
    def serve_page(page):
        slug = page[:-5] if page.endswith(".html") else page
        if slug not in PAGES:
            return render_template("error.html", code=404, message="Page not found"), 404
        template, roles = PAGES[slug]
        user = auth.current_user()
        if not user:
            return redirect("/login.html")
        if roles and user["role"] not in roles:
            return redirect("/sales.html")
        return render_template(template, user=user, active=slug, settings=get_settings(db.get_db()))

    def wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def not_found(_e):
        if wants_json():
            return jsonify(error="Not found"), 404
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return jsonify(error="Method not allowed"), 405

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify(error="Upload too large"), 413

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception("Unhandled error: %s", e)
        if wants_json():
            return jsonify(error="Internal server error"), 500
        return render_template("error.html", code=500, message="Something went wrong"), 500

    return app


app = create_app() if os.environ.get("INVENTRA_EAGER", "1") == "1" else None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=env_flag("FLASK_DEBUG", "0"), port=port)
