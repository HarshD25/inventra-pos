import threading

from conftest import Client, make_product


def checkout(client, cart, **extra):
    return client.post("/api/checkout", {"cart": cart, **extra})


def stock_map(client):
    return {p["id"]: p["stock"] for p in client.get("/api/products").get_json()["items"]}


def test_totals_tax_and_stock(admin):
    a = make_product(admin, price="10.00")
    b = make_product(admin, name="Gadget", price="19.99")
    r = checkout(admin, [{"id": a, "qty": 2}, {"id": b, "qty": 1}])
    assert r.status_code == 201
    s = r.get_json()["sale"]
    assert s["subtotal_cents"] == 3999 and s["tax_cents"] == 200 and s["total_cents"] == 4199   # 5% tax, half-up
    assert s["receipt_no"] == f"INV-{s['id']:06d}"
    stocks = stock_map(admin)
    assert stocks[a] == 18 and stocks[b] == 19


def test_server_ignores_client_prices(admin):
    a = make_product(admin, price="10.00")
    s = checkout(admin, [{"id": a, "qty": 1, "price": 0.01}]).get_json()["sale"]
    assert s["subtotal_cents"] == 1000


def test_oversell_rejected_and_rolled_back(admin):
    a = make_product(admin, stock=3)
    b = make_product(admin, name="Other", stock=10)
    r = checkout(admin, [{"id": b, "qty": 2}, {"id": a, "qty": 4}])
    assert r.status_code == 409
    assert stock_map(admin) == {a: 3, b: 10}
    assert admin.get("/api/sales").get_json()["total"] == 0


def test_invalid_carts(admin):
    a = make_product(admin)
    assert checkout(admin, []).status_code == 400
    assert checkout(admin, [{"id": a, "qty": 0}]).status_code == 400
    assert checkout(admin, [{"id": a, "qty": -1}]).status_code == 400
    assert checkout(admin, [{"id": a, "qty": 1.5}]).status_code == 400
    assert checkout(admin, [{"id": 9999, "qty": 1}]).status_code == 404
    admin.delete(f"/api/products/{a}")
    assert checkout(admin, [{"id": a, "qty": 1}]).status_code == 409   # archived product can't be sold


def test_duplicate_lines_are_merged(admin):
    a = make_product(admin, stock=5)
    assert checkout(admin, [{"id": a, "qty": 3}, {"id": a, "qty": 3}]).status_code == 409
    s = checkout(admin, [{"id": a, "qty": 2}, {"id": a, "qty": 2}]).get_json()["sale"]
    assert s["items"][0]["qty"] == 4


def test_discounts_and_cashier_limit(admin, cashier):
    a = make_product(admin, price="100.00", stock=50)
    s = checkout(admin, [{"id": a, "qty": 1}], discount={"type": "percent", "value": 20}).get_json()["sale"]
    assert s["discount_cents"] == 2000 and s["total_cents"] == 8400          # (100-20)*1.05
    assert checkout(cashier, [{"id": a, "qty": 1}], discount={"type": "percent", "value": 25}).status_code == 403
    assert checkout(cashier, [{"id": a, "qty": 1}], discount={"type": "amount", "value": "10"}).status_code == 201
    assert checkout(admin, [{"id": a, "qty": 1}], discount={"type": "amount", "value": "150"}).status_code == 400
    assert checkout(admin, [{"id": a, "qty": 1}], discount={"type": "percent", "value": 101}).status_code == 400


def test_cash_change_and_payment_validation(admin):
    a = make_product(admin, price="10.00")
    s = checkout(admin, [{"id": a, "qty": 1}], payment={"method": "cash", "tendered": "20"}).get_json()["sale"]
    assert s["total_cents"] == 1050 and s["change_cents"] == 950
    assert checkout(admin, [{"id": a, "qty": 1}], payment={"method": "cash", "tendered": "5"}).status_code == 400
    assert checkout(admin, [{"id": a, "qty": 1}], payment={"method": "bitcoin"}).status_code == 400
    s = checkout(admin, [{"id": a, "qty": 1}], payment={"method": "card"}).get_json()["sale"]
    assert s["change_cents"] == 0 and s["tendered_cents"] == s["total_cents"]


def test_idempotency_key_prevents_double_charge(admin):
    a = make_product(admin, stock=10)
    r1 = checkout(admin, [{"id": a, "qty": 2}], idempotency_key="abc-123")
    r2 = checkout(admin, [{"id": a, "qty": 2}], idempotency_key="abc-123")
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.get_json()["sale"]["id"] == r2.get_json()["sale"]["id"]
    assert stock_map(admin)[a] == 8


def test_concurrent_checkouts_never_oversell(app, admin):
    a = make_product(admin, stock=5)
    results = []

    def worker():
        c = Client(app)
        c.login()
        results.append(checkout(c, [{"id": a, "qty": 1}]).status_code)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 5 and results.count(409) == 7
    assert stock_map(admin)[a] == 0


def test_cashier_sees_only_own_sales(admin, cashier):
    a = make_product(admin)
    own = checkout(cashier, [{"id": a, "qty": 1}]).get_json()["sale"]["id"]
    other = checkout(admin, [{"id": a, "qty": 1}]).get_json()["sale"]["id"]
    assert cashier.get("/api/sales").get_json()["total"] == 1
    assert cashier.get(f"/api/sales/{own}").status_code == 200
    assert cashier.get(f"/api/sales/{other}").status_code == 404
    assert admin.get("/api/sales").get_json()["total"] == 2


def test_refund_partial_then_full(admin, cashier, db):
    a = make_product(admin, price="100.00", stock=10)
    sale = checkout(admin, [{"id": a, "qty": 3}], discount={"type": "percent", "value": 10}).get_json()["sale"]
    # subtotal 300, discount 30, tax 13.50 -> total 283.50
    assert sale["total_cents"] == 28350
    item = sale["items"][0]["id"]
    assert cashier.post(f"/api/sales/{sale['id']}/refund", {"items": [{"sale_item_id": item, "qty": 1}]}).status_code == 403
    r1 = admin.post(f"/api/sales/{sale['id']}/refund", {"items": [{"sale_item_id": item, "qty": 1}], "reason": "defect"})
    assert r1.status_code == 200 and r1.get_json()["amount_cents"] == 9450          # 1/3 of 283.50
    assert r1.get_json()["sale"]["status"] == "partially_refunded"
    assert stock_map(admin)[a] == 8
    assert admin.post(f"/api/sales/{sale['id']}/refund", {"items": [{"sale_item_id": item, "qty": 3}]}).status_code == 409
    r2 = admin.post(f"/api/sales/{sale['id']}/refund", {"items": [{"sale_item_id": item, "qty": 2}]})
    assert r2.get_json()["amount_cents"] == 28350 - 9450
    final = r2.get_json()["sale"]
    assert final["status"] == "refunded" and final["refunded_cents"] == 28350
    assert stock_map(admin)[a] == 10
    total = db.execute("SELECT SUM(delta) FROM stock_movements WHERE product_id=?", (a,)).fetchone()[0]
    assert total == 10


def test_refund_without_restock(admin):
    a = make_product(admin, stock=5)
    sale = checkout(admin, [{"id": a, "qty": 1}]).get_json()["sale"]
    admin.post(f"/api/sales/{sale['id']}/refund",
               {"items": [{"sale_item_id": sale["items"][0]["id"], "qty": 1}], "restock": False})
    assert stock_map(admin)[a] == 4


def test_stats_and_exports(admin):
    a = make_product(admin, name="=HYPERLINK(\"x\")", price="10.00", cost="4.00", stock=3, reorder_level=5)
    checkout(admin, [{"id": a, "qty": 1}], payment={"method": "upi"})
    st = admin.get("/api/stats?days=7").get_json()
    assert st["today"]["orders"] == 1 and st["today"]["revenue_cents"] == 1050
    assert st["today"]["profit_cents"] == 600 and st["low_stock_count"] == 1
    assert sum(st["chart"]["revenue_cents"]) == 1050 and len(st["chart"]["labels"]) == 7
    assert st["top_products"][0]["units"] == 1 and st["payments"][0]["method"] == "upi"
    inv = admin.get("/api/export/inventory")
    assert inv.mimetype == "text/csv" and "'=HYPERLINK" in inv.get_data(as_text=True)
    assert admin.get("/api/export/sales?from=2000-01-01&to=2100-01-01").status_code == 200
    assert admin.get("/api/export/bogus").status_code == 404
    assert admin.get("/api/export/sales?from=garbage").status_code == 400


def test_sales_filters(admin):
    a = make_product(admin)
    checkout(admin, [{"id": a, "qty": 1}], payment={"method": "card"}, customer={"name": "Ravi"})
    checkout(admin, [{"id": a, "qty": 1}], payment={"method": "cash"})
    assert admin.get("/api/sales?payment=card").get_json()["total"] == 1
    assert admin.get("/api/sales?q=ravi").get_json()["total"] == 1
    assert admin.get("/api/sales?q=INV-0000").get_json()["total"] == 2
    assert admin.get("/api/sales?from=2099-01-01").get_json()["total"] == 0
