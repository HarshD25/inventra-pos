# Inventra POS

A simple inventory and point-of-sale web app made with Flask and SQLite. A small shop can sell items, track stock, refund orders and see sales on a dashboard.

**Live demo:** https://inventra-pos-addp.onrender.com

Login: `admin` / `admin123` (full access) or `cashier` / `cashier123` (limited)

![Dashboard](screenshots/dashboard.png)
![POS](screenshots/pos.png)

## Features

- **POS:** search or scan a SKU, discount and tax, cash/card/UPI, change calculation, printable receipt
- **Products:** SKU, category, cost and selling price, low-stock alerts, archive instead of delete
- **Stock history:** every stock change is saved with a reason
- **Refunds:** full or partial, with an option to put items back in stock
- **Dashboard:** revenue, orders, profit, top products, low stock, CSV export
- **Users:** admin and cashier roles, user management, store settings, audit log

## What I focused on

- Money is stored as whole numbers (paise/cents), so there are no rounding errors.
- The server calculates all prices and totals, so the browser can't change them.
- Stock never goes below zero. A test sends 12 purchases at once for 5 items, and only 5 go through.
- An old database is upgraded automatically without losing data.
- Passwords are hashed, accounts lock after 5 wrong logins, and every request checks the user's role.

## Tech

Python, Flask, SQLite, JavaScript, Tailwind CSS, Chart.js, pytest, Docker, GitHub Actions

## Run it

```bash
python -m venv venv
venv\Scripts\activate        # Mac/Linux: source venv/bin/activate
pip install -r requirements-dev.txt
python app.py
```

Open http://localhost:5000. The first run adds sample products and a month of sales.

For an empty store, set `DEMO_MODE=0` and `ADMIN_PASSWORD` first (see `.env.example`).

Run the 33 tests with `python -m pytest`.

## Files

- `app.py` starts the app
- `api.py` API routes
- `services.py` checkout, refunds, stock
- `auth.py` login and roles
- `db.py` database setup and sample data
- `templates/` pages
- `tests/` tests

## Limitations

SQLite suits one shop or a demo. There is one tax rate for the whole store.