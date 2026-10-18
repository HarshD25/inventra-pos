"""Authentication & authorisation: JWT in an HttpOnly cookie, role checks, login throttling."""
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from db import get_db

COOKIE = "token"
MAX_FAILS_PER_USER = 5
MAX_FAILS_PER_IP = 30
LOCK_WINDOW = "-15 minutes"


def issue_token(user):
    hours = current_app.config["TOKEN_HOURS"]
    payload = {"sub": str(user["id"]), "exp": datetime.now(timezone.utc) + timedelta(hours=hours)}
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def set_auth_cookie(resp, user):
    resp.set_cookie(
        COOKIE, issue_token(user), httponly=True, samesite="Lax",
        secure=current_app.config["COOKIE_SECURE"], max_age=current_app.config["TOKEN_HOURS"] * 3600,
    )
    return resp


def current_user():
    if "user" in g:
        return g.user
    g.user = None
    token = request.cookies.get(COOKIE)
    if token:
        try:
            data = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
            row = get_db().execute(
                "SELECT id, username, role FROM users WHERE id=? AND is_active=1", (int(data["sub"]),)
            ).fetchone()
            g.user = dict(row) if row else None
        except (jwt.PyJWTError, KeyError, ValueError):
            g.user = None
    return g.user


def require_auth(*roles):
    """Decorator: 401 if not signed in, 403 if the role is not allowed."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(error="Authentication required"), 401
            if roles and user["role"] not in roles:
                return jsonify(error="You do not have permission to do this"), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def login_blocked(username, ip):
    db = get_db()
    by_user = db.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE username=? AND success=0 AND created_at > datetime('now', ?)",
        (username, LOCK_WINDOW)).fetchone()[0]
    by_ip = db.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip=? AND success=0 AND created_at > datetime('now', ?)",
        (ip, LOCK_WINDOW)).fetchone()[0]
    return by_user >= MAX_FAILS_PER_USER or by_ip >= MAX_FAILS_PER_IP


def record_login(username, ip, success):
    db = get_db()
    db.execute("INSERT INTO login_attempts (username, ip, success) VALUES (?,?,?)", (username, ip, 1 if success else 0))
    if success:
        db.execute("DELETE FROM login_attempts WHERE username=? AND success=0", (username,))
    db.execute("DELETE FROM login_attempts WHERE created_at < datetime('now', '-1 day')")
