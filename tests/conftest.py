import os
import sys

os.environ["INVENTRA_EAGER"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from app import create_app
from db import connect


@pytest.fixture()
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "test-secret",
                       "DEMO_MODE": False, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "admin12345"})


class Client:
    def __init__(self, app):
        self.c = app.test_client()

    def _kw(self, kw):
        kw.setdefault("headers", {})["X-Requested-With"] = "XMLHttpRequest"
        return kw

    def get(self, url, **kw): return self.c.get(url, **self._kw(kw))
    def post(self, url, json=None, **kw): return self.c.post(url, json=json, **self._kw(kw))
    def put(self, url, json=None, **kw): return self.c.put(url, json=json, **self._kw(kw))
    def delete(self, url, **kw): return self.c.delete(url, **self._kw(kw))

    def login(self, username="admin", password="admin12345"):
        return self.post("/api/login", {"username": username, "password": password})


@pytest.fixture()
def admin(app):
    c = Client(app)
    assert c.login().status_code == 200
    return c


@pytest.fixture()
def cashier(app, admin):
    r = admin.post("/api/users", {"username": "casey", "password": "casey12345", "role": "cashier"})
    assert r.status_code == 201
    c = Client(app)
    assert c.login("casey", "casey12345").status_code == 200
    return c


@pytest.fixture()
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


def make_product(client, **over):
    data = {"name": "Widget", "price": "10.00", "cost": "4.00", "stock": 20, "reorder_level": 5, "category": "Tools"}
    data.update(over)
    r = client.post("/api/products", data)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]
