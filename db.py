"""SQLite access layer: connection handling, versioned migrations, demo seeding."""
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash

def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

DEFAULT_SETTINGS = {
    "store_name": "Inventra Retail",
    "currency_symbol": "\u20b9",
    "tax_rate": "5",
    "max_cashier_discount_pct": "10",
    "receipt_footer": "Thank you for shopping with us!",
}


def run_script(conn, script):
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def m1_base(conn):
    run_script(conn,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            icon TEXT DEFAULT 'fa-box'
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_amount REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def m2_pos_upgrade(conn):
    conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'cashier'")
    conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
    conn.execute("UPDATE users SET role='admin', created_at=CURRENT_TIMESTAMP")

    run_script(conn,
        """
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        CREATE TABLE products_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT,
            name TEXT NOT NULL,
            category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
            cost_cents INTEGER NOT NULL DEFAULT 0 CHECK (cost_cents >= 0),
            stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
            reorder_level INTEGER NOT NULL DEFAULT 10 CHECK (reorder_level >= 0),
            icon TEXT NOT NULL DEFAULT 'fa-box',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    merged = {}
    for r in conn.execute("SELECT id, name, price, stock, icon FROM products ORDER BY id"):
        cents = int(round(float(r["price"]) * 100))
        key = ((r["name"] or "").strip().lower(), cents)
        if key in merged:
            merged[key]["stock"] += max(int(r["stock"] or 0), 0)
        else:
            merged[key] = {"id": r["id"], "name": (r["name"] or "").strip(), "cents": cents,
                           "stock": max(int(r["stock"] or 0), 0), "icon": r["icon"] or "fa-box"}
    for p in merged.values():
        icon = p["icon"] if p["icon"].startswith("fa-") else "fa-" + p["icon"]
        conn.execute(
            "INSERT INTO products_new (id, sku, name, price_cents, stock, icon) VALUES (?,?,?,?,?,?)",
            (p["id"], f"SKU-{p['id']:05d}", p["name"], p["cents"], p["stock"], icon),
        )
    run_script(conn, "DROP TABLE products; ALTER TABLE products_new RENAME TO products;")
    run_script(conn,
        """
        CREATE UNIQUE INDEX idx_products_sku ON products(sku);
        CREATE INDEX idx_products_category ON products(category_id);

        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN
                ('initial','sale','refund','restock','adjustment','damage')),
            note TEXT,
            sale_id INTEGER,
            user_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_movements_product ON stock_movements(product_id, id DESC);
        """
    )
    conn.execute(
        "INSERT INTO stock_movements (product_id, delta, reason, note) "
        "SELECT id, stock, 'initial', 'Opening balance' FROM products WHERE stock > 0"
    )

    conn.execute(
        """
        CREATE TABLE sales_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_no TEXT UNIQUE,
            idempotency_key TEXT UNIQUE,
            cashier_id INTEGER REFERENCES users(id),
            customer_name TEXT,
            customer_phone TEXT,
            subtotal_cents INTEGER NOT NULL,
            discount_cents INTEGER NOT NULL DEFAULT 0,
            tax_cents INTEGER NOT NULL DEFAULT 0,
            total_cents INTEGER NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'cash'
                CHECK (payment_method IN ('cash','card','upi')),
            tendered_cents INTEGER NOT NULL DEFAULT 0,
            change_cents INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK (status IN ('completed','partially_refunded','refunded')),
            refunded_cents INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for r in conn.execute("SELECT id, total_amount, created_at FROM sales ORDER BY id").fetchall():
        cents = int(round(float(r["total_amount"]) * 100))
        conn.execute(
            "INSERT INTO sales_new (id, receipt_no, subtotal_cents, total_cents, tendered_cents, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (r["id"], f"LEG-{r['id']:06d}", cents, cents, cents, r["created_at"] or "1970-01-01 00:00:00"),
        )
    run_script(conn, "DROP TABLE sales; ALTER TABLE sales_new RENAME TO sales;")
    run_script(conn,
        """
        CREATE INDEX idx_sales_created ON sales(created_at);
        CREATE INDEX idx_sales_cashier ON sales(cashier_id, created_at);

        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            sku TEXT,
            name TEXT NOT NULL,
            unit_price_cents INTEGER NOT NULL,
            unit_cost_cents INTEGER NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL CHECK (qty > 0),
            refunded_qty INTEGER NOT NULL DEFAULT 0,
            CHECK (refunded_qty >= 0 AND refunded_qty <= qty)
        );
        CREATE INDEX idx_sale_items_sale ON sale_items(sale_id);
        CREATE INDEX idx_sale_items_product ON sale_items(product_id);

        CREATE TABLE refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            amount_cents INTEGER NOT NULL,
            reason TEXT,
            restocked INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE refund_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_id INTEGER NOT NULL REFERENCES refunds(id) ON DELETE CASCADE,
            sale_item_id INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
            qty INTEGER NOT NULL CHECK (qty > 0)
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            ip TEXT,
            success INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_login_attempts ON login_attempts(username, created_at);
        """
    )
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


MIGRATIONS = [m1_base, m2_pos_upgrade]


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, fn in enumerate(MIGRATIONS, start=1):
        if number <= version:
            continue
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version={number}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

def ensure_admin(conn, username, password):
    if conn.execute("SELECT 1 FROM users WHERE role='admin' AND is_active=1").fetchone():
        return
    existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        conn.execute("UPDATE users SET role='admin', is_active=1 WHERE id=?", (existing["id"],))
    else:
        conn.execute(
            "INSERT INTO users (username, password, role, created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (username, generate_password_hash(password), "admin"),
        )


def init_db(app):
    conn = connect(app.config["DATABASE"])
    try:
        migrate(conn)
        ensure_admin(conn, app.config["ADMIN_USERNAME"], app.config["ADMIN_PASSWORD"])
        if app.config["DEMO_MODE"]:
            seed_demo(conn)
    finally:
        conn.close()

DEMO_PRODUCTS = [
    # name, category, price, cost, stock, reorder, icon  (prices in rupees)
    ("iPhone 15", "Phones", 69900.00, 61500.00, 14, 5, "fa-mobile-screen"),
    ("Samsung Galaxy S24", "Phones", 74999.00, 65000.00, 11, 5, "fa-mobile-screen"),
    ("MacBook Air M2", "Laptops", 94900.00, 82000.00, 9, 4, "fa-laptop"),
    ("Dell Inspiron 15", "Laptops", 52990.00, 45500.00, 6, 3, "fa-laptop"),
    ("LG 27\" 4K Monitor", "Displays", 24990.00, 20000.00, 12, 4, "fa-tv"),
    ("Logitech Wireless Mouse", "Accessories", 699.00, 420.00, 60, 15, "fa-computer-mouse"),
    ("Mechanical Gaming Keyboard", "Accessories", 2999.00, 1900.00, 40, 10, "fa-keyboard"),
    ("USB-C Fast Charging Cable", "Accessories", 399.00, 150.00, 120, 30, "fa-plug"),
    ("Noise-Cancelling Headphones", "Audio", 7999.00, 5600.00, 18, 6, "fa-headphones"),
    ("Bluetooth Speaker", "Audio", 2499.00, 1500.00, 25, 8, "fa-volume-high"),
    ("Cotton T-Shirt", "Apparel", 499.00, 200.00, 90, 25, "fa-shirt"),
    ("Insulated Steel Bottle", "Home", 599.00, 250.00, 45, 15, "fa-mug-hot"),
]


def seed_demo(conn, days=30, seed=2026):
    from services import adjust_stock, create_product, create_sale, refund_sale

    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0:
        return
    rng = random.Random(seed)
    if not conn.execute("SELECT 1 FROM users WHERE username='cashier'").fetchone():
        conn.execute(
            "INSERT INTO users (username, password, role, created_at) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            ("cashier", generate_password_hash("cashier123"), "cashier"),
        )
    admin = dict(conn.execute("SELECT id, username, role FROM users WHERE role='admin' LIMIT 1").fetchone())
    cashier = dict(conn.execute("SELECT id, username, role FROM users WHERE username='cashier'").fetchone())

    ids = []
    for name, cat, price, cost, stock, reorder, icon in DEMO_PRODUCTS:
        ids.append(create_product(conn, admin, {
            "name": name, "category": cat, "price": price, "cost": cost,
            "stock": stock + 40, "reorder_level": reorder, "icon": icon}))
    now = datetime.now(timezone.utc)
    sold_refundable = []
    for day in range(days, -1, -1):
        for _ in range(rng.randint(1, 3) + (2 if day % 7 in (5, 6) else 0)):
            when = now - timedelta(days=day, hours=rng.randint(0, 9), minutes=rng.randint(0, 59))
            if when > now:
                when = now - timedelta(minutes=rng.randint(1, 30))
            items = [{"id": pid, "qty": rng.choice([1, 1, 1, 2, 3])}
                     for pid in rng.sample(ids, rng.randint(1, 3))]
            discount = {"type": "percent", "value": rng.choice([5, 10])} if rng.random() < 0.15 else None
            method = rng.choices(["cash", "card", "upi"], weights=[4, 4, 2])[0]
            try:
                sale_id, _ = create_sale(
                    conn, cashier if rng.random() < 0.6 else admin, items, discount=discount,
                    payment={"method": method},
                    customer={"name": rng.choice(["", "", "Walk-in", "Asha Rao", "Mohan K", "Priya S"])},
                    created_at=when.strftime("%Y-%m-%d %H:%M:%S"))
                sold_refundable.append(sale_id)
            except Exception:
                continue
    for sale_id in rng.sample(sold_refundable, min(3, len(sold_refundable))):
        first = conn.execute("SELECT id FROM sale_items WHERE sale_id=? LIMIT 1", (sale_id,)).fetchone()
        refund_sale(conn, admin, sale_id, [{"sale_item_id": first["id"], "qty": 1}], "Customer return", True)
    for pid, target in ((ids[3], 2), (ids[8], 5), (ids[5], 12)):
        cur = conn.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()["stock"]
        if cur > target:
            adjust_stock(conn, admin, pid, target - cur, "adjustment", "Cycle count correction")


@click.command("seed-demo")
def seed_demo_command():
    """Seed demo products/sales into an empty database."""
    seed_demo(connect(current_app.config["DATABASE"]))
    click.echo("Demo data ready.")
