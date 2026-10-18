"""JSON API (blueprint mounted at /api). Thin HTTP layer over services.py."""
import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, Response, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

import auth
import services as svc
from auth import require_auth
from db import get_db
from services import ApiError

bp = Blueprint("api", __name__, url_prefix="/api")
ADMIN = "admin"
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

@bp.before_request
def csrf_guard():
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify(error="Missing X-Requested-With header"), 403


@bp.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(error=e.message), e.status


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object")
    return data


def paging(default=25, maximum=500):
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per = min(max(int(request.args.get("per_page", default)), 1), maximum)
    except ValueError:
        raise ApiError("Invalid paging parameters")
    return page, per


def page_meta(total, page, per):
    return {"total": total, "page": page, "per_page": per, "pages": max((total + per - 1) // per, 1)}


def like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def tz_minutes():
    try:
        return max(min(int(request.args.get("tz", 0)), 840), -720)
    except ValueError:
        return 0


def local_bounds(date_from, date_to, tz):
    """Convert local calendar dates (YYYY-MM-DD) to UTC [start, end) timestamps for SQL."""
    out = [None, None]
    try:
        if date_from:
            out[0] = (datetime.strptime(date_from, "%Y-%m-%d") - timedelta(minutes=tz)).strftime("%Y-%m-%d %H:%M:%S")
        if date_to:
            out[1] = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1, minutes=-tz)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ApiError("Dates must be formatted YYYY-MM-DD")
    return out


def iso(ts):
    return ts.replace(" ", "T") + "Z" if ts else ts


def product_json(r, admin):
    d = {"id": r["id"], "sku": r["sku"], "name": r["name"], "category": r["category"], "price_cents": r["price_cents"],
         "stock": r["stock"], "reorder_level": r["reorder_level"], "icon": r["icon"], "is_active": bool(r["is_active"]),
         "low_stock": r["stock"] <= r["reorder_level"]}
    if admin:
        d["cost_cents"] = r["cost_cents"]
    return d


def csv_cell(v):
    """Neutralise spreadsheet formula injection (=, +, -, @) in exported text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v

@bp.post("/login")
def login():
    data = body()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        raise ApiError("Username and password are required")
    ip = request.remote_addr or "?"
    if auth.login_blocked(username, ip):
        return jsonify(error="Too many failed attempts. Try again in 15 minutes."), 429
    row = get_db().execute("SELECT id, username, role, password, is_active FROM users WHERE username=?",
                           (username,)).fetchone()
    ok = bool(row and row["is_active"] and check_password_hash(row["password"], password))
    auth.record_login(username, ip, ok)
    if not ok:
        return jsonify(error="Invalid credentials"), 401
    resp = jsonify(message="Logged in", user={"username": row["username"], "role": row["role"]})
    return auth.set_auth_cookie(resp, row)


@bp.post("/logout")
def logout():
    resp = jsonify(message="Logged out")
    resp.delete_cookie(auth.COOKIE)
    return resp


@bp.get("/me")
@require_auth()
def me():
    user = auth.current_user()
    return jsonify(user=user, settings=svc.get_settings(get_db()))


@bp.post("/me/password")
@require_auth()
def change_password():
    data = body()
    db, user = get_db(), auth.current_user()
    row = db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
    if not check_password_hash(row["password"], str(data.get("current_password", ""))):
        raise ApiError("Current password is incorrect", 403)
    new = str(data.get("new_password", ""))
    if len(new) < MIN_PASSWORD:
        raise ApiError(f"New password must be at least {MIN_PASSWORD} characters")
    db.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new), user["id"]))
    svc.audit(db, user, "user.password_change", "self")
    return jsonify(message="Password updated")

@bp.get("/settings")
@require_auth()
def get_settings():
    return jsonify(svc.get_settings(get_db()))


@bp.put("/settings")
@require_auth(ADMIN)
def put_settings():
    data, db = body(), get_db()
    updates = {}
    if "store_name" in data:
        updates["store_name"] = svc.clean_text(data["store_name"], "Store name", 60, required=True)
    if "currency_symbol" in data:
        updates["currency_symbol"] = svc.clean_text(data["currency_symbol"], "Currency symbol", 4, required=True)
    if "receipt_footer" in data:
        updates["receipt_footer"] = svc.clean_text(data["receipt_footer"], "Receipt footer", 120)
    for key, field in (("tax_rate", "Tax rate"), ("max_cashier_discount_pct", "Cashier discount limit")):
        if key in data:
            try:
                v = Decimal(str(data[key]))
            except Exception:
                raise ApiError(f"Invalid {field}")
            if not v.is_finite() or v < 0 or v > 100:
                raise ApiError(f"{field} must be between 0 and 100")
            updates[key] = str(v.normalize()) if v else "0"
    if not updates:
        raise ApiError("Nothing to update")
    with svc.transaction(db):
        for k, v in updates.items():
            db.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        svc.audit(db, auth.current_user(), "settings.update", ", ".join(sorted(updates)))
    return jsonify(svc.get_settings(db))

@bp.get("/products")
@require_auth()
def list_products():
    db, user = get_db(), auth.current_user()
    is_admin = user["role"] == ADMIN
    page, per = paging()
    where, args = [], []
    status_filter = request.args.get("active", "active") if is_admin else "active"
    if status_filter == "active":
        where.append("p.is_active=1")
    elif status_filter == "inactive":
        where.append("p.is_active=0")
    q = request.args.get("q", "").strip()
    if q:
        where.append("(p.name LIKE ? ESCAPE '\\' OR p.sku LIKE ? ESCAPE '\\')")
        args += [like(q), like(q)]
    cat = request.args.get("category", "").strip()
    if cat:
        where.append("c.name = ?"); args.append(cat)
    stock = request.args.get("stock", "all")
    if stock == "in":
        where.append("p.stock > p.reorder_level")
    elif stock == "low":
        where.append("p.stock > 0 AND p.stock <= p.reorder_level")
    elif stock == "out":
        where.append("p.stock = 0")
    order = {"name": "p.name COLLATE NOCASE ASC", "price": "p.price_cents DESC", "stock": "p.stock ASC",
             "newest": "p.id DESC"}.get(request.args.get("sort", "name"), "p.name COLLATE NOCASE ASC")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM products p LEFT JOIN categories c ON c.id = p.category_id {clause}"
    total = db.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = db.execute(f"SELECT p.*, c.name AS category {base} ORDER BY {order} LIMIT ? OFFSET ?",
                      (*args, per, (page - 1) * per)).fetchall()
    cats = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name COLLATE NOCASE")]
    return jsonify(items=[product_json(r, is_admin) for r in rows], categories=cats, **page_meta(total, page, per))


@bp.post("/products")
@require_auth(ADMIN)
def create_product():
    db = get_db()
    pid = svc.create_product(db, auth.current_user(), body())
    return jsonify(message="Product added successfully", id=pid), 201


@bp.put("/products/<int:pid>")
@require_auth(ADMIN)
def update_product(pid):
    svc.update_product(get_db(), auth.current_user(), pid, body())
    return jsonify(message="Product updated")


@bp.delete("/products/<int:pid>")
@require_auth(ADMIN)
def delete_product(pid):
    """Soft delete: sold products must stay in history, reports and receipts."""
    db = get_db()
    svc.update_product(db, auth.current_user(), pid, {"is_active": False})
    return jsonify(message="Product archived")


@bp.post("/products/<int:pid>/stock")
@require_auth(ADMIN)
def stock_adjust(pid):
    data = body()
    new_stock = svc.adjust_stock(get_db(), auth.current_user(), pid, data.get("delta"), data.get("reason"), data.get("note", ""))
    return jsonify(message="Stock updated", stock=new_stock)


@bp.get("/products/<int:pid>/movements")
@require_auth(ADMIN)
def stock_history(pid):
    rows = get_db().execute(
        "SELECT m.id, m.delta, m.reason, m.note, m.sale_id, m.created_at, u.username AS user "
        "FROM stock_movements m LEFT JOIN users u ON u.id=m.user_id WHERE m.product_id=? ORDER BY m.id DESC LIMIT 50",
        (pid,)).fetchall()
    return jsonify(items=[{**dict(r), "created_at": iso(r["created_at"])} for r in rows])

@bp.post("/checkout")
@require_auth()
def checkout():
    data, db, user = body(), get_db(), auth.current_user()
    sale_id, created = svc.create_sale(
        db, user, data.get("cart"), discount=data.get("discount"), payment=data.get("payment"),
        customer=data.get("customer"), idempotency_key=data.get("idempotency_key"))
    sale = svc.sale_detail(db, sale_id)
    return jsonify(message="Checkout completed successfully!", sale=_sale_json(sale)), (201 if created else 200)


def _sale_json(s):
    s = dict(s)
    s["created_at"] = iso(s["created_at"])
    for r in s.get("refunds", []):
        r["created_at"] = iso(r["created_at"])
    return s


@bp.get("/sales")
@require_auth()
def list_sales():
    db, user = get_db(), auth.current_user()
    page, per = paging()
    where, args = [], []
    if user["role"] != ADMIN:
        where.append("s.cashier_id = ?"); args.append(user["id"])
    q = request.args.get("q", "").strip()
    if q:
        where.append("(s.receipt_no LIKE ? ESCAPE '\\' OR s.customer_name LIKE ? ESCAPE '\\' OR s.customer_phone LIKE ? ESCAPE '\\')")
        args += [like(q)] * 3
    if request.args.get("status") in ("completed", "partially_refunded", "refunded"):
        where.append("s.status = ?"); args.append(request.args["status"])
    if request.args.get("payment") in svc.PAYMENT_METHODS:
        where.append("s.payment_method = ?"); args.append(request.args["payment"])
    start, end = local_bounds(request.args.get("from"), request.args.get("to"), tz_minutes())
    if start:
        where.append("s.created_at >= ?"); args.append(start)
    if end:
        where.append("s.created_at < ?"); args.append(end)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM sales s {clause}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT s.id, s.receipt_no, s.customer_name, s.total_cents, s.refunded_cents, s.payment_method, s.status, "
        f"s.created_at, u.username AS cashier, (SELECT COALESCE(SUM(qty),0) FROM sale_items WHERE sale_id=s.id) AS units "
        f"FROM sales s LEFT JOIN users u ON u.id=s.cashier_id {clause} ORDER BY s.id DESC LIMIT ? OFFSET ?",
        (*args, per, (page - 1) * per)).fetchall()
    return jsonify(items=[{**dict(r), "created_at": iso(r["created_at"])} for r in rows], **page_meta(total, page, per))


@bp.get("/sales/<int:sale_id>")
@require_auth()
def get_sale(sale_id):
    db, user = get_db(), auth.current_user()
    sale = svc.sale_detail(db, sale_id)
    if not sale or (user["role"] != ADMIN and sale["cashier_id"] != user["id"]):
        raise ApiError("Sale not found", 404)
    return jsonify(_sale_json(sale))


@bp.post("/sales/<int:sale_id>/refund")
@require_auth(ADMIN)
def refund(sale_id):
    data, db = body(), get_db()
    refund_id, amount = svc.refund_sale(db, auth.current_user(), sale_id, data.get("items"),
                                        data.get("reason", ""), bool(data.get("restock", True)))
    return jsonify(message="Refund processed", refund_id=refund_id, amount_cents=amount,
                   sale=_sale_json(svc.sale_detail(db, sale_id)))

@bp.get("/stats")
@require_auth(ADMIN)
def stats():
    db, tz = get_db(), tz_minutes()
    try:
        days = min(max(int(request.args.get("days", 7)), 1), 365)
    except ValueError:
        raise ApiError("Invalid days")
    local_today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).strftime("%Y-%m-%d")
    first_day = (datetime.strptime(local_today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    r_start, r_end = local_bounds(first_day, local_today, tz)
    t_start, t_end = local_bounds(local_today, local_today, tz)

    def kpis(start, end):
        row = db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_cents),0) gross, COALESCE(SUM(refunded_cents),0) refunded, "
            "COALESCE(SUM(tax_cents),0) tax, COALESCE(SUM(discount_cents),0) discounts "
            "FROM sales WHERE created_at >= ? AND created_at < ?", (start, end)).fetchone()
        profit = 0.0
        for s in db.execute(
                "SELECT s.subtotal_cents sub, s.discount_cents disc, "
                "SUM(si.unit_price_cents*(si.qty-si.refunded_qty)) net, SUM(si.unit_cost_cents*(si.qty-si.refunded_qty)) cogs "
                "FROM sales s JOIN sale_items si ON si.sale_id=s.id WHERE s.created_at >= ? AND s.created_at < ? GROUP BY s.id",
                (start, end)):
            share = s["disc"] * s["net"] / s["sub"] if s["sub"] else 0
            profit += s["net"] - share - s["cogs"]
        revenue = row["gross"] - row["refunded"]
        return {"orders": row["n"], "revenue_cents": revenue, "refunded_cents": row["refunded"],
                "tax_cents": row["tax"], "discount_cents": row["discounts"], "profit_cents": round(profit),
                "avg_order_cents": round(revenue / row["n"]) if row["n"] else 0}

    modifier = f"{tz:+d} minutes"
    by_day = {r["d"]: r for r in db.execute(
        "SELECT date(created_at, ?) d, COUNT(*) n, SUM(total_cents - refunded_cents) rev FROM sales "
        "WHERE created_at >= ? AND created_at < ? GROUP BY d", (modifier, r_start, r_end))}
    labels, revenue, orders = [], [], []
    for i in range(days):
        d = (datetime.strptime(first_day, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d)
        revenue.append(by_day[d]["rev"] if d in by_day else 0)
        orders.append(by_day[d]["n"] if d in by_day else 0)

    top = db.execute(
        "SELECT si.name, SUM(si.qty - si.refunded_qty) units, SUM((si.qty - si.refunded_qty) * si.unit_price_cents) revenue_cents "
        "FROM sale_items si JOIN sales s ON s.id = si.sale_id WHERE s.created_at >= ? AND s.created_at < ? "
        "GROUP BY COALESCE(si.product_id, si.name) HAVING units > 0 ORDER BY units DESC, revenue_cents DESC LIMIT 5",
        (r_start, r_end)).fetchall()
    payments = db.execute(
        "SELECT payment_method method, COUNT(*) n, SUM(total_cents - refunded_cents) revenue_cents FROM sales "
        "WHERE created_at >= ? AND created_at < ? GROUP BY payment_method", (r_start, r_end)).fetchall()
    low = db.execute(
        "SELECT id, sku, name, stock, reorder_level FROM products WHERE is_active=1 AND stock <= reorder_level "
        "ORDER BY stock ASC, name LIMIT 8").fetchall()
    low_count = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1 AND stock <= reorder_level").fetchone()[0]
    inv = db.execute("SELECT COUNT(*) n, COALESCE(SUM(stock*cost_cents),0) cost, COALESCE(SUM(stock*price_cents),0) retail "
                     "FROM products WHERE is_active=1").fetchone()
    recent = db.execute(
        "SELECT s.id, s.receipt_no, s.total_cents, s.payment_method, s.status, s.created_at, u.username cashier "
        "FROM sales s LEFT JOIN users u ON u.id=s.cashier_id ORDER BY s.id DESC LIMIT 6").fetchall()
    return jsonify(
        today=kpis(t_start, t_end), range=kpis(r_start, r_end), days=days,
        chart={"labels": labels, "revenue_cents": revenue, "orders": orders},
        top_products=[dict(r) for r in top], payments=[dict(r) for r in payments],
        low_stock=[dict(r) for r in low], low_stock_count=low_count,
        inventory={"products": inv["n"], "cost_value_cents": inv["cost"], "retail_value_cents": inv["retail"]},
        recent_sales=[{**dict(r), "created_at": iso(r["created_at"])} for r in recent])

def _user_json(r):
    return {"id": r["id"], "username": r["username"], "role": r["role"], "is_active": bool(r["is_active"]),
            "created_at": iso(r["created_at"])}


def _active_admin_count(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]


@bp.get("/users")
@require_auth(ADMIN)
def list_users():
    rows = get_db().execute("SELECT id, username, role, is_active, created_at FROM users ORDER BY id").fetchall()
    return jsonify(items=[_user_json(r) for r in rows])


@bp.post("/users")
@require_auth(ADMIN)
def create_user():
    data, db = body(), get_db()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = data.get("role", "cashier")
    if not USERNAME_RE.match(username):
        raise ApiError("Username must be 3-32 characters (letters, digits, . _ -)")
    if len(password) < MIN_PASSWORD:
        raise ApiError(f"Password must be at least {MIN_PASSWORD} characters")
    if role not in ("admin", "cashier"):
        raise ApiError("Role must be admin or cashier")
    try:
        with svc.transaction(db):
            uid = db.execute("INSERT INTO users (username, password, role, created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
                             (username, generate_password_hash(password), role)).lastrowid
            svc.audit(db, auth.current_user(), "user.create", f"{username} ({role})")
    except sqlite3.IntegrityError:
        raise ApiError("That username is already taken", 409)
    return jsonify(message="User created", id=uid), 201


@bp.put("/users/<int:uid>")
@require_auth(ADMIN)
def update_user(uid):
    data, db, me_ = body(), get_db(), auth.current_user()
    target = db.execute("SELECT id, username, role, is_active FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        raise ApiError("User not found", 404)
    role = data.get("role", target["role"])
    active = bool(data.get("is_active", target["is_active"]))
    if role not in ("admin", "cashier"):
        raise ApiError("Role must be admin or cashier")
    losing_admin = target["role"] == "admin" and target["is_active"] and (role != "admin" or not active)
    if losing_admin and (uid == me_["id"] or _active_admin_count(db) <= 1):
        raise ApiError("You cannot demote/deactivate yourself or the last active admin", 409)
    with svc.transaction(db):
        db.execute("UPDATE users SET role=?, is_active=? WHERE id=?", (role, 1 if active else 0, uid))
        if data.get("password"):
            if len(str(data["password"])) < MIN_PASSWORD:
                raise ApiError(f"Password must be at least {MIN_PASSWORD} characters")
            db.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(str(data["password"])), uid))
        svc.audit(db, me_, "user.update", f"{target['username']} role={role} active={int(active)}"
                  + (" password-reset" if data.get("password") else ""))
    return jsonify(message="User updated")


@bp.get("/audit")
@require_auth(ADMIN)
def audit_log():
    rows = get_db().execute("SELECT id, username, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify(items=[{**dict(r), "created_at": iso(r["created_at"])} for r in rows])

@bp.get("/export/<report>")
@require_auth(ADMIN)
def export_csv(report):
    db = get_db()
    start, end = local_bounds(request.args.get("from"), request.args.get("to"), tz_minutes())
    rng, rargs = "", []
    if start:
        rng += " AND s.created_at >= ?"; rargs.append(start)
    if end:
        rng += " AND s.created_at < ?"; rargs.append(end)
    out = io.StringIO()
    w = csv.writer(out)
    money = lambda c: f"{c / 100:.2f}"
    if report == "sales":
        w.writerow(["Receipt", "Date (UTC)", "Cashier", "Customer", "Payment", "Subtotal", "Discount", "Tax", "Total", "Refunded", "Status"])
        for r in db.execute("SELECT s.*, u.username cashier FROM sales s LEFT JOIN users u ON u.id=s.cashier_id "
                            f"WHERE 1=1 {rng} ORDER BY s.id DESC", rargs):
            w.writerow([r["receipt_no"], r["created_at"], r["cashier"] or "", csv_cell(r["customer_name"] or ""), r["payment_method"],
                        money(r["subtotal_cents"]), money(r["discount_cents"]), money(r["tax_cents"]), money(r["total_cents"]),
                        money(r["refunded_cents"]), r["status"]])
    elif report == "inventory":
        w.writerow(["SKU", "Name", "Category", "Price", "Cost", "Stock", "Reorder level", "Active"])
        for r in db.execute("SELECT p.*, c.name category FROM products p LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.name COLLATE NOCASE"):
            w.writerow([r["sku"], csv_cell(r["name"]), csv_cell(r["category"] or ""), money(r["price_cents"]),
                        money(r["cost_cents"]), r["stock"], r["reorder_level"], "yes" if r["is_active"] else "no"])
    else:
        raise ApiError("Unknown report. Use sales or inventory", 404)
    name = f"inventra_{report}_{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})
