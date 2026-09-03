import sqlite3
import jwt
import os
import csv
import io
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'inventory-pos-web-application-2026')

def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

# Init DB with hashed password support
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            icon TEXT DEFAULT 'fa-box'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_amount REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create default admin user with HASHED password if not exists
    user = cursor.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    if not user:
        hashed_pw = generate_password_hash('admin123')
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('admin', hashed_pw))
    
    conn.commit()
    conn.close()

init_db()

# Auth Middleware
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token')
        if not token:
            return jsonify({'error': 'Token is missing'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = data['user']
        except Exception:
            return jsonify({'error': 'Token is invalid or expired'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

# ---------------- API ROUTES ----------------

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({'error': 'Username and password are required'}), 400

    conn = get_db()
    user = conn.cursor().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if user and check_password_hash(user['password'], password):
        token = jwt.encode({
            'user': username,
            'exp': datetime.now(timezone.utc) + timedelta(hours=8)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        resp = jsonify({'message': 'Logged in successfully'})
        resp.set_cookie('token', token, httponly=True)
        return resp

    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = jsonify({'message': 'Logged out'})
    resp.delete_cookie('token')
    return resp

@app.route('/api/stats', methods=['GET'])
@token_required
def get_stats(current_user):
    conn = get_db()
    cursor = conn.cursor()
    
    total_revenue = cursor.execute("SELECT COALESCE(SUM(total_amount), 0) FROM sales").fetchone()[0]
    total_sales = cursor.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    low_stock = cursor.execute("SELECT COUNT(*) FROM products WHERE stock < 10").fetchone()[0]
    
    chart_rows = cursor.execute("""
        SELECT id, total_amount, strftime('%m/%d', created_at) as sale_date 
        FROM sales 
        ORDER BY id ASC LIMIT 10
    """).fetchall()
    
    if chart_rows:
        labels = [row['sale_date'] if row['sale_date'] else f"Sale #{row['id']}" for row in chart_rows]
        totals = [row['total_amount'] for row in chart_rows]
    else:
        labels = ['Order #1', 'Order #2', 'Order #3', 'Order #4', 'Order #5']
        totals = [0, 0, 0, 0, 0]

    conn.close()
    return jsonify({
        "revenue": total_revenue,
        "sales": total_sales,
        "low_stock": low_stock,
        "chart": {"labels": labels, "data": totals}
    })

@app.route('/api/products', methods=['GET', 'POST'])
@token_required
def handle_products(current_user):
    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        data = request.get_json() or {}
        name = str(data.get('name', '')).strip()
        price = data.get('price')
        stock = data.get('stock')
        icon = str(data.get('icon', 'fa-box')).strip() or 'fa-box'

        if not name:
            conn.close()
            return jsonify({'error': 'Product name cannot be empty'}), 400
        try:
            price = float(price)
            stock = int(stock)
            if price <= 0:
                conn.close()
                return jsonify({'error': 'Price must be greater than $0'}), 400
            if stock < 0:
                conn.close()
                return jsonify({'error': 'Stock cannot be negative'}), 400
        except (ValueError, TypeError):
            conn.close()
            return jsonify({'error': 'Invalid format for price or stock'}), 400

        cursor.execute("INSERT INTO products (name, price, stock, icon) VALUES (?, ?, ?, ?)",
                       (name, price, stock, icon))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Product added successfully'}), 201

    rows = cursor.execute("SELECT id, name, price, stock, icon FROM products ORDER BY id DESC").fetchall()
    conn.close()
    
    products = [
        {
            'id': row['id'],
            'name': row['name'],
            'price': row['price'],
            'stock': row['stock'],
            'icon': row['icon'] if row['icon'] else 'fa-box'
        }
        for row in rows
    ]
    return jsonify(products)

@app.route('/api/products/<product_id>', methods=['DELETE'])
def delete_product(product_id):
    conn = None
    try:
        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
        
        if cursor.rowcount == 0:
            return jsonify({"error": "Product not found"}), 404
            
        conn.commit()
        return jsonify({"message": "Product deleted successfully"}), 200

    except sqlite3.Error as err:
        if conn:
            conn.rollback()
        return jsonify({"error": f"Database error: {str(err)}"}), 500

    except Exception as err:
        return jsonify({"error": f"Unexpected error: {str(err)}"}), 500

    finally:
        if conn:
            conn.close()

@app.route('/api/checkout', methods=['POST'])
@token_required
def checkout(current_user):
    data = request.get_json() or {}
    cart = data.get('cart', [])

    if not cart or not isinstance(cart, list):
        return jsonify({'error': 'Cart is empty or invalid'}), 400

    conn = get_db()
    cursor = conn.cursor()
    total_amount = 0

    try:
        # Atomic Transaction
        cursor.execute("BEGIN TRANSACTION;")
        for item in cart:
            prod_id = item.get('id')
            qty = item.get('qty', 0)

            if not isinstance(qty, int) or qty <= 0:
                conn.rollback()
                conn.close()
                return jsonify({'error': f'Invalid quantity for item ID {prod_id}'}), 400

            product = cursor.execute("SELECT * FROM products WHERE id = ?", (prod_id,)).fetchone()
            
            if not product:
                conn.rollback()
                conn.close()
                return jsonify({'error': f'Product ID {prod_id} not found'}), 404
            
            if product['stock'] < qty:
                conn.rollback()
                conn.close()
                return jsonify({'error': f'Not enough stock for {product["name"]}'}), 400

            total_amount += product['price'] * qty
            cursor.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, prod_id))

        cursor.execute("INSERT INTO sales (total_amount) VALUES (?)", (total_amount,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Transaction failed: ' + str(e)}), 500

    conn.close()
    return jsonify({'message': 'Checkout completed successfully!'})

@app.route('/api/export/<report_type>', methods=['GET'])
@token_required
def export_csv(current_user, report_type):
    conn = get_db()
    cursor = conn.cursor()
    output = io.StringIO()
    writer = csv.writer(output)
    
    date_str = datetime.now().strftime('%Y%m%d')

    if report_type == 'sales':
        writer.writerow(['Sale ID', 'Total Amount ($)', 'Date & Time'])
        rows = cursor.execute("SELECT id, total_amount, created_at FROM sales ORDER BY id DESC").fetchall()
        for row in rows:
            writer.writerow([row['id'], f"{row['total_amount']:.2f}", row['created_at']])
        filename = f"Inventra_Sales_Report_{date_str}.csv"

    elif report_type == 'inventory':
        writer.writerow(['Product ID', 'Name', 'Price ($)', 'Stock Quantity'])
        rows = cursor.execute("SELECT id, name, price, stock FROM products ORDER BY name ASC").fetchall()
        for row in rows:
            writer.writerow([row['id'], row['name'], f"{row['price']:.2f}", row['stock']])
        filename = f"Inventra_Inventory_Report_{date_str}.csv"

    else:
        conn.close()
        return jsonify({'error': 'Invalid report type'}), 400

    conn.close()
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# Serve HTML Pages
@app.route('/')
@app.route('/<page>')
def serve_page(page='dashboard.html'):
    if not page.endswith('.html'):
        page += '.html'
    return render_template(page, user='admin')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

if __name__ == '__main__':
    app.run(debug=True, port=3600)