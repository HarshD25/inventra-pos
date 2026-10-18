import re
import sqlite3
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAYMENT_METHODS = ("cash", "card", "upi")
MANUAL_STOCK_REASONS = ("restock", "adjustment", "damage")
ICON_RE = re.compile(r"^fa-[a-z0-9-]{1,40}$")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{0,20}$")
MAX_PRICE_CENTS = 100_000_000  # 1,000,000.00


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def to_cents(value, field="amount"):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ApiError(f"{field} is required")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ApiError(f"Invalid {field}")
    if not d.is_finite() or d < 0:
        raise ApiError(f"{field} must be zero or greater")
    cents = int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if cents > MAX_PRICE_CENTS:
        raise ApiError(f"{field} is too large")
    return cents


def to_int(value, field, minimum=0, maximum=1_000_000):
    if isinstance(value, bool):
        raise ApiError(f"Invalid {field}")
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        n = int(str(value).strip()) if not isinstance(value, int) else value
    except (ValueError, TypeError):
        raise ApiError(f"{field} must be a whole number")
    if n < minimum or n > maximum:
        raise ApiError(f"{field} must be between {minimum} and {maximum}")
    return n


def div_round(n, d):
    """Integer round-half-up of n/d for n >= 0, d > 0."""
    return (2 * n + d) // (2 * d)


def clean_text(value, field, max_len, required=False):
    s = " ".join(str(value or "").split())
    if required and not s:
        raise ApiError(f"{field} is required")
    if len(s) > max_len:
        raise ApiError(f"{field} must be at most {max_len} characters")
    return s


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def audit(conn, user, action, detail=""):
    conn.execute(
        "INSERT INTO audit_log (user_id, username, action, detail) VALUES (?,?,?,?)",
        (user.get("id") if user else None, user.get("username") if user else None, action, detail[:500]),
    )


def get_or_create_category(conn, name):
    name = clean_text(name, "Category", 40)
    if not name:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO categories (name) VALUES (?)", (name,)).lastrowid


def _movement(conn, product_id, delta, reason, user, note=None, sale_id=None):
    conn.execute(
        "INSERT INTO stock_movements (product_id, delta, reason, note, sale_id, user_id) VALUES (?,?,?,?,?,?)",
        (product_id, delta, reason, note, sale_id, user.get("id") if user else None),
    )

def _normalise_sku(value):
    sku = clean_text(value, "SKU", 40).upper().replace(" ", "-")
    return sku or None


def _icon(value):
    icon = str(value or "fa-box").strip()
    if icon and not icon.startswith("fa-"):
        icon = "fa-" + icon
    return icon if ICON_RE.match(icon) else "fa-box"


def create_product(conn, user, data):
    with transaction(conn):
        pid = _create_in_txn(conn, user, data)
        audit(conn, user, "product.create", f"#{pid} {clean_text(data.get('name'), 'Name', 120)}")
    return pid


def update_product(conn, user, pid, data):
    with transaction(conn):
        _update_in_txn(conn, user, pid, data)
        audit(conn, user, "product.update", f"#{pid} {', '.join(sorted(data))}")


def adjust_stock(conn, user, pid, delta, reason, note=""):
    delta = to_int(delta, "Quantity change", -1_000_000, 1_000_000)
    if delta == 0:
        raise ApiError("Quantity change cannot be zero")
    if reason not in MANUAL_STOCK_REASONS:
        raise ApiError("Reason must be restock, adjustment or damage")
    if reason == "restock" and delta < 0:
        raise ApiError("A restock must add stock")
    if reason == "damage" and delta > 0:
        raise ApiError("Damaged/write-off entries must remove stock")
    note = clean_text(note, "Note", 200)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE products SET stock = stock + ?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND stock + ? >= 0",
            (delta, pid, delta),
        )
        if cur.rowcount == 0:
            exists = conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone()
            raise ApiError("Stock cannot go below zero", 409) if exists else ApiError("Product not found", 404)
        _movement(conn, pid, delta, reason, user, note or None)
        audit(conn, user, "stock." + reason, f"product #{pid} {delta:+d}")
    return conn.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()["stock"]


def _create_in_txn(conn, user, data, note="Opening balance"):
    """Validate + insert a product. The caller owns the transaction."""
    name = clean_text(data.get("name"), "Product name", 120, required=True)
    price = to_cents(data.get("price"), "Price")
    cost = to_cents(data.get("cost") or 0, "Cost")
    stock = to_int(data.get("stock") or 0, "Stock")
    reorder_raw = data.get("reorder_level")
    reorder = to_int(10 if reorder_raw in (None, "") else reorder_raw, "Reorder level")
    sku = _normalise_sku(data.get("sku"))
    try:
        cat_id = get_or_create_category(conn, data.get("category"))
        pid = conn.execute(
            "INSERT INTO products (sku, name, category_id, price_cents, cost_cents, stock, reorder_level, icon)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sku, name, cat_id, price, cost, stock, reorder, _icon(data.get("icon"))),
        ).lastrowid
        if not sku:
            conn.execute("UPDATE products SET sku=? WHERE id=?", (f"SKU-{pid:05d}", pid))
    except sqlite3.IntegrityError:
        raise ApiError("A product with this SKU already exists", 409)
    if stock:
        _movement(conn, pid, stock, "initial", user, note)
    return pid


def _require_sku(value):
    sku = _normalise_sku(value)
    if not sku:
        raise ApiError("SKU cannot be empty")
    return sku


def _update_in_txn(conn, user, pid, data):
    mapping = {
        "name": ("name", lambda v: clean_text(v, "Product name", 120, required=True)),
        "sku": ("sku", _require_sku),
        "price": ("price_cents", lambda v: to_cents(v, "Price")),
        "cost": ("cost_cents", lambda v: to_cents(v or 0, "Cost")),
        "reorder_level": ("reorder_level", lambda v: to_int(v, "Reorder level")),
        "icon": ("icon", _icon),
        "is_active": ("is_active", lambda v: 1 if v else 0),
    }
    sets, args = [], []
    for key, (col, fn) in mapping.items():
        if key in data:
            sets.append(f"{col}=?"); args.append(fn(data[key]))
    if "category" in data:
        sets.append("category_id=?"); args.append(get_or_create_category(conn, data["category"]))
    if not sets:
        raise ApiError("Nothing to update")
    sets.append("updated_at=CURRENT_TIMESTAMP")
    try:
        cur = conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", (*args, pid))
    except sqlite3.IntegrityError:
        raise ApiError("A product with this SKU already exists", 409)
    if cur.rowcount == 0:
        raise ApiError("Product not found", 404)

def _parse_discount(discount, subtotal, user, settings):
    if not discount or not discount.get("value") or str(discount.get("value")).strip() in ("", "0"):
        return 0
    kind = discount.get("type", "percent")
    if kind == "percent":
        try:
            pct = Decimal(str(discount["value"]))
        except InvalidOperation:
            raise ApiError("Invalid discount")
        if not pct.is_finite() or pct < 0 or pct > 100:
            raise ApiError("Discount percent must be between 0 and 100")
        cents = int((Decimal(subtotal) * pct / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    elif kind == "amount":
        cents = to_cents(discount["value"], "Discount")
        if cents > subtotal:
            raise ApiError("Discount cannot exceed the order subtotal")
    else:
        raise ApiError("Discount type must be 'percent' or 'amount'")
    if user.get("role") != "admin" and subtotal > 0:
        limit = Decimal(settings.get("max_cashier_discount_pct", "10"))
        if Decimal(cents) * 100 > limit * subtotal:
            raise ApiError(f"Discounts above {limit.normalize()}% need an admin", 403)
    return cents


def create_sale(conn, user, items, discount=None, payment=None, customer=None,
                idempotency_key=None, created_at=None):
    if not isinstance(items, list) or not items:
        raise ApiError("Cart is empty")
    wanted = {}
    for it in items:
        if not isinstance(it, dict):
            raise ApiError("Invalid cart item")
        pid = to_int(it.get("id", it.get("product_id")), "Product id", 1, 2**31)
        qty = to_int(it.get("qty"), "Quantity", 1, 9999)
        wanted[pid] = wanted.get(pid, 0) + qty
    if len(wanted) > 100:
        raise ApiError("Too many different items in one sale")
    payment = payment or {}
    method = payment.get("method", "cash")
    if method not in PAYMENT_METHODS:
        raise ApiError("Payment method must be cash, card or upi")
    customer = customer or {}
    cust_name = clean_text(customer.get("name"), "Customer name", 80) or None
    cust_phone = clean_text(customer.get("phone"), "Customer phone", 20) or None
    if cust_phone and not PHONE_RE.match(cust_phone):
        raise ApiError("Invalid customer phone number")
    key = clean_text(idempotency_key, "Idempotency key", 80) or None

    with transaction(conn):
        if key:
            prior = conn.execute("SELECT id, cashier_id FROM sales WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                if prior["cashier_id"] != user.get("id"):
                    raise ApiError("Idempotency key already used", 409)
                return prior["id"], False
        settings = get_settings(conn)
        lines, subtotal = [], 0
        for pid in sorted(wanted):
            qty = wanted[pid]
            p = conn.execute(
                "SELECT id, sku, name, price_cents, cost_cents, is_active FROM products WHERE id=?", (pid,)
            ).fetchone()
            if not p:
                raise ApiError(f"Product #{pid} not found", 404)
            if not p["is_active"]:
                raise ApiError(f"{p['name']} is no longer available", 409)
            # Conditional update = oversell protection even without the surrounding lock.
            cur = conn.execute(
                "UPDATE products SET stock = stock - ?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND stock >= ?",
                (qty, pid, qty),
            )
            if cur.rowcount == 0:
                have = conn.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()["stock"]
                raise ApiError(f"Not enough stock for {p['name']} (available: {have})", 409)
            subtotal += p["price_cents"] * qty
            lines.append((p, qty))
        discount_cents = _parse_discount(discount, subtotal, user, settings)
        taxable = subtotal - discount_cents
        rate = Decimal(settings.get("tax_rate", "0"))
        tax_cents = int((Decimal(taxable) * rate / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        total = taxable + tax_cents
        if method == "cash":
            tendered = to_cents(payment["tendered"], "Amount tendered") if payment.get("tendered") not in (None, "") else total
            if tendered < total:
                raise ApiError("Amount tendered is less than the total", 400)
        else:
            tendered = total
        cols = ["cashier_id", "customer_name", "customer_phone", "subtotal_cents", "discount_cents",
                "tax_cents", "total_cents", "payment_method", "tendered_cents", "change_cents", "idempotency_key"]
        vals = [user.get("id"), cust_name, cust_phone, subtotal, discount_cents, tax_cents, total,
                method, tendered, tendered - total, key]
        if created_at:
            cols.append("created_at"); vals.append(created_at)
        sale_id = conn.execute(
            f"INSERT INTO sales ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        ).lastrowid
        conn.execute("UPDATE sales SET receipt_no=? WHERE id=?", (f"INV-{sale_id:06d}", sale_id))
        for p, qty in lines:
            conn.execute(
                "INSERT INTO sale_items (sale_id, product_id, sku, name, unit_price_cents, unit_cost_cents, qty)"
                " VALUES (?,?,?,?,?,?,?)",
                (sale_id, p["id"], p["sku"], p["name"], p["price_cents"], p["cost_cents"], qty),
            )
            _movement(conn, p["id"], -qty, "sale", user, sale_id=sale_id)
    return sale_id, True


def refund_sale(conn, user, sale_id, items, reason="", restock=True):
    """Full or partial refund by line item. Returns (refund_id, amount_cents)."""
    if not isinstance(items, list) or not items:
        raise ApiError("Select at least one item to refund")
    wanted = {}
    for it in items:
        if not isinstance(it, dict):
            raise ApiError("Invalid refund item")
        sid = to_int(it.get("sale_item_id"), "Sale item", 1, 2**31)
        wanted[sid] = wanted.get(sid, 0) + to_int(it.get("qty"), "Refund quantity", 1, 9999)
    reason = clean_text(reason, "Reason", 200)
    with transaction(conn):
        sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not sale:
            raise ApiError("Sale not found", 404)
        lines = {r["id"]: r for r in conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,))}
        if not lines:
            raise ApiError("Legacy sales without line items cannot be refunded", 409)
        amount = 0
        for sid, qty in wanted.items():
            line = lines.get(sid)
            if not line:
                raise ApiError(f"Item #{sid} does not belong to this sale", 400)
            if qty > line["qty"] - line["refunded_qty"]:
                raise ApiError(f"Cannot refund {qty} x {line['name']}: only "
                               f"{line['qty'] - line['refunded_qty']} left to refund", 409)
            if sale["subtotal_cents"] > 0:
                # Each unit refunds its proportional share of discount and tax.
                amount += div_round(qty * line["unit_price_cents"] * sale["total_cents"], sale["subtotal_cents"])
        remaining = sale["total_cents"] - sale["refunded_cents"]
        fully = all(
            (line["refunded_qty"] + wanted.get(sid, 0)) == line["qty"] for sid, line in lines.items()
        )
        amount = remaining if fully else min(amount, remaining)
        refund_id = conn.execute(
            "INSERT INTO refunds (sale_id, amount_cents, reason, restocked, created_by) VALUES (?,?,?,?,?)",
            (sale_id, amount, reason or None, 1 if restock else 0, user.get("id")),
        ).lastrowid
        for sid, qty in wanted.items():
            line = lines[sid]
            conn.execute("UPDATE sale_items SET refunded_qty = refunded_qty + ? WHERE id=?", (qty, sid))
            conn.execute("INSERT INTO refund_items (refund_id, sale_item_id, qty) VALUES (?,?,?)",
                         (refund_id, sid, qty))
            if restock and line["product_id"]:
                conn.execute("UPDATE products SET stock = stock + ?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (qty, line["product_id"]))
                _movement(conn, line["product_id"], qty, "refund", user, reason or None, sale_id)
        conn.execute("UPDATE sales SET refunded_cents = refunded_cents + ?, status=? WHERE id=?",
                     (amount, "refunded" if fully else "partially_refunded", sale_id))
        audit(conn, user, "sale.refund", f"{sale['receipt_no']} amount={amount}")
    return refund_id, amount


def sale_detail(conn, sale_id):
    sale = conn.execute(
        "SELECT s.*, u.username AS cashier FROM sales s LEFT JOIN users u ON u.id = s.cashier_id WHERE s.id=?",
        (sale_id,),
    ).fetchone()
    if not sale:
        return None
    out = dict(sale)
    out.pop("idempotency_key", None)
    out["items"] = [dict(r) for r in conn.execute(
        "SELECT id, product_id, sku, name, unit_price_cents, qty, refunded_qty FROM sale_items "
        "WHERE sale_id=? ORDER BY id", (sale_id,))]
    refunds = []
    for r in conn.execute(
            "SELECT r.*, u.username AS refunded_by FROM refunds r LEFT JOIN users u ON u.id=r.created_by "
            "WHERE r.sale_id=? ORDER BY r.id", (sale_id,)):
        rd = dict(r)
        rd["items"] = [dict(i) for i in conn.execute(
            "SELECT si.name, ri.qty FROM refund_items ri JOIN sale_items si ON si.id=ri.sale_item_id "
            "WHERE ri.refund_id=?", (r["id"],))]
        refunds.append(rd)
    out["refunds"] = refunds
    return out
