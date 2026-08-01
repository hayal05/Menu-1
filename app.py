"""
Ebuka Coffee - ኢቡካ ቡና  |  Menu backend
--------------------------------------
A small Flask app that serves the menu website and stores the menu
(categories + items) in a local SQLite database. The admin panel in the
browser talks to this server via a tiny JSON API, gated by an admin PIN.

Customers can attach a payment screenshot to their order; it's stored as-is
and shown to the admin for manual verification (no automatic QR/receipt
verification — that was removed for reliability).

Run locally:
    pip install -r requirements.txt
    python app.py
    -> open http://localhost:5000

Deploy on Render (or any host that runs Python):
    Build command: pip install -r requirements.txt
    Start command: gunicorn app:app
"""

import os
import json
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

# Characters chosen to avoid visual ambiguity when a customer copies the code
# down by hand (no 0/O, no 1/I).
ORDER_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
ORDER_CODE_LENGTH = 6


def generate_order_code(conn, length=ORDER_CODE_LENGTH, attempts=8):
    """Generate a random public order code that isn't already in use. Falls
    back to a longer code if we somehow keep colliding (astronomically
    unlikely at this volume, but cheap to guard against)."""
    for _ in range(attempts):
        code = ''.join(secrets.choice(ORDER_CODE_ALPHABET) for _ in range(length))
        exists = conn.execute("SELECT 1 FROM orders WHERE public_code = ?", (code,)).fetchone()
        if not exists:
            return code
    return ''.join(secrets.choice(ORDER_CODE_ALPHABET) for _ in range(length + 2))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'menu.db')

# Change this via an environment variable in production (Render -> Environment tab),
# or just edit the default string below.
ADMIN_PIN = os.environ.get('ADMIN_PIN', '2580')

DEFAULT_STATE = {
    "brand": {
        "name": "Saba Coffee ‑ ሳባ ቡና",
        "type": "Restaurant & Coffee House",
    },
    "payment": {
        "bankName": "Commercial Bank of Ethiopia (CBE)",
        "accountName": "Saba Coffee",
        "accountNumber": "1000000000000",
        "instructions": "Please transfer the total amount and upload a screenshot of your payment confirmation before submitting your order."
    },
    "categories": [
        {"id": "tsom", "name": "የጾም ምግቦች"},
        {"id": "fisk", "name": "የፍስክ ምግቦች"},
        {"id": "pizzaburger", "name": "ፒዛ እና በርገር"},
        {"id": "hot", "name": "ትኩስ ነገሮች"},
        {"id": "cold", "name": "ቀዝቃዛ መጠጦች"},
    ],
    "items": [
        {"id": "i-1", "categoryId": "tsom", "name": "ሽሮ ወጥ", "nameEn": "Shiro Wet", "price": 90, "desc": "የተፈጨ አተር ወጥ ከቅቤ ጋር", "icon": "🍲", "available": True},
        {"id": "i-2", "categoryId": "tsom", "name": "አተር ወጥ", "nameEn": "Ater Wet", "price": 85, "desc": "", "icon": "🍛", "available": True},
        {"id": "i-3", "categoryId": "tsom", "name": "ፍርፍር", "nameEn": "Firfir", "price": 80, "desc": "", "icon": "🍚", "available": True},
        {"id": "i-4", "categoryId": "tsom", "name": "ጎመን ወጥ", "nameEn": "Gomen", "price": 75, "desc": "", "icon": "🥬", "available": True},
        {"id": "i-5", "categoryId": "fisk", "name": "ዶሮ ወጥ", "nameEn": "Doro Wet", "price": 180, "desc": "ከእንቁላል ጋር", "icon": "🍗", "available": True},
        {"id": "i-6", "categoryId": "fisk", "name": "ክትፎ", "nameEn": "Kitfo", "price": 220, "desc": "", "icon": "🥩", "available": True},
        {"id": "i-7", "categoryId": "fisk", "name": "ጥብስ", "nameEn": "Tibs", "price": 200, "desc": "", "icon": "🍖", "available": True},
        {"id": "i-8", "categoryId": "pizzaburger", "name": "ማርጌሪታ ፒዛ", "nameEn": "Margherita Pizza", "price": 220, "desc": "", "icon": "🍕", "available": True},
        {"id": "i-9", "categoryId": "pizzaburger", "name": "ፔፐሮኒ ፒዛ", "nameEn": "Pepperoni Pizza", "price": 260, "desc": "", "icon": "🍕", "available": True},
        {"id": "i-10", "categoryId": "pizzaburger", "name": "ቺዝ በርገር", "nameEn": "Cheese Burger", "price": 180, "desc": "", "icon": "🍔", "available": True},
        {"id": "i-11", "categoryId": "hot", "name": "ቡና", "nameEn": "Buna / Coffee", "price": 40, "desc": "", "icon": "☕", "available": True},
        {"id": "i-12", "categoryId": "hot", "name": "ማኪያቶ", "nameEn": "Macchiato", "price": 45, "desc": "", "icon": "☕", "available": True},
        {"id": "i-13", "categoryId": "hot", "name": "ሻይ", "nameEn": "Tea", "price": 30, "desc": "", "icon": "🍵", "available": True},
        {"id": "i-14", "categoryId": "cold", "name": "ጭማቂ", "nameEn": "Fresh Juice", "price": 60, "desc": "", "icon": "🥤", "available": True},
        {"id": "i-15", "categoryId": "cold", "name": "ለስላሳ", "nameEn": "Soft Drink", "price": 45, "desc": "", "icon": "🥤", "available": True},
        {"id": "i-16", "categoryId": "cold", "name": "ስሙዚ", "nameEn": "Smoothie", "price": 90, "desc": "", "icon": "🍹", "available": True},
    ],
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS menu (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            items TEXT NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            payment_screenshot TEXT
        )"""
    )
    # Migrate older databases that predate the payment_screenshot column.
    existing_cols = [r['name'] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if 'payment_screenshot' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_screenshot TEXT")
        conn.commit()
    # Migrate older databases that predate CBE auto-verification.
    if 'verification_status' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN verification_status TEXT DEFAULT 'unverified'")
        conn.commit()
    if 'verification_data' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN verification_data TEXT")
        conn.commit()
    # Migrate older databases that predate public order codes.
    if 'public_code' not in existing_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN public_code TEXT")
        conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_public_code ON orders(public_code)")
    conn.commit()

    row = conn.execute("SELECT data FROM menu WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO menu (id, data) VALUES (1, ?)", (json.dumps(DEFAULT_STATE),)
        )
        conn.commit()
    else:
        # Migrate older saved menus that predate the "payment" field.
        saved = json.loads(row['data'])
        if 'payment' not in saved:
            saved['payment'] = DEFAULT_STATE['payment']
            conn.execute("UPDATE menu SET data = ? WHERE id = 1", (json.dumps(saved),))
            conn.commit()
        # Migrate older saved menus that predate the "brand" field.
        if 'brand' not in saved:
            saved['brand'] = DEFAULT_STATE['brand']
            conn.execute("UPDATE menu SET data = ? WHERE id = 1", (json.dumps(saved),))
            conn.commit()
    conn.close()


init_db()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/menu', methods=['GET'])
def get_menu():
    conn = get_conn()
    row = conn.execute("SELECT data FROM menu WHERE id = 1").fetchone()
    conn.close()
    return jsonify(json.loads(row['data']))


@app.route('/api/menu', methods=['POST'])
def save_menu():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(force=True, silent=True)
    if not payload or 'categories' not in payload or 'items' not in payload:
        return jsonify({'error': 'invalid payload'}), 400

    conn = get_conn()
    conn.execute("UPDATE menu SET data = ? WHERE id = 1", (json.dumps(payload),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/verify', methods=['POST'])
def verify_pin():
    payload = request.get_json(force=True, silent=True) or {}
    pin = payload.get('pin', '')
    return jsonify({'ok': pin == ADMIN_PIN})


# ---------------------------------------------------------------------------
# Orders: customers submit their name, phone number, and cart from the site.
# Order data is only ever readable/manageable by the admin (PIN-protected).
# ---------------------------------------------------------------------------

@app.route('/api/orders', methods=['POST'])
def create_order():
    payload = request.get_json(force=True, silent=True) or {}
    name = str(payload.get('name', '')).strip()
    phone = str(payload.get('phone', '')).strip()
    items = payload.get('items')
    # Optional: a base64 data-URL (e.g. "data:image/png;base64,....") of the
    # customer's bank-transfer / payment confirmation screenshot.
    payment_screenshot = payload.get('paymentScreenshot')

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not phone:
        return jsonify({'error': 'phone is required'}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({'error': 'order must include at least one item'}), 400

    total = 0.0
    for it in items:
        try:
            total += float(it.get('price', 0)) * int(it.get('qty', 1))
        except (TypeError, ValueError, AttributeError):
            return jsonify({'error': 'invalid item in order'}), 400

    if payment_screenshot is not None:
        if not isinstance(payment_screenshot, str) or not payment_screenshot.startswith('data:image/'):
            return jsonify({'error': 'invalid payment screenshot'}), 400
        # Guard against absurdly large uploads (base64 data URLs run ~33% bigger
        # than the original file; this caps the source image at roughly 6MB).
        if len(payment_screenshot) > 8_000_000:
            return jsonify({'error': 'payment screenshot is too large'}), 400

    conn = get_conn()
    order_code = generate_order_code(conn)
    cur = conn.execute(
        "INSERT INTO orders (customer_name, phone, items, total, status, created_at, payment_screenshot, "
        "public_code) VALUES (?, ?, ?, ?, 'new', ?, ?, ?)",
        (
            name, phone, json.dumps(items), total,
            datetime.now(timezone.utc).isoformat(), payment_screenshot, order_code,
        ),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return jsonify({
        'ok': True,
        'orderId': order_id,
        'orderCode': order_code,
        'total': total,
    })


@app.route('/api/orders', methods=['GET'])
def list_orders():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    conn = get_conn()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()

    orders = [
        {
            'id': r['id'],
            'code': r['public_code'],
            'name': r['customer_name'],
            'phone': r['phone'],
            'items': json.loads(r['items']),
            'total': r['total'],
            'status': r['status'],
            'createdAt': r['created_at'],
            'paymentScreenshot': r['payment_screenshot'],
        }
        for r in rows
    ]
    return jsonify(orders)


@app.route('/api/orders/status/<code>', methods=['GET'])
def order_status_by_code(code):
    """Public order-status lookup — no PIN required. Looks up a single order
    by its random public code (given to the customer right after checkout),
    not by phone number, so no scanning/matching across every order and no
    way to browse or guess your way into someone else's order history."""
    code = (code or '').strip().upper()
    if not code:
        return jsonify({'error': 'Order code is required'}), 400

    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE public_code = ?", (code,)).fetchone()
    conn.close()

    if row is None:
        return jsonify({'error': 'No order found with that code'}), 404

    return jsonify({
        'id': row['id'],
        'code': row['public_code'],
        'items': json.loads(row['items']),
        'total': row['total'],
        'status': row['status'],
        'createdAt': row['created_at'],
    })


@app.route('/api/orders/<int:order_id>', methods=['PATCH'])
def update_order(order_id):
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(force=True, silent=True) or {}
    status = payload.get('status')
    if status not in ('new', 'done'):
        return jsonify({'error': 'invalid status'}), 400

    conn = get_conn()
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/orders/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    conn = get_conn()
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
