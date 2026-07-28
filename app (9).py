"""
Ebuka Coffee - ኢቡካ ቡና  |  Menu backend
--------------------------------------
A small Flask app that serves the menu website and stores the menu
(categories + items) in a local SQLite database. The admin panel in the
browser talks to this server via a tiny JSON API, gated by an admin PIN.

Run locally:
    pip install -r requirements.txt
    python app.py
    -> open http://localhost:5000

Deploy on Render (or any host that runs Python):
    Build command: pip install -r requirements.txt
    Start command: gunicorn app:app
"""

import os
import io
import re
import json
import base64
import sqlite3
from urllib.parse import urlparse
from datetime import datetime, timezone

import numpy as np
import cv2
import requests
import pdfplumber
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'menu.db')

# Change this via an environment variable in production (Render -> Environment tab),
# or just edit the default string below.
ADMIN_PIN = os.environ.get('ADMIN_PIN', '2580')

DEFAULT_STATE = {
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


# ---------------------------------------------------------------------------
# CBE payment auto-verification
# ---------------------------------------------------------------------------
# Every CBE ("Commercial Bank of Ethiopia") transfer receipt carries a QR code
# that encodes a short verification URL (served from apps.cbe.com.et). That
# URL returns the bank's own copy of the transaction receipt (payer, receiver,
# amount, date, reference number...). So instead of trusting a screenshot at
# face value, we:
#   1. decode the QR code embedded in the uploaded screenshot,
#   2. make sure it actually points at a CBE domain (never fetch arbitrary
#      URLs a forged image might contain),
#   3. fetch the receipt CBE serves for that URL and pull the fields out of it,
#   4. compare the amount + receiving account against the order + the
#      admin's configured payment details.
#
# NOTE: CBE does not publish an official API for this — the field labels
# below are based on the layout CBE's verification receipts commonly use.
# If CBE changes that layout the regexes may need updating; that's why we
# always store the raw extracted text too, so an admin can still eyeball a
# screenshot that fails auto-parsing.

CBE_ROOT_DOMAIN = 'cbe.com.et'


def _is_cbe_host(hostname):
    """True if hostname is cbe.com.et or any subdomain of it (e.g. the real
    receipt-verification link lives on mbreciept.cbe.com.et — CBE's own
    subdomain, typo and all — not a made-up 'apps.cbe.com.et')."""
    if not hostname:
        return False
    hostname = hostname.lower()
    return hostname == CBE_ROOT_DOMAIN or hostname.endswith('.' + CBE_ROOT_DOMAIN)

CBE_FIELD_PATTERNS = {
    # CBE's own receipt text (and the "Thank you" screen the app shows) reads
    # as a sentence, not a labeled form — e.g.:
    #   "You have successfully transferred 500 ETB from your account
    #    1*********2345 to Saba Coffee with Abay Bank account number
    #    1**56789 on Jun 22, 2026 07:13 AM with Transaction ID: FT26173S4Z9B.
    #    Remark: p. Total Amount Debited: 508.30 ETB with Service Charge of
    #    ETB7.00, VAT (15%) of ETB1.05 and Disaster Recovery (5%) of ETB0.25."
    # The "transferred X ETB" figure is what actually reaches the merchant —
    # that's what we compare against the order total. "Total Amount Debited"
    # includes the sender's bank fees on top and is not useful for matching.
    'amount': [
        r'transferred\s+([\d,]+\.?\d*)\s*ETB',
        r"Transferred\s*Amount\s*[:\-]?\s*([\d,]+\.?\d*)",   # fallback: older tabular-receipt guess
        r"\bAmount\s*[:\-]?\s*([\d,]+\.?\d*)\s*(?:ETB|Birr)?",
    ],
    'total_debited': [
        r'Total\s+Amount\s+Debited\s*:?\s*([\d,]+\.?\d*)\s*ETB',
    ],
    'payer_account': [
        r'from\s+your\s+account\s+([0-9\*]+)',
        r"Payer'?s?\s*Account\s*[:\-]?\s*([0-9\*Xx]+)",
    ],
    'receiver': [
        r'\bto\s+([A-Za-z][A-Za-z\.\s]*?)\s+(?:with\s+[A-Za-z\s]+?\s+)?account\s+number',
        r"Receiver'?s?\s*Name\s*[:\-]?\s*([^\n]+)",
        r"Credited\s*Party\s*Name\s*[:\-]?\s*([^\n]+)",
    ],
    'receiver_account': [
        r'account\s+number\s+([0-9\*Xx]+)',
        r"Receiver'?s?\s*Account\s*[:\-]?\s*([0-9\*Xx]+)",
        r"Credited\s*Party\s*Account\s*[:\-]?\s*([0-9\*Xx]+)",
    ],
    'reference': [
        r'Transaction\s+ID\s*:?\s*([A-Za-z0-9]+)',
        r'Reference\s*No\.?\s*\(?\s*VAT\s*Invoice\s*No\.?\)?\s*[:\-]?\s*([A-Za-z0-9]+)',
        r'Reference\s*No\.?\s*[:\-]?\s*([A-Za-z0-9]+)',
        r'\bFT\s*([A-Za-z0-9]{8,})',
    ],
    'date': [
        r'\bon\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)',
        r"Payment\s*Date\s*(?:&|and)?\s*Time\s*[:\-]?\s*([0-9A-Za-z\/\-.,: ]+)",
        r"Transaction\s*Date\s*[:\-]?\s*([0-9A-Za-z\/\-.,: ]+)",
    ],
    'payer': [
        r"Payer'?s?\s*Name\s*[:\-]?\s*([^\n]+)",
    ],
    'remark': [
        r'Remark\s*:\s*([^\.]*)\.',
    ],
}


def _decode_qr_from_data_url(data_url):
    """Decode a QR code embedded in a base64 image data-URL.

    Returns the raw string encoded in the QR (normally a CBE verification
    URL) or None if no QR code could be found.
    """
    try:
        _, encoded = data_url.split(',', 1)
        img_bytes = base64.b64decode(encoded)
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None

        detector = cv2.QRCodeDetector()

        data, _, _ = detector.detectAndDecode(img)
        if data:
            return data

        # Screenshots are often small/low-res relative to a real photo, and
        # QR detectors do noticeably better on upscaled images — try that
        # before giving up.
        h, w = img.shape[:2]
        longest = max(h, w)
        if longest and longest < 1400:
            scale = 1400 / longest
            resized = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
            data, _, _ = detector.detectAndDecode(resized)
            if data:
                return data

        # Grayscale + threshold sometimes helps with busy chat-app screenshots.
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(thresh)
        return data or None
    except Exception:
        return None


def _fetch_cbe_receipt(url):
    """Fetch the receipt page/PDF a CBE QR code points to and return its
    text content. Refuses to fetch anything outside CBE's own domain."""
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception:
        return {'ok': False, 'error': 'invalid_url'}

    if not _is_cbe_host(host):
        return {'ok': False, 'error': 'qr_not_cbe'}

    try:
        resp = requests.get(
            url,
            timeout=12,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; EbukaCoffeeOrderVerifier/1.0)'},
        )
    except requests.RequestException as exc:
        return {'ok': False, 'error': 'fetch_failed', 'detail': str(exc)}

    if resp.status_code != 200:
        return {'ok': False, 'error': 'fetch_failed', 'detail': f'HTTP {resp.status_code}'}

    content_type = resp.headers.get('Content-Type', '')
    if 'pdf' in content_type.lower() or resp.content[:4] == b'%PDF':
        try:
            text_parts = []
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                for page in pdf.pages:
                    text_parts.append(page.extract_text() or '')
            text = '\n'.join(text_parts)
        except Exception as exc:
            return {'ok': False, 'error': 'pdf_parse_failed', 'detail': str(exc)}
    else:
        # HTML/plain text receipt — strip tags crudely and normalize whitespace.
        text = re.sub(r'<[^>]+>', ' ', resp.text)
        text = re.sub(r'&nbsp;', ' ', text)

    text = re.sub(r'[ \t]+', ' ', text)
    return {'ok': True, 'text': text}


def _parse_cbe_receipt_text(text):
    fields = {}
    for key, patterns in CBE_FIELD_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                fields[key] = match.group(1).strip()
                break
    fields['raw_text'] = text.strip()[:4000]
    return fields


def _get_payment_config():
    conn = get_conn()
    row = conn.execute("SELECT data FROM menu WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {}
    return json.loads(row['data']).get('payment', {}) or {}


def verify_cbe_payment(order_total, payment_screenshot):
    """Best-effort auto-verification of a CBE payment screenshot.

    Returns a dict: { status, reason, qrUrl, fields }
    status is one of:
      no_screenshot | no_qr | qr_not_cbe | fetch_failed | parse_failed |
      verified | review | mismatch
    This never raises — callers should treat any exception here as
    'error' and still let the order go through, since auto-verification
    is a convenience on top of manual admin review, not a gate.
    """
    result = {'status': 'no_screenshot', 'reason': '', 'qrUrl': None, 'fields': {}}

    if not payment_screenshot:
        result['reason'] = 'No payment screenshot was uploaded.'
        return result

    qr_value = _decode_qr_from_data_url(payment_screenshot)
    if not qr_value:
        result['status'] = 'no_qr'
        result['reason'] = 'Could not find a readable QR code in the screenshot.'
        return result
    result['qrUrl'] = qr_value

    fetched = _fetch_cbe_receipt(qr_value)
    if not fetched['ok']:
        if fetched['error'] == 'qr_not_cbe':
            result['status'] = 'qr_not_cbe'
            result['reason'] = 'The QR code does not point to a CBE verification page.'
        else:
            result['status'] = 'fetch_failed'
            result['reason'] = f"Could not fetch the CBE receipt ({fetched.get('detail', fetched['error'])})."
        return result

    fields = _parse_cbe_receipt_text(fetched['text'])
    result['fields'] = fields

    if not fields.get('amount'):
        result['status'] = 'parse_failed'
        result['reason'] = 'Fetched the CBE receipt but could not read the transaction details from it.'
        return result

    try:
        receipt_amount = float(fields['amount'].replace(',', ''))
    except (TypeError, ValueError):
        receipt_amount = None

    amount_ok = receipt_amount is not None and abs(receipt_amount - float(order_total)) < 1.0

    payment_cfg = _get_payment_config()
    expected_name = (payment_cfg.get('accountName') or '').strip().lower()
    expected_number = re.sub(r'\D', '', payment_cfg.get('accountNumber') or '')

    receiver_name = (fields.get('receiver') or '').strip().lower()
    receiver_number = re.sub(r'\D', '', fields.get('receiver_account') or '')

    name_ok = bool(expected_name) and bool(receiver_name) and expected_name in receiver_name
    number_ok = bool(expected_number) and bool(receiver_number) and expected_number[-6:] == receiver_number[-6:]

    if amount_ok and (name_ok or number_ok):
        result['status'] = 'verified'
        result['reason'] = 'Amount and receiving account matched the order and configured payment details.'
    elif amount_ok:
        result['status'] = 'review'
        result['reason'] = 'Amount matches the order, but the receiving name/account on the receipt could not be confirmed — please double check.'
    else:
        result['status'] = 'mismatch'
        got = fields.get('amount', '?')
        result['reason'] = f'Amount on the CBE receipt ({got}) does not match the order total ({order_total}).'

    return result


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

    # Best-effort auto-verification via the CBE QR code. Never let a failure
    # here block the order — this is a convenience layer for the admin, not
    # a payment gateway.
    try:
        verification = verify_cbe_payment(total, payment_screenshot)
    except Exception as exc:
        verification = {'status': 'error', 'reason': str(exc), 'qrUrl': None, 'fields': {}}

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO orders (customer_name, phone, items, total, status, created_at, payment_screenshot, "
        "verification_status, verification_data) VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?)",
        (
            name, phone, json.dumps(items), total,
            datetime.now(timezone.utc).isoformat(), payment_screenshot,
            verification['status'], json.dumps(verification),
        ),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return jsonify({
        'ok': True,
        'orderId': order_id,
        'total': total,
        'verificationStatus': verification['status'],
        'verificationReason': verification['reason'],
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
            'name': r['customer_name'],
            'phone': r['phone'],
            'items': json.loads(r['items']),
            'total': r['total'],
            'status': r['status'],
            'createdAt': r['created_at'],
            'paymentScreenshot': r['payment_screenshot'],
            'verificationStatus': r['verification_status'],
            'verificationData': json.loads(r['verification_data']) if r['verification_data'] else None,
        }
        for r in rows
    ]
    return jsonify(orders)


@app.route('/api/orders/<int:order_id>/verify', methods=['POST'])
def reverify_order(order_id):
    """Re-run CBE QR auto-verification for an existing order (e.g. after a
    transient network failure, or if the admin swapped in a corrected
    screenshot)."""
    pin = request.headers.get('X-Admin-Pin', '')
    if pin != ADMIN_PIN:
        return jsonify({'error': 'unauthorized'}), 401

    conn = get_conn()
    row = conn.execute("SELECT total, payment_screenshot FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({'error': 'order not found'}), 404

    try:
        verification = verify_cbe_payment(row['total'], row['payment_screenshot'])
    except Exception as exc:
        verification = {'status': 'error', 'reason': str(exc), 'qrUrl': None, 'fields': {}}

    conn.execute(
        "UPDATE orders SET verification_status = ?, verification_data = ? WHERE id = ?",
        (verification['status'], json.dumps(verification), order_id),
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'verificationStatus': verification['status'], 'verificationData': verification})


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
