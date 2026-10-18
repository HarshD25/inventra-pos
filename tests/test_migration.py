import sqlite3

from werkzeug.security import generate_password_hash

from app import create_app
from conftest import Client
from db import connect


def build_legacy(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
        CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL, stock INTEGER NOT NULL, icon TEXT DEFAULT 'fa-box');
        CREATE TABLE sales (id INTEGER PRIMARY KEY AUTOINCREMENT, total_amount REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    c.execute("INSERT INTO users (username, password) VALUES ('admin', ?)", (generate_password_hash("admin123"),))
    c.executemany("INSERT INTO products (name, price, stock, icon) VALUES (?,?,?,?)", [
        ("Logitech Mouse", 49.99, 25, "computer-mouse"), ("logitech mouse ", 49.99, 5, "fa-box"),
        ("Keyboard", 89.5, 17, "keyboard")])
    c.execute("INSERT INTO sales (total_amount, created_at) VALUES (1088.5, '2026-09-03 14:53:25')")
    c.commit()
    c.close()


def test_legacy_database_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy(path)
    app = create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "k", "DEMO_MODE": False})
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    prods = {r["name"]: r for r in conn.execute("SELECT * FROM products")}
    assert len(prods) == 2                                                    # duplicates merged
    assert prods["Logitech Mouse"]["stock"] == 30 and prods["Logitech Mouse"]["price_cents"] == 4999
    assert prods["Keyboard"]["price_cents"] == 8950 and prods["Keyboard"]["icon"] == "fa-keyboard"
    sale = conn.execute("SELECT * FROM sales").fetchone()
    assert sale["total_cents"] == 108850 and sale["receipt_no"] == "LEG-000001"
    assert conn.execute("SELECT role FROM users").fetchone()["role"] == "admin"
    assert conn.execute("SELECT SUM(delta) FROM stock_movements").fetchone()[0] == 30 + 17   # opening balances
    c = Client(app)
    assert c.login("admin", "admin123").status_code == 200                    # old password hash still works
    assert c.get("/api/products").get_json()["total"] == 2
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "x.db")
    for _ in range(2):
        create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "k", "DEMO_MODE": False})
    assert connect(path).execute("PRAGMA user_version").fetchone()[0] == 2


def test_demo_seed_is_consistent(tmp_path):
    path = str(tmp_path / "demo.db")
    app = create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "k", "DEMO_MODE": True})
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] >= 10
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] > 20

    bad = conn.execute("SELECT p.id FROM products p WHERE p.stock != "
                       "(SELECT COALESCE(SUM(delta),0) FROM stock_movements WHERE product_id=p.id)").fetchall()
    assert not bad
    assert Client(app).login("cashier", "cashier123").status_code == 200
