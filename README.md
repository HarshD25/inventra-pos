# Inventra POS — Retail & Inventory Management System

A full-stack, responsive Point of Sale (POS) and Inventory Management web application built with Python (Flask), SQLite, JavaScript, and Tailwind CSS.

---

## 🚀 Key Features

- **Real-Time POS Terminal:** Searchable product catalog, dynamic cart management, stock limit checks, and printable receipts.
- **Inventory Dashboard:** Overview of low-stock items, product categories, pricing, and visual product icons.
- **CSV Export Reporting:** Downloadable sales logs and inventory reports with dynamic timestamp file naming.
- **Secure Authentication:** User login system backed by JWTs stored securely in `HttpOnly` cookies.

---

## 🛠️ Tech Stack

- **Backend:** Python, Flask, SQLite
- **Frontend:** HTML5, Tailwind CSS, JavaScript (ES6+), FontAwesome
- **Authentication:** PyJWT (JSON Web Tokens)

---

## 🔑 Demo Credentials

To log in and test the live application or local build:

- **Username:** `admin`
- **Password:** `admin123`

---

## 📦 Local Setup Instructions

1. **Clone the repository:**

   ```bash
   git clone [https://github.com/YOUR_USERNAME/inventra-pos.git](https://github.com/YOUR_USERNAME/inventra-pos.git)
   cd inventra-pos

   ```

1. Install dependencies:

   Bash
   pip install -r requirements.txt

1. Run the Flask application:

   Bash
   python app.py

1. Access the web app:
   Open your browser and navigate to http://127.0.0.1:5000/login.html
