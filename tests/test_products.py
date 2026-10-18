from conftest import make_product


def test_create_list_update_archive(admin):
    pid = make_product(admin, sku="abc 1")
    items = admin.get("/api/products").get_json()["items"]
    assert items[0]["sku"] == "ABC-1" and items[0]["price_cents"] == 1000 and items[0]["cost_cents"] == 400
    assert admin.put(f"/api/products/{pid}", {"price": "12.50", "name": "Widget Pro"}).status_code == 200
    p = admin.get("/api/products").get_json()["items"][0]
    assert p["price_cents"] == 1250 and p["name"] == "Widget Pro"
    assert admin.delete(f"/api/products/{pid}").status_code == 200
    assert admin.get("/api/products").get_json()["total"] == 0
    assert admin.get("/api/products?active=all").get_json()["total"] == 1


def test_validation(admin):
    assert admin.post("/api/products", {"name": "", "price": 1}).status_code == 400
    assert admin.post("/api/products", {"name": "x", "price": -1}).status_code == 400
    assert admin.post("/api/products", {"name": "x", "price": "abc"}).status_code == 400
    assert admin.post("/api/products", {"name": "x", "price": 1, "stock": -3}).status_code == 400
    assert admin.post("/api/products", {"name": "x", "price": 1, "stock": 1.5}).status_code == 400
    assert admin.post("/api/products", {"name": "x", "price": "NaN"}).status_code == 400


def test_duplicate_sku_rejected(admin):
    make_product(admin, sku="DUP")
    assert admin.post("/api/products", {"name": "y", "price": 1, "sku": "dup"}).status_code == 409


def test_auto_sku_and_icon_sanitised(admin):
    make_product(admin, icon="fa-x\" onclick=\"alert(1)")
    p = admin.get("/api/products").get_json()["items"][0]
    assert p["sku"].startswith("SKU-") and p["icon"] == "fa-box"


def test_search_filter_paging(admin):
    for i in range(7):
        make_product(admin, name=f"Item {i}", stock=0 if i == 0 else 20)
    make_product(admin, name="100% Cotton_Tee", category="Apparel", stock=2)
    assert admin.get("/api/products?q=cotton_").get_json()["total"] == 1
    assert admin.get("/api/products?q=%25").get_json()["total"] == 1      # % is literal, not a wildcard
    assert admin.get("/api/products?stock=out").get_json()["total"] == 1
    assert admin.get("/api/products?stock=low").get_json()["total"] == 1
    assert admin.get("/api/products?category=Apparel").get_json()["total"] == 1
    page = admin.get("/api/products?per_page=3&page=2").get_json()
    assert len(page["items"]) == 3 and page["pages"] == 3


def test_cashier_does_not_see_cost(admin, cashier):
    make_product(admin)
    assert "cost_cents" not in cashier.get("/api/products").get_json()["items"][0]
    assert "cost_cents" in admin.get("/api/products").get_json()["items"][0]


def test_stock_adjustment_ledger(admin, db):
    pid = make_product(admin, stock=10)
    assert admin.post(f"/api/products/{pid}/stock", {"delta": 5, "reason": "restock"}).get_json()["stock"] == 15
    assert admin.post(f"/api/products/{pid}/stock", {"delta": -3, "reason": "damage", "note": "dropped"}).get_json()["stock"] == 12
    assert admin.post(f"/api/products/{pid}/stock", {"delta": -50, "reason": "adjustment"}).status_code == 409
    assert admin.post(f"/api/products/{pid}/stock", {"delta": -1, "reason": "restock"}).status_code == 400
    assert admin.post(f"/api/products/{pid}/stock", {"delta": 0, "reason": "adjustment"}).status_code == 400
    total = db.execute("SELECT SUM(delta) FROM stock_movements WHERE product_id=?", (pid,)).fetchone()[0]
    assert total == 12                                                    # ledger reconciles with stock
    assert len(admin.get(f"/api/products/{pid}/movements").get_json()["items"]) == 3
