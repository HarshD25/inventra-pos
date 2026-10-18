from conftest import Client


def test_login_logout_and_me(app):
    c = Client(app)
    assert c.get("/api/me").status_code == 401
    assert c.login().status_code == 200
    me = c.get("/api/me").get_json()
    assert me["user"]["role"] == "admin"
    c.post("/api/logout")
    assert c.get("/api/me").status_code == 401


def test_wrong_password_and_throttle(app):
    c = Client(app)
    for _ in range(5):
        assert c.login("admin", "nope").status_code == 401
    assert c.login("admin", "admin12345").status_code == 429     # locked even with the right password


def test_successful_login_clears_failures(app):
    c = Client(app)
    for _ in range(3):
        c.login("admin", "bad")
    assert c.login().status_code == 200
    for _ in range(3):
        c.login("admin", "bad")
    assert c.login().status_code == 200


def test_csrf_header_required_for_writes(app):
    raw = app.test_client()
    r = raw.post("/api/login", json={"username": "admin", "password": "admin12345"})
    assert r.status_code == 403


def test_cashier_cannot_use_admin_endpoints(cashier):
    assert cashier.post("/api/products", {"name": "x", "price": 1}).status_code == 403
    assert cashier.get("/api/users").status_code == 403
    assert cashier.get("/api/stats").status_code == 403
    assert cashier.put("/api/settings", {"tax_rate": 1}).status_code == 403
    assert cashier.get("/api/export/sales").status_code == 403


def test_deactivated_user_loses_access_immediately(app, admin, cashier):
    uid = next(u["id"] for u in admin.get("/api/users").get_json()["items"] if u["username"] == "casey")
    assert cashier.get("/api/me").status_code == 200
    assert admin.put(f"/api/users/{uid}", {"is_active": False}).status_code == 200
    assert cashier.get("/api/me").status_code == 401


def test_cannot_demote_last_admin(admin):
    me = admin.get("/api/users").get_json()["items"][0]
    r = admin.put(f"/api/users/{me['id']}", {"role": "cashier"})
    assert r.status_code == 409


def test_change_password(app, admin):
    assert admin.post("/api/me/password", {"current_password": "wrong", "new_password": "newpass123"}).status_code == 403
    assert admin.post("/api/me/password", {"current_password": "admin12345", "new_password": "short"}).status_code == 400
    assert admin.post("/api/me/password", {"current_password": "admin12345", "new_password": "newpass123"}).status_code == 200
    assert Client(app).login("admin", "newpass123").status_code == 200


def test_pages_require_login_and_roles(app, cashier):
    anon = app.test_client()
    assert anon.get("/dashboard.html").status_code == 302
    assert cashier.get("/admin.html").status_code == 302            # redirected away
    assert cashier.get("/sales.html").status_code == 200
    assert anon.get("/nope").status_code == 404
