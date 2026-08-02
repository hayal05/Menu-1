"""
Saba Coffee - ሳባ ቡና  |  Menu backend
--------------------------------------
A small Flask app that serves the menu website and stores the menu
(categories + items) in a Postgres database hosted on Neon. The admin panel
in the browser talks to this server via a tiny JSON API, gated by an admin
PIN.

Customers can attach a payment screenshot to their order; it's stored as-is
and shown to the admin for manual verification (no automatic QR/receipt
verification — that was removed for reliability).

--------------------------------------------------------------------------
NEON SETUP (one-time)
--------------------------------------------------------------------------
1. Create a free project at https://neon.tech
2. In the Neon dashboard, open your project -> "Connection Details" and copy
   the connection string. Prefer the **pooled** connection string (the host
   contains "-pooler" in it, e.g. ...-pooler.us-east-2.aws.neon.tech) — this
   app opens/closes a connection per request, and Neon's pooler is built for
   exactly that pattern, so it avoids exhausting Neon's connection limit.
3. On Render, open your service -> Environment tab, and add:
       DATABASE_URL = <the connection string you copied>
   (Render's own "Add a Postgres" free databases are a separate thing from
   Neon — this app only needs one DATABASE_URL env var, wherever it points.)
4. Deploy. The tables are created automatically on first boot — no manual
   migration step needed.

Run locally:
    pip install -r requirements.txt
    export DATABASE_URL="postgresql://user:pass@host/dbname?sslmode=require"
    python app.py
    -> open http://localhost:5000

Deploy on Render (or any host that runs Python):
    Build command: pip install -r requirements.txt
    Start command: gunicorn app:app
"""

import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

# Characters chosen to avoid visual ambiguity when a customer copies the code
# down by hand (no 0/O, no 1/I).
ORDER_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
ORDER_CODE_LENGTH = 6

# Change this via an environment variable in production (Render -> Environment tab),
# or just edit the default string below.
ADMIN_PIN = os.environ.get('ADMIN_PIN', '2580')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it in Render -> Environment, using the "
        "connection string from your Neon project (Connection Details page)."
    )

DEFAULT_STATE = {
    "brand": {
        "name": "Saba Coffee ‑ ሳባ ቡና",
        "type": "Restaurant & Coffee House",
        "footerText": "SABA COFFEE · ADDIS ABABA",
        "profileImage": "",
    },
    "payment": {
        "bankName": "Commercial Bank of Ethiopia (CBE)",
        "accountName": "Saba Coffee",
        "accountNumber": "1000000000000",
        "instructions": "Please transfer the total amount and upload a screenshot of your payment confirmation before submitting your order."
    },
    # Physical tables the admin has generated a QR code for. Each QR points
    # back at this site with ?table=<label> in the URL, so anything ordered
    # after scanning it is auto-tagged with that table's label — no manual
    # picking of "in house" needed at that point.
    "tables": [],
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


@contextmanager
def get_conn():
    """Open a fresh connection per request/use and always close it. With
    Neon's pooled connection string this is cheap — Neon's own pgbouncer
    handles the actual pooling on their side, so we don't need to manage a
    connection pool of our own in the app (that's the pattern Neon
    recommends for short-lived serverless/request-scoped connections)."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def generate_order_code(cur, length=ORDER_CODE_LENGTH, attempts=8):
    """Generate a random public order code that isn't already in use. Falls
    back to a longer code if we somehow keep colliding (astronomically
    unlikely at this volume, but cheap to guard against)."""
    for _ in range(attempts):
        code = ''.join(secrets.choice(ORDER_CODE_ALPHABET) for _ in range(length))
        cur.execute("SELECT 1 FROM orders WHERE public_code = %s", (code,))
        if cur.fetchone() is None:
            return code
    return ''.join(secrets.choice(ORDER_CODE_ALPHABET) for _ in range(length + 2))


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS menu (id INTEGER PRIMARY KEY CHECK (id = 1), data JSONB NOT NULL)"
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    customer_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    items JSONB NOT NULL,
                    total NUMERIC NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMPTZ NOT NULL,
                    payment_screenshot TEXT,
                    verification_status TEXT DEFAULT 'unverified',
                    verification_data TEXT,
                    public_code TEXT
                )"""
            )
            # Defensive migrations for tables that may already exist from an
            # earlier, narrower schema — Postgres supports IF NOT EXISTS on
            # ADD COLUMN, so this is safe to run on every boot.
            for col_def in (
                "payment_screenshot TEXT",
                "verification_status TEXT DEFAULT 'unverified'",
                "verification_data TEXT",
                "public_code TEXT",
                # 'dine_in' orders are placed directly from the menu (no cart/checkout
                # form — see the "In house / Take away" prompt on the Add button).
                # 'table_number' is left ready for the QR-per-table feature to come
                # later; it's unused for now and always NULL.
                "order_type TEXT NOT NULL DEFAULT 'takeaway'",
                "table_number TEXT",
            ):
                cur.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col_def}")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_public_code ON orders(public_code)"
            )

            cur.execute("SELECT data FROM menu WHERE id = 1")
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO menu (id, data) VALUES (1, %s)",
                    (psycopg2.extras.Json(DEFAULT_STATE),),
                )
            else:
                saved = row['data']
                changed = False
                # Migrate older saved menus that predate the "payment" field.
                if 'payment' not in saved:
                    saved['payment'] = DEFAULT_STATE['payment']
                    changed = True
                # Migrate older saved menus that predate the "brand" field.
                if 'brand' not in saved:
                    saved['brand'] = DEFAULT_STATE['brand']
                    changed = True
                # Migrate older saved menus that predate the brand.footerText field.
                elif 'footerText' not in saved['brand']:
                    saved['brand']['footerText'] = DEFAULT_STATE['brand']['footerText']
                    changed = True
                # Migrate older saved menus that predate the brand.profileImage field.
                if 'brand' in saved and 'profileImage' not in saved['brand']:
                    saved['brand']['profileImage'] = DEFAULT_STATE['brand']['profileImage']
                    changed = True
                # Migrate older saved menus that predate the "tables" field (QR-per-table feature).
                if 'tables' not in saved:
                    saved['tables'] = DEFAULT_STATE['tables']
                    changed = True
                if changed:
                    cur.execute(
                        "UPDATE menu SET data = %s WHERE id = 1",
                        (psycopg2.extras.Json(saved),),
                    )
        conn.commit()


init_db()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/menu', methods=['GET'])
def get_menu():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM menu WHERE id = 1")
            row = cur.fetchone()
    return jsonify(row['data'])


@app.route('/api/menu', methods=['POST'])
def save_menu():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(force=True, silent=True)
    if not payload or 'categories' not in payload or 'items' not in payload:
        return jsonify({'error': 'invalid payload'}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE menu SET data = %s WHERE id = 1",
                (psycopg2.extras.Json(payload),),
            )
        conn.commit()
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

    order_type = str(payload.get('orderType', 'takeaway')).strip().lower()
    if order_type not in ('takeaway', 'dine_in'):
        order_type = 'takeaway'

    # Only meaningful for dine-in orders: which table this came from, either
    # scanned automatically from that table's QR code or typed in by hand.
    table_label = str(payload.get('tableLabel', '') or '').strip()[:60] or None
    if order_type != 'dine_in':
        table_label = None

    if order_type == 'dine_in':
        # Dine-in orders are placed directly from the menu with no checkout
        # form (see the "In house / Take away" prompt), so there's no name/phone
        # to require yet — a per-table QR code will identify these orders later.
        if not name:
            name = 'የቤት ውስጥ ደንበኛ / In-house guest'
    else:
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

    with get_conn() as conn:
        with conn.cursor() as cur:
            order_code = generate_order_code(cur)
            cur.execute(
                "INSERT INTO orders (customer_name, phone, items, total, status, created_at, "
                "payment_screenshot, public_code, order_type, table_number) VALUES "
                "(%s, %s, %s, %s, 'new', %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    name, phone, psycopg2.extras.Json(items), total,
                    datetime.now(timezone.utc), payment_screenshot, order_code, order_type, table_label,
                ),
            )
            order_id = cur.fetchone()['id']
        conn.commit()

    return jsonify({
        'ok': True,
        'orderId': order_id,
        'orderCode': order_code,
        'orderType': order_type,
        'tableLabel': table_label,
        'total': total,
    })


@app.route('/api/orders', methods=['GET'])
def list_orders():
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders ORDER BY id DESC")
            rows = cur.fetchall()

    orders = [
        {
            'id': r['id'],
            'code': r['public_code'],
            'name': r['customer_name'],
            'phone': r['phone'],
            'items': r['items'],
            'total': float(r['total']),
            'status': r['status'],
            'createdAt': r['created_at'].isoformat(),
            'paymentScreenshot': r['payment_screenshot'],
            'orderType': r.get('order_type') or 'takeaway',
            'tableLabel': r.get('table_number'),
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

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE public_code = %s", (code,))
            row = cur.fetchone()

    if row is None:
        return jsonify({'error': 'No order found with that code'}), 404

    return jsonify({
        'id': row['id'],
        'code': row['public_code'],
        'items': row['items'],
        'total': float(row['total']),
        'status': row['status'],
        'createdAt': row['created_at'].isoformat(),
        'orderType': row.get('order_type') or 'takeaway',
        'tableLabel': row.get('table_number'),
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

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = %s WHERE id = %s", (status, order_id))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/orders/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        conn.commit()
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
