import os
import requests
import certifi
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import pymongo
from bson.objectid import ObjectId
import resend
import razorpay

#dummy comment to trigger redeploy
# Load .env from project root (parent of web/) so SMTP and MONGO_URL are always found
_env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(_env_path, override=True)
app = Flask(__name__)
app.secret_key = "your_secret_key_here" # Required for sessions

# Configure uploads folder
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Create uploads folder if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# Initialize Razorpay Client
razorpay_client = None
razorpay_key_id = os.getenv("RAZORPAY_KEY_ID")
razorpay_key_secret = os.getenv("RAZORPAY_KEY_SECRET")
if razorpay_key_id and razorpay_key_secret:
    razorpay_client = razorpay.Client(auth=(razorpay_key_id, razorpay_key_secret))

uri = os.getenv("MONGO_URL")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where())
db = client["online_store"]
USERS_COLLECTION = db["users"]
ADMIN_COLLECTION = db["admin"]
ORDER_COLLECTION = db["order"]
STATUS_COLLECTION = db["status"]
ADMIN_APPLICATIONS_COLLECTION = db["admin_applications"]

# Login Manager Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_data, account_type='customer'):
        if not user_data or '_id' not in user_data:
            raise ValueError("Invalid user data: missing _id field")
        self.id = str(user_data['_id'])
        self.account_type = 'admin' if account_type == 'admin' else 'customer'
        self.username = user_data.get('username')
        self.role = 'admin' if self.account_type == 'admin' else 'customer'

    def get_id(self):
        return f"{self.account_type}:{self.id}"


def _coerce_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _get_account_collection(account_type):
    return ADMIN_COLLECTION if account_type == 'admin' else USERS_COLLECTION


def _find_account_by_login(identifier):
    lookup = (identifier or '').strip()
    if not lookup:
        return None, None

    query = {
        "$or": [
            {"username": lookup},
            {"email": lookup.lower()}
        ]
    }

    admin_data = ADMIN_COLLECTION.find_one(query)
    if admin_data:
        return admin_data, 'admin'

    user_data = USERS_COLLECTION.find_one(query)
    if user_data:
        return user_data, 'customer'

    return None, None


def _find_account_by_username(username, account_type=None):
    lookup = (username or '').strip()
    if not lookup:
        return None, None

    if account_type in {'admin', 'customer'}:
        account_data = _get_account_collection(account_type).find_one({"username": lookup})
        return account_data, account_type

    admin_data = ADMIN_COLLECTION.find_one({"username": lookup})
    if admin_data:
        return admin_data, 'admin'

    user_data = USERS_COLLECTION.find_one({"username": lookup})
    if user_data:
        return user_data, 'customer'

    return None, None

@login_manager.user_loader
def load_user(user_id):
    if not user_id:
        return None

    account_type = 'customer'
    raw_user_id = user_id

    if ':' in user_id:
        account_type, raw_user_id = user_id.split(':', 1)

    object_id = _coerce_object_id(raw_user_id)
    if not object_id:
        return None

    collection = _get_account_collection(account_type)
    user_data = collection.find_one({"_id": object_id})

    if not user_data and account_type == 'customer':
        user_data = ADMIN_COLLECTION.find_one({"_id": object_id})
        if user_data:
            account_type = 'admin'

    if not user_data and account_type == 'admin':
        user_data = USERS_COLLECTION.find_one({"_id": object_id})
        if user_data:
            account_type = 'customer'

    return User(user_data, account_type) if user_data else None


def _get_guest_cart():
    return session.get('guest_cart', [])


def _save_guest_cart(cart_ids):
    session['guest_cart'] = cart_ids
    session.modified = True


def _merge_guest_cart_into_user(user_id):
    guest_cart_ids = _get_guest_cart()
    if not guest_cart_ids:
        return

    object_ids = []
    for pid in guest_cart_ids:
        try:
            object_ids.append(ObjectId(pid))
        except Exception:
            continue

    if object_ids:
        USERS_COLLECTION.update_one(
            {"_id": _coerce_object_id(user_id)},
            {"$push": {"cart": {"$each": object_ids}}}
        )

    session.pop('guest_cart', None)
    session.modified = True


def _allowed_file(filename):
    """Check if the file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _save_uploaded_file(file):
    """Save uploaded file and return the filename/URL."""
    if not file or file.filename == '':
        return None
    
    if not _allowed_file(file.filename):
        return None
    
    # Create a secure filename
    filename = secure_filename(file.filename)
    # Add timestamp to prevent filename conflicts
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
    filename = timestamp + filename
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    # Return the URL path for the image
    return f"/uploads/{filename}"


def _get_post_login_redirect():
    next_url = request.args.get('next') or request.form.get('next')
    if next_url and next_url.startswith('/'):
        return next_url
    return url_for('index')


def generate_otp_code() -> str:
    return f"{random.randint(100000, 999999)}"


def _normalize_datetime(value):
    """Convert mixed datetime representations from Mongo into a naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept both ISO strings with Z and plain ISO strings.
        text = text.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    # Keep comparisons consistent with datetime.utcnow() used in this file.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


ORDER_STATUS_LABELS = {
    "placed": "Placed",
    "on_the_way": "On The Way",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}

ORDER_STATUS_ICONS = {
    "placed": "fa-receipt",
    "on_the_way": "fa-truck-fast",
    "delivered": "fa-circle-check",
    "cancelled": "fa-ban",
}

ORDER_STATUS_BADGE_CLASSES = {
    "placed": "status-placed",
    "on_the_way": "status-on-the-way",
    "delivered": "status-delivered",
    "cancelled": "status-cancelled",
}

ORDER_STATUS_ALIASES = {
    "pending": "placed",
    "processing": "placed",
    "confirmed": "placed",
    "shipped": "on_the_way",
    "shipping": "on_the_way",
    "in_transit": "on_the_way",
    "completed": "delivered",
}

ORDER_STATUS_FLOW = ("placed", "on_the_way", "delivered", "cancelled")


def _normalize_order_status(status):
    if status is None:
        return "placed"

    key = str(status).strip().lower().replace('-', '_').replace(' ', '_')
    key = ORDER_STATUS_ALIASES.get(key, key)
    if key not in ORDER_STATUS_LABELS:
        return "placed"
    return key


def _prepare_order_for_display(order):
    prepared_order = dict(order)
    normalized_status = _normalize_order_status(prepared_order.get('status'))
    prepared_order['status'] = normalized_status
    prepared_order['status_label'] = ORDER_STATUS_LABELS[normalized_status]
    prepared_order['status_icon'] = ORDER_STATUS_ICONS[normalized_status]
    prepared_order['status_badge_class'] = ORDER_STATUS_BADGE_CLASSES[normalized_status]
    prepared_order['order_date'] = _normalize_datetime(prepared_order.get('order_date'))

    item_count = 0
    for item in prepared_order.get('items', []) or []:
        try:
            item_count += int(item.get('quantity', 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    prepared_order['item_count'] = item_count
    return prepared_order


def _get_status_map(order_ids):
    normalized_ids = [oid for oid in (_coerce_object_id(value) for value in (order_ids or [])) if oid]
    if not normalized_ids:
        return {}

    status_docs = list(STATUS_COLLECTION.find({"order_id": {"$in": normalized_ids}}))
    return {
        str(status_doc.get('order_id')): status_doc
        for status_doc in status_docs
        if status_doc.get('order_id')
    }


def _sort_orders_for_display(orders, status_map=None):
    prepared_orders = []
    for order in (orders or []):
        prepared_order = dict(order)
        status_doc = None
        if status_map and prepared_order.get('_id'):
            status_doc = status_map.get(str(prepared_order['_id']))

        if status_doc:
            prepared_order['status'] = status_doc.get('current_status')
            prepared_order['status_updated_at'] = status_doc.get('updated_at')
            prepared_order['status_history'] = status_doc.get('history', [])

        prepared_orders.append(_prepare_order_for_display(prepared_order))

    return sorted(
        prepared_orders,
        key=lambda order: order.get('order_date') or datetime.min,
        reverse=True,
    )


def _get_orders_for_users(user_ids):
    normalized_user_ids = [oid for oid in (_coerce_object_id(value) for value in (user_ids or [])) if oid]
    if not normalized_user_ids:
        return {}

    order_docs = list(ORDER_COLLECTION.find({"user_id": {"$in": normalized_user_ids}}))
    status_map = _get_status_map([order.get('_id') for order in order_docs])
    orders_by_user = {}

    for order in _sort_orders_for_display(order_docs, status_map):
        user_id = order.get('user_id')
        if not user_id:
            continue
        orders_by_user.setdefault(str(user_id), []).append(order)

    return orders_by_user


def _build_orders_dashboard():
    order_docs = list(ORDER_COLLECTION.find({}))
    status_map = _get_status_map([order.get('_id') for order in order_docs])
    orders = _sort_orders_for_display(order_docs, status_map)
    active_orders = [
        order for order in orders
        if order.get('status') in {'placed', 'on_the_way'}
    ]

    summary = {
        "total_orders": 0,
        "placed_orders": 0,
        "on_the_way_orders": 0,
        "delivered_orders": 0,
        "cancelled_orders": 0,
        "total_revenue": 0,
    }

    for order in active_orders:
        summary['total_orders'] += 1
        summary[f"{order['status']}_orders"] += 1
        try:
            summary['total_revenue'] += float(order.get('total_amount', 0) or 0)
        except (TypeError, ValueError):
            continue

    return active_orders, summary


def _build_customers_info():
    customers = list(USERS_COLLECTION.find({}, {"username": 1, "email": 1}))
    customer_ids = [customer.get('_id') for customer in customers if customer.get('_id')]

    orders_by_customer = {}
    if customer_ids:
        pipeline = [
            {"$match": {"user_id": {"$in": customer_ids}}},
            {"$group": {"_id": "$user_id", "order_count": {"$sum": 1}}},
        ]
        for item in ORDER_COLLECTION.aggregate(pipeline):
            orders_by_customer[str(item.get('_id'))] = int(item.get('order_count', 0) or 0)

    customer_rows = []
    for customer in customers:
        customer_rows.append(
            {
                "name": customer.get('username') or 'N/A',
                "email": customer.get('email') or 'N/A',
                "order_count": orders_by_customer.get(str(customer.get('_id')), 0),
            }
        )

    customer_rows.sort(key=lambda row: (-row.get('order_count', 0), row.get('name', '').lower()))

    summary = {
        "total_customers": len(customer_rows),
        "total_orders": sum(row.get('order_count', 0) for row in customer_rows),
    }

    return customer_rows, summary


@app.template_filter('datetime_display')
def datetime_display(value):
    display_value = _normalize_datetime(value)
    if not display_value:
        return 'N/A'
    return display_value.strftime('%d %b %Y, %I:%M %p')


def _ensure_normalized_collections():
    USERS_COLLECTION.create_index('username')
    USERS_COLLECTION.create_index('email')
    ADMIN_COLLECTION.create_index('username')
    ADMIN_COLLECTION.create_index('email')
    ORDER_COLLECTION.create_index([('user_id', pymongo.ASCENDING), ('order_date', pymongo.DESCENDING)])
    STATUS_COLLECTION.create_index('order_id', unique=True)
    STATUS_COLLECTION.create_index('user_id')


def _migrate_legacy_admins():
    legacy_admins = list(USERS_COLLECTION.find({"role": "admin"}))
    for legacy_admin in legacy_admins:
        admin_doc = dict(legacy_admin)
        admin_doc['role'] = 'admin'
        admin_doc.pop('orders', None)

        ADMIN_COLLECTION.replace_one(
            {"_id": admin_doc['_id']},
            admin_doc,
            upsert=True,
        )
        USERS_COLLECTION.delete_one({"_id": admin_doc['_id']})


def _migrate_legacy_orders():
    legacy_customers = list(USERS_COLLECTION.find(
        {"orders": {"$exists": True, "$type": "array", "$ne": []}},
        {"username": 1, "email": 1, "orders": 1},
    ))

    for customer in legacy_customers:
        customer_id = customer.get('_id')
        for legacy_order in customer.get('orders', []):
            if not isinstance(legacy_order, dict):
                continue

            order_id = _coerce_object_id(legacy_order.get('_id')) or ObjectId()
            order_date = _normalize_datetime(legacy_order.get('order_date')) or datetime.utcnow()
            current_status = _normalize_order_status(legacy_order.get('status'))
            status_updated_at = _normalize_datetime(legacy_order.get('status_updated_at')) or order_date

            order_doc = {
                "_id": order_id,
                "user_id": customer_id,
                "customer_name": customer.get('username', ''),
                "customer_email": customer.get('email', ''),
                "order_date": order_date,
                "items": legacy_order.get('items', []),
                "total_amount": legacy_order.get('total_amount', 0),
            }

            ORDER_COLLECTION.update_one(
                {"_id": order_id},
                {"$setOnInsert": order_doc},
                upsert=True,
            )

            STATUS_COLLECTION.update_one(
                {"order_id": order_id},
                {
                    "$setOnInsert": {
                        "order_id": order_id,
                        "user_id": customer_id,
                        "current_status": current_status,
                        "updated_at": status_updated_at,
                        "history": [{"status": current_status, "updated_at": status_updated_at}],
                    }
                },
                upsert=True,
            )

        USERS_COLLECTION.update_one({"_id": customer_id}, {"$unset": {"orders": ""}})


def _initialize_data_model():
    try:
        _ensure_normalized_collections()
        _migrate_legacy_admins()
        _migrate_legacy_orders()
    except Exception as exc:
        app.logger.exception(f"Failed to initialize normalized MongoDB collections: {exc}")


_initialize_data_model()


def _current_user_object_id():
    if not getattr(current_user, 'is_authenticated', False):
        return None
    raw_user_id = str(current_user.id).split(':', 1)[-1]
    return _coerce_object_id(raw_user_id)


def _is_product_in_wishlist(product_id):
    """Check if a product is in the current user's wishlist."""
    if not current_user.is_authenticated or current_user.role != 'customer':
        return False
    
    user_id = _current_user_object_id()
    product_oid = _coerce_object_id(product_id)
    
    if not user_id or not product_oid:
        return False
    
    wishlist_doc = WISHLIST_COLLECTION.find_one({"user_id": user_id})
    if not wishlist_doc:
        return False
    
    items = wishlist_doc.get('items', [])
    return any(str(item) == str(product_oid) for item in items)


def _upsert_order_status(order_id, user_id, status, updated_at=None):
    normalized_status = _normalize_order_status(status)
    timestamp = _normalize_datetime(updated_at) or datetime.utcnow()
    order_object_id = _coerce_object_id(order_id)
    user_object_id = _coerce_object_id(user_id)
    if not order_object_id or not user_object_id:
        return None

    existing_status = STATUS_COLLECTION.find_one({"order_id": order_object_id})
    history = []
    if isinstance(existing_status, dict):
        history = list(existing_status.get('history', []))

    if not history or history[-1].get('status') != normalized_status:
        history.append({"status": normalized_status, "updated_at": timestamp})

    STATUS_COLLECTION.update_one(
        {"order_id": order_object_id},
        {
            "$set": {
                "user_id": user_object_id,
                "current_status": normalized_status,
                "updated_at": timestamp,
                "history": history,
            }
        },
        upsert=True,
    )
    return normalized_status


def _get_orders_for_user(user_id):
    user_object_id = _coerce_object_id(user_id)
    if not user_object_id:
        return []
    orders = list(ORDER_COLLECTION.find({"user_id": user_object_id}))
    status_map = _get_status_map([order.get('_id') for order in orders])
    return _sort_orders_for_display(orders, status_map)


def _get_smtp_config():
    """Load and normalize SMTP config from environment. Strips whitespace and optional quotes."""
    def _s(v):
        if v is None:
            return None
        s = str(v).strip().strip('"').strip("'")
        return s if s else None

    return {
        "host": _s(os.getenv("SMTP_HOST")) or "smtp.gmail.com",
        "port": int(_s(os.getenv("SMTP_PORT")) or "587"),
        "user": _s(os.getenv("SMTP_USER")),
        "password": _s(os.getenv("SMTP_PASSWORD")),
        "sender": _s(os.getenv("EMAIL_SENDER")) or _s(os.getenv("SMTP_USER")),
    }


def mask_email(email: str) -> str:
    """
    Return a privacy-friendly version of the email, e.g.
    'jo****@gmail.com' or 'a****@domain.com'.
    """
    if not email:
        return ""
    email = email.strip()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "****"
    else:
        masked_local = local[:2] + "****"
    return f"{masked_local}@{domain}"


def _sanitize_env_value(value):
    if value is None:
        return None
    cleaned = str(value).strip().strip('"').strip("'")
    return cleaned if cleaned else None

def _get_resend_config():
    # Render Dashboard variables override everything else
    sender_email = os.getenv("RESEND_FROM_EMAIL") or os.getenv("EMAIL_SENDER")
    sender_name = os.getenv("RESEND_FROM_NAME") or "Organic Pulse"
    
    return {
        "api_key": os.getenv("RESEND_API_KEY"),
        "from_field": f"{sender_name} <{sender_email}>"
    }


def _send_otp_via_resend(to_email: str, otp_code: str) -> bool:
    config = _get_resend_config()
    api_key = config["api_key"]
    # Change: Force the sandbox email for the testing phase
    sender_email = "onboarding@resend.dev" 
    recipient_email = (to_email or "").strip().lower()

    if not api_key:
        app.logger.warning("RESEND_API_KEY not found in environment. Skipping Resend.")
        return False

    url = "https://api.resend.com/emails"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        # ADD THIS LINE: Explicit User-Agent helps bypass some 403 filters
        "User-Agent": "python-requests/flask-app" 
    }
    
    data = {
        "from": "Mota Anaj <auth@motaanaj.com>", # MUST be this exact string in sandbox
        "to": [recipient_email],         # MUST be your registered Resend email
        "subject": "Your QuickStore Verification Code",
        "html": f"<h3>Welcome!</h3><p>Your code is: <strong>{otp_code}</strong></p>"
    }

    try:
        # Added a check to see if we are trying to send to a non-sandbox email
        response = requests.post(url, headers=headers, json=data, timeout=15)
        
        # LOGGING: This is crucial for Render. Check your Render "Logs" tab for this!
        app.logger.info("Resend Status: %s | Response: %s", response.status_code, response.text)
        
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        app.logger.error("Resend API Error: %s", e)
        return False


def _send_otp_via_smtp(to_email: str, otp_code: str) -> bool:
    smtp = _get_smtp_config()
    recipient_email = (to_email or "").strip().lower()

    if not recipient_email or "@" not in recipient_email:
        app.logger.error("Recipient email is missing or invalid for SMTP OTP send.")
        return False

    required = [smtp.get("host"), smtp.get("port"), smtp.get("user"), smtp.get("password"), smtp.get("sender")]
    if not all(required):
        app.logger.error("SMTP settings are incomplete. Check SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/EMAIL_SENDER.")
        return False

    msg = MIMEText(
        f"Welcome!\n\nYour verification code is: {otp_code}\n\nThis code expires in 10 minutes.",
        "plain",
        "utf-8"
    )
    msg["Subject"] = "Your QuickStore Verification Code"
    msg["From"] = smtp["sender"]
    msg["To"] = recipient_email

    try:
        port = int(smtp["port"])
        host = smtp["host"]
        user = smtp["user"]
        password = smtp["password"]

        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(user, password)
                server.sendmail(smtp["sender"], [recipient_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(user, password)
                server.sendmail(smtp["sender"], [recipient_email], msg.as_string())

        app.logger.info("OTP email sent via SMTP to %s", recipient_email)
        return True
    except Exception as e:
        app.logger.error("Failed to send OTP email via SMTP: %s", e)
        return False


def send_otp_email(to_email: str, otp_code: str) -> bool:
    if _send_otp_via_resend(to_email, otp_code):
        return True

    app.logger.info("Falling back to SMTP for OTP email delivery.")
    return _send_otp_via_smtp(to_email, otp_code)

# --- SESSION HELPERS ---
@app.before_request
def _clear_guest_cart_on_new_session():
    """Ensure guest cart does not persist across distinct browser sessions.

    Flask sets `session.new` to True when no valid session cookie is sent by the
    client, which happens when the user opens a fresh browser session or the
    cookie has expired/been cleared.  When that occurs we remove any existing
    ``guest_cart`` data so anonymous visitors always start with an empty cart
    unless they actively add items during that session.
    """
    # only act for anonymous users; authenticated carts are stored on the user
    if not current_user.is_authenticated:
        if session.new and 'guest_cart' in session:
            session.pop('guest_cart', None)
            session.modified = True


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serve uploaded product images."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# --- PUBLIC ROUTES ---
@app.route('/')
def index():
    products = list(db.catalog.find())
    # Get wishlist status for each product
    wishlist_data = {}
    if current_user.is_authenticated and current_user.role == 'customer':
        user_id = _current_user_object_id()
        wishlist_doc = WISHLIST_COLLECTION.find_one({"user_id": user_id})
        if wishlist_doc:
            for item_id in wishlist_doc.get('items', []):
                wishlist_data[str(item_id)] = True
    
    return render_template('index.html', products=products, wishlist_data=wishlist_data)

@app.route('/product/<id>')
def product_detail(id):
    product = db.catalog.find_one({"_id": ObjectId(id)})
    is_in_wishlist = _is_product_in_wishlist(id) if product else False
    return render_template('product_detail.html', product=product, is_in_wishlist=is_in_wishlist)

# --- AUTH ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    # determine where to send the user after they'll eventually log in
    post_login_redirect = _get_post_login_redirect()

    # if user is merely viewing the form (GET) and we have a "next" hint,
    # show a contextual message so they understand where they'll end up.
    if request.method == 'GET':
        next_hint = request.args.get('next', '')
        if next_hint.startswith('/cart') or next_hint.startswith('/checkout'):
            flash('You need to sign in before accessing your cart.')

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password')

        if not username or not isinstance(password, str) or not password.strip():
            flash('Please enter both username and password.')
            return redirect(url_for('login'))

        password = password.strip()
        
        user_data, account_type = _find_account_by_login(username)
        
        if user_data:
            stored_password = user_data.get('password')

            if isinstance(stored_password, bytes):
                stored_password = stored_password.decode('utf-8', errors='ignore')
            elif stored_password is None:
                stored_password = ''
            else:
                stored_password = str(stored_password)

            try:
                password_valid = bool(stored_password) and check_password_hash(stored_password, password)
            except (ValueError, TypeError):
                app.logger.exception("Invalid password format for user '%s'", user_data.get('username'))
                password_valid = False

            if not password_valid:
                flash('Invalid password for existing user.')
                return redirect(url_for('login'))

            # Always send OTP for sign-in
            email = (user_data.get('email') or '').strip().lower()
            if not email:
                flash('No email address is associated with this account. Please register again.')
                return redirect(url_for('register'))

            otp_code = generate_otp_code()
            expires_at = datetime.utcnow() + timedelta(minutes=10)
            _get_account_collection(account_type).update_one(
                {"_id": user_data['_id']},
                {"$set": {"otp_code": otp_code, "otp_expires_at": expires_at}}
            )
            if send_otp_email(email, otp_code):
                masked = mask_email(email)
                flash(f'We have emailed a verification code to {masked}. Enter it to sign in.', 'success')
            else:
                flash('We could not send the verification email. Please try again later or contact support.', 'error')
            # propagate the redirect target through the verification step
            return redirect(url_for('verify_otp', username=user_data.get('username'), account_type=account_type, next=post_login_redirect))
        else:
            flash('No account found with that username. Please register first.')
            return redirect(url_for('register'))
            
    return render_template('login.html')
@app.route('/logout')
@login_required
def logout():
    # remove any guest cart that might be lingering in the session; after a
    # logout we want a truly fresh anonymous shopping experience.
    session.pop('guest_cart', None)
    logout_user()
    return redirect(url_for('index'))

# --- DIAGNOSTIC ROUTES (for debugging email config) ---
@app.route('/debug/resend-test', methods=['GET', 'POST'])
@app.route('/debug/email-test', methods=['GET', 'POST'])
def debug_resend_test():
    """Diagnostic endpoint: Test Resend config without authentication.
    
    GET: Show config status (API key length, sender email, etc.)
    POST: Send test email to verify Resend connectivity
    """
    config = _get_resend_config()
    result = {
        "api_key_set": bool(config["api_key"]),
        "api_key_length": len(config["api_key"]) if config["api_key"] else 0,
        "sender_email": config["sender_email"],
        "sender_name": config["sender_name"],
        "has_valid_sender": bool(config["sender_email"] and "@" in config["sender_email"]),
    }

    if request.method == 'POST':
        test_email = (request.form.get('test_email') or '').strip().lower()
        test_code = "123456"
        
        if not test_email or '@' not in test_email:
            result["error"] = "Invalid test email provided"
            return jsonify(result), 400

        # Attempt send and capture details
        try:
            url = "https://api.resend.com/emails"
            headers = {
                "accept": "application/json",
                "Authorization": f"Bearer {config['api_key']}",
                "content-type": "application/json"
            }
            data = {
                "from": config["from_field"],
                "to": [test_email],
                "subject": "Test OTP from Organic Pulse",
                "html": f"<h3>Test Email</h3><p>Test code: <strong>{test_code}</strong></p>"
            }
            response = requests.post(url, headers=headers, json=data, timeout=15)
            
            result["request_sent"] = True
            result["status_code"] = response.status_code
            result["success"] = 200 <= response.status_code < 300
            
            if not result["success"]:
                result["error_response"] = response.text[:500]
            else:
                result["message"] = "Test email sent successfully"
                
        except Exception as e:
            result["request_sent"] = False
            result["error"] = str(e)

    return jsonify(result)


# --- ADMIN APPLICATIONS HELPER ---
def _build_admin_applications_dashboard():
    """Fetch pending admin applications with user details."""
    applications = list(ADMIN_APPLICATIONS_COLLECTION.find({"status": "pending"}).sort("applied_at", -1))
    
    # Enrich applications with user data
    enriched_apps = []
    for app in applications:
        user_id = _coerce_object_id(app.get('user_id'))
        user_data = USERS_COLLECTION.find_one({"_id": user_id}) if user_id else None
        
        if user_data:
            enriched_apps.append({
                "_id": app['_id'],
                "user_id": user_id,
                "username": user_data.get('username', 'N/A'),
                "email": user_data.get('email', 'N/A'),
                "reason": app.get('reason', ''),
                "applied_at": app.get('applied_at', datetime.utcnow()),
                "status": app.get('status', 'pending'),
            })
    
    return enriched_apps


def _has_pending_application(user_id):
    """Check if a user has a pending application."""
    user_object_id = _coerce_object_id(user_id)
    if not user_object_id:
        return False
    return bool(ADMIN_APPLICATIONS_COLLECTION.find_one({
        "user_id": user_object_id,
        "status": "pending"
    }))


# --- ADMIN ROUTES ---
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return "Access Denied", 403
    products = list(db.catalog.find())
    active_tab = request.args.get('tab', 'inventory')
    if active_tab not in {'inventory', 'orders', 'applications'}:
        active_tab = 'inventory'

    orders, order_metrics = _build_orders_dashboard()
    applications = _build_admin_applications_dashboard()
    
    status_options = [
        {"value": status, "label": ORDER_STATUS_LABELS[status]}
        for status in ORDER_STATUS_FLOW
    ]
    return render_template(
        'admin_dashboard.html',
        products=products,
        orders=orders,
        order_metrics=order_metrics,
        applications=applications,
        status_options=status_options,
        active_tab=active_tab,
    )


@app.route('/admin/orders/<order_id>/status', methods=['POST'])
@login_required
def update_order_status(order_id):
    if current_user.role != 'admin':
        return "Access Denied", 403

    order_object_id = _coerce_object_id(order_id)
    new_status = _normalize_order_status(request.form.get('status'))

    if not order_object_id:
        flash('Invalid order reference.', 'error')
        return redirect(url_for('admin_dashboard', tab='orders'))

    try:
        order_exists = ORDER_COLLECTION.find_one({"_id": order_object_id}, {"_id": 1, "user_id": 1})
        if not order_exists:
            flash('Order not found.', 'warning')
            return redirect(url_for('admin_dashboard', tab='orders'))

        user_object_id = _coerce_object_id(order_exists.get('user_id'))
        if not user_object_id:
            flash('Order user reference is invalid.', 'error')
            return redirect(url_for('admin_dashboard', tab='orders'))

        _upsert_order_status(order_object_id, user_object_id, new_status, datetime.utcnow())
    except Exception as exc:
        app.logger.error(f"Error updating order status: {exc}")
        flash('Could not update that order status.', 'error')
        return redirect(url_for('admin_dashboard', tab='orders'))

    flash(f"Order status updated to {ORDER_STATUS_LABELS[new_status]}.", 'success')

    return redirect(url_for('admin_dashboard', tab='orders'))


@app.route('/admin/customers-info')
@login_required
def customers_info():
    if current_user.role != 'admin':
        return "Access Denied", 403

    customers, customers_summary = _build_customers_info()
    return render_template(
        'customers_info.html',
        customers=customers,
        customers_summary=customers_summary,
    )

@app.route('/admin/users')
@login_required
def manage_users():
    if current_user.role != 'admin':
        return "Access Denied", 403
    all_users = list(USERS_COLLECTION.find())
    return render_template('manage_users.html', users=all_users)

@app.route('/admin/make_admin/<user_id>')
@login_required
def make_admin(user_id):
    if current_user.role == 'admin':
        user_doc = USERS_COLLECTION.find_one({"_id": _coerce_object_id(user_id)})
        if user_doc:
            admin_doc = dict(user_doc)
            admin_doc['role'] = 'admin'
            ADMIN_COLLECTION.replace_one({"_id": admin_doc['_id']}, admin_doc, upsert=True)
            USERS_COLLECTION.delete_one({"_id": admin_doc['_id']})
            flash("User promoted to Admin")
    return redirect(url_for('manage_users'))

@app.route('/apply-for-admin', methods=['GET', 'POST'])
@login_required
def apply_for_admin():
    """Allow customers to apply to become admin."""
    if current_user.role != 'customer':
        flash("Only customers can apply to become admin.", 'warning')
        return redirect(url_for('index'))
    
    user_id = _coerce_object_id(current_user.id)
    if not user_id:
        flash("Invalid user session.", 'error')
        return redirect(url_for('index'))
    
    # Check if already has pending application
    if _has_pending_application(user_id):
        flash("You already have a pending admin application.", 'warning')
        return redirect(url_for('index'))
    
    # Check if already an admin (shouldn't happen, but safety check)
    if current_user.role == 'admin':
        flash("You are already an admin.", 'info')
        return redirect(url_for('admin_dashboard'))
    
    if request.method == 'GET':
        return render_template('apply_for_admin.html')
    
    # POST: Submit application
    reason = (request.form.get('reason') or '').strip()
    
    if not reason or len(reason) < 10:
        flash("Please provide a reason of at least 10 characters.", 'error')
        return render_template('apply_for_admin.html')
    
    try:
        application = {
            "_id": ObjectId(),
            "user_id": user_id,
            "reason": reason,
            "status": "pending",
            "applied_at": datetime.utcnow(),
            "reviewed_at": None,
            "reviewed_by": None,
            "decision": None,
        }
        ADMIN_APPLICATIONS_COLLECTION.insert_one(application)
        flash("Your admin application has been submitted! Admins will review it shortly.", 'success')
        return redirect(url_for('index'))
    except Exception as e:
        app.logger.error(f"Error submitting admin application: {e}")
        flash("An error occurred while submitting your application. Please try again.", 'error')
        return render_template('apply_for_admin.html')

@app.route('/admin/application/<app_id>/approve', methods=['POST'])
@login_required
def approve_admin_application(app_id):
    """Approve an admin application and promote the user."""
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    app_object_id = _coerce_object_id(app_id)
    if not app_object_id:
        flash('Invalid application reference.', 'error')
        return redirect(url_for('admin_dashboard', tab='applications'))
    
    try:
        # Find the application
        application = ADMIN_APPLICATIONS_COLLECTION.find_one({"_id": app_object_id})
        if not application:
            flash('Application not found.', 'warning')
            return redirect(url_for('admin_dashboard', tab='applications'))
        
        if application['status'] != 'pending':
            flash('This application has already been processed.', 'info')
            return redirect(url_for('admin_dashboard', tab='applications'))
        
        # Find the user
        user_id = _coerce_object_id(application.get('user_id'))
        user_doc = USERS_COLLECTION.find_one({"_id": user_id})
        
        if not user_doc:
            flash('User not found.', 'error')
            return redirect(url_for('admin_dashboard', tab='applications'))
        
        # Promote user to admin
        admin_doc = dict(user_doc)
        admin_doc['role'] = 'admin'
        admin_doc['promoted_at'] = datetime.utcnow()
        
        ADMIN_COLLECTION.replace_one({"_id": admin_doc['_id']}, admin_doc, upsert=True)
        USERS_COLLECTION.delete_one({"_id": admin_doc['_id']})
        
        # Update application status
        ADMIN_APPLICATIONS_COLLECTION.update_one(
            {"_id": app_object_id},
            {
                "$set": {
                    "status": "approved",
                    "reviewed_at": datetime.utcnow(),
                    "reviewed_by": current_user.username,
                    "decision": "approved",
                }
            }
        )
        
        flash(f"User '{user_doc.get('username')}' has been promoted to admin!", 'success')
        
    except Exception as exc:
        app.logger.error(f"Error approving admin application: {exc}")
        flash('An error occurred while approving the application.', 'error')
    
    return redirect(url_for('admin_dashboard', tab='applications'))

@app.route('/admin/application/<app_id>/reject', methods=['POST'])
@login_required
def reject_admin_application(app_id):
    """Reject an admin application."""
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    app_object_id = _coerce_object_id(app_id)
    if not app_object_id:
        flash('Invalid application reference.', 'error')
        return redirect(url_for('admin_dashboard', tab='applications'))
    
    try:
        # Find the application
        application = ADMIN_APPLICATIONS_COLLECTION.find_one({"_id": app_object_id})
        if not application:
            flash('Application not found.', 'warning')
            return redirect(url_for('admin_dashboard', tab='applications'))
        
        if application['status'] != 'pending':
            flash('This application has already been processed.', 'info')
            return redirect(url_for('admin_dashboard', tab='applications'))
        
        # Update application status
        ADMIN_APPLICATIONS_COLLECTION.update_one(
            {"_id": app_object_id},
            {
                "$set": {
                    "status": "rejected",
                    "reviewed_at": datetime.utcnow(),
                    "reviewed_by": current_user.username,
                    "decision": "rejected",
                }
            }
        )
        
        flash('Admin application has been rejected.', 'success')
        
    except Exception as exc:
        app.logger.error(f"Error rejecting admin application: {exc}")
        flash('An error occurred while rejecting the application.', 'error')
    
    return redirect(url_for('admin_dashboard', tab='applications'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    # preserve next parameter so we can send the user back after verifying
    next_url = request.args.get('next') or request.form.get('next')

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Prevent duplicate usernames
        existing_user, _ = _find_account_by_username(username)
        if existing_user:
            flash('Username already exists!')
            return redirect(url_for('register'))

        if not email:
            flash('Please enter your email address for OTP verification.')
            return redirect(url_for('register'))

        email = email.strip().lower()
        otp_code = generate_otp_code()
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        sent = send_otp_email(email, otp_code)
        if not sent:
            flash(
                'We could not send the verification email. Please check your email address and try again, '
                'or contact support if the problem continues.', 'error'
            )
            return redirect(url_for('register'))

        data = {
            "username": username,
            "email": email,
            "password": generate_password_hash(password),
            "is_verified": False,
            "otp_code": otp_code,
            "otp_expires_at": expires_at,
            "cart": []
        }
        USERS_COLLECTION.insert_one(data)
        flash('We have sent a verification code to your email. Enter it to activate your account.', 'success')
        args = {"username": username}
        args["account_type"] = 'customer'
        if next_url:
            args["next"] = next_url
        return redirect(url_for('verify_otp', **args))
        
    return render_template('register.html')


@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    username = request.args.get('username') or request.form.get('username')
    account_type = request.args.get('account_type') or request.form.get('account_type')
    post_login_redirect = _get_post_login_redirect()

    if request.method == 'POST':
        otp_input = request.form.get('otp')
        username = request.form.get('username')
        account_type = request.form.get('account_type')

        user_data, account_type = _find_account_by_username(username, account_type)
        if not user_data:
            flash('User not found. Please register again.', 'error')
            return redirect(url_for('register'))

        stored_code = str(user_data.get('otp_code', '')).strip()
        expires_at = _normalize_datetime(user_data.get('otp_expires_at'))
        now_utc = datetime.utcnow()

        if not stored_code or not expires_at or expires_at < now_utc:
            new_code = generate_otp_code()
            new_expires = datetime.utcnow() + timedelta(minutes=10)
            _get_account_collection(account_type).update_one(
                {"_id": user_data['_id']},
                {"$set": {"otp_code": new_code, "otp_expires_at": new_expires}}
            )
            if send_otp_email(user_data.get('email') or '', new_code):
                flash('Your verification code expired. We have sent a new one to your email.', 'success')
            else:
                flash('We could not send the new code. Please check your email or try again later.', 'error')
            args = {"username": username, "account_type": account_type}
            if post_login_redirect:
                args["next"] = post_login_redirect
            return redirect(url_for('verify_otp', **args))

        if str(otp_input or '').strip() == str(stored_code).strip():
            _get_account_collection(account_type).update_one(
                {"_id": user_data['_id']},
                {
                    "$set": {"is_verified": True},
                    "$unset": {"otp_code": "", "otp_expires_at": ""}
                }
            )
            user_obj = User(user_data, account_type)
            login_user(user_obj)
            if account_type == 'customer':
                _merge_guest_cart_into_user(user_data['_id'])
            flash('Your account has been verified. Welcome to GreenFields Farm Shop!', 'success')
            if account_type == 'admin':
                return redirect(url_for('index'))
            return redirect(post_login_redirect)

        flash('Wrong OTP entered. Please try again, resend, or go back to login.', 'error')
        email_mask = mask_email(user_data.get('email') or '')
        return render_template('verify_otp.html', username=username, account_type=account_type, email_mask=email_mask, next_url=post_login_redirect)

    # GET: show the verify form and display which email we're sending codes to
    email_mask = ""
    if username:
        user_data, account_type = _find_account_by_username(username, account_type)
        if user_data and user_data.get('email'):
            email_mask = mask_email(user_data['email'])

    return render_template('verify_otp.html', username=username, account_type=account_type, email_mask=email_mask, next_url=post_login_redirect)

@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    username = request.form.get('username')
    account_type = request.form.get('account_type')
    post_login_redirect = _get_post_login_redirect()

    user_data, account_type = _find_account_by_username(username, account_type)
    if not user_data:
        flash('User not found.', 'error')
        return redirect(url_for('login'))

    email = user_data.get('email')
    if not email:
        flash('No email found.', 'error')
        return redirect(url_for('login'))

    otp_code = generate_otp_code()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    _get_account_collection(account_type).update_one(
        {"_id": user_data['_id']},
        {"$set": {"otp_code": otp_code, "otp_expires_at": expires_at}}
    )
    if send_otp_email(email, otp_code):
        flash('New OTP sent to your email.', 'success')
    else:
        flash('Failed to send OTP.', 'error')
    return redirect(url_for('verify_otp', username=username, account_type=account_type, next=post_login_redirect))

@app.route('/admin/edit/<id>', methods=['POST'])
@login_required
def edit_product(id):
    if current_user.role != 'admin':
        flash('Access Denied', 'error')
        return redirect(url_for('admin_dashboard'))
    
    try:
        # Get existing product to preserve image if not uploading new one
        existing_product = db.catalog.find_one({"_id": ObjectId(id)})
        if not existing_product:
            flash('Product not found', 'error')
            return redirect(url_for('admin_dashboard'))
        
        # Handle image upload or keep existing
        image_url = existing_product.get('image', "https://placehold.co/400x400?text=No+Image")
        if 'image_file' in request.files:
            image_file = request.files['image_file']
            if image_file and image_file.filename != '':
                new_image_url = _save_uploaded_file(image_file)
                if new_image_url:
                    image_url = new_image_url
                else:
                    flash('Invalid image file. Please use PNG, JPG, JPEG, GIF, or WebP format.', 'warning')
        
        update_data = {
            "item": request.form.get('item'),
            "brand": request.form.get('brand'),
            "category": request.form.get('category'),
            "price": int(request.form.get('price')),
            "stock": int(request.form.get('stock')),
            "image": image_url
        }
        
        db.catalog.update_one(
            {"_id": ObjectId(id)},
            {"$set": update_data}
        )
        flash('Product updated successfully', 'success')
    except Exception as e:
        app.logger.error(f"Error updating product: {e}")
        flash('Error updating product', 'error')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete/<id>', methods=['POST'])
@login_required
def delete_product(id):
    if current_user.role != 'admin':
        flash('Access Denied', 'error')
        return redirect(url_for('admin_dashboard'))
    
    try:
        # Validate product_id first
        product_oid = _coerce_object_id(id)
        if not product_oid:
            flash('Invalid product ID', 'error')
            return redirect(url_for('admin_dashboard'))
        
        product = db.catalog.find_one({"_id": product_oid})
        if not product:
            flash('Product not found', 'error')
            return redirect(url_for('admin_dashboard'))
        
        # Delete the product from catalog
        db.catalog.delete_one({"_id": product_oid})
        
        # Remove from any active carts
        USERS_COLLECTION.update_many(
            {"cart": product_oid},
            {"$pull": {"cart": product_oid}}
        )
        
        flash(f'Product "{product.get("item")}" deleted successfully', 'success')
    except Exception as e:
        app.logger.error(f"Error deleting product: {e}")
        flash('Error deleting product', 'error')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add-product', methods=['POST'])
@login_required
def add_product():
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    # Handle image upload
    image_url = None
    if 'image_file' in request.files:
        image_file = request.files['image_file']
        if image_file and image_file.filename != '':
            image_url = _save_uploaded_file(image_file)
            if not image_url:
                flash('Invalid image file. Please use PNG, JPG, JPEG, GIF, or WebP format.', 'error')
                return redirect(url_for('admin_dashboard'))
    
    # Fallback to placeholder if no image provided
    if not image_url:
        image_url = "https://placehold.co/400x400?text=No+Image"
    
    new_item = {
        "item": request.form.get('item'),
        "brand": request.form.get('brand'),
        "category": request.form.get('category'),
        "price": int(request.form.get('price')),
        "stock": int(request.form.get('stock')),
        "image": image_url
    }
    
    db.catalog.insert_one(new_item)
    flash('New product added successfully!')
    return redirect(url_for('admin_dashboard'))

# --- CUSTOMER ROUTES ---
@app.route('/add_to_cart/<product_id>')
def add_to_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        if current_user.is_authenticated and current_user.role == 'customer':
            user_object_id = _current_user_object_id()
            USERS_COLLECTION.update_one(
                {"_id": user_object_id},
                {"$push": {"cart": ObjectId(product_id)}}
            )
        elif current_user.is_authenticated and current_user.role != 'customer':
            if is_ajax:
                return jsonify({'error': 'Admins cannot add to cart'}), 403
            flash("Admins manage inventory; customers manage carts!")
            return redirect(url_for('index'))
        else:
            guest_cart = _get_guest_cart()
            guest_cart.append(product_id)
            _save_guest_cart(guest_cart)
        
        if is_ajax:
            return jsonify({'success': True}), 200
        flash('Item added to cart!')
        return redirect(url_for('index'))
    except Exception as e:
        app.logger.error(f"Error adding to cart: {e}")
        if is_ajax:
            return jsonify({'error': str(e)}), 500
        return redirect(url_for('index'))

@app.route('/cart')
def view_cart():
    # only customers may view the cart; redirect admins back to the dashboard
    if current_user.is_authenticated and current_user.role != 'customer':
        flash("Admins manage inventory; customers manage carts!")
        return redirect(url_for('admin_dashboard'))

    # load raw id list from either user record or session
    if current_user.is_authenticated and current_user.role == 'customer':
        user_data = USERS_COLLECTION.find_one({"_id": _current_user_object_id()})
        cart_ids = user_data.get('cart', [])
    else:
        cart_ids = _get_guest_cart()

    # aggregate quantities so UI can display +1, +2, etc.
    cart_map: dict = {}
    for pid in cart_ids:
        try:
            oid = pid if isinstance(pid, ObjectId) else ObjectId(pid)
        except Exception:
            continue
        prod = db.catalog.find_one({"_id": oid})
        if not prod:
            continue
        key = str(oid)
        entry = cart_map.setdefault(key, {"product": prod, "quantity": 0})
        entry["quantity"] += 1

    cart_items = []
    total_price = 0
    for entry in cart_map.values():
        prod = entry["product"]
        qty = entry["quantity"]
        cart_items.append({"product": prod, "quantity": qty})
        total_price += prod.get('price', 0) * qty

    return render_template('cart.html', items=cart_items, total=total_price)

@app.route('/remove_from_cart/<product_id>')
def remove_from_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        # Validate product_id first
        product_oid = _coerce_object_id(product_id)
        if not product_oid:
            if is_ajax:
                return jsonify({'error': 'Invalid product ID'}), 400
            flash('Invalid product ID.')
            return redirect(url_for('view_cart'))
        
        # remove all occurrences (delete item completely)
        if current_user.is_authenticated and current_user.role == 'customer':
            USERS_COLLECTION.update_one(
                {"_id": _current_user_object_id()},
                {"$pull": {"cart": product_oid}}
            )
        elif current_user.is_authenticated and current_user.role != 'customer':
            if is_ajax:
                return jsonify({'error': 'Admins cannot manage carts'}), 403
            flash("Admins manage inventory; customers manage carts!")
            return redirect(url_for('admin_dashboard'))
        else:
            guest_cart = _get_guest_cart()
            if product_id in guest_cart:
                # remove all occurrences of the id
                guest_cart = [pid for pid in guest_cart if pid != product_id]
                _save_guest_cart(guest_cart)
        
        if is_ajax:
            return jsonify({'success': True}), 200
        flash('Item removed from cart.')
        return redirect(url_for('view_cart'))
    except Exception as e:
        app.logger.error(f"Error removing from cart: {e}")
        if is_ajax:
            return jsonify({'error': str(e)}), 500
        flash('Error removing item from cart.')
        return redirect(url_for('view_cart'))


@app.route('/cart/increment/<product_id>')
def increment_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        if current_user.is_authenticated and current_user.role == 'customer':
            # Validate product_id first
            product_oid = _coerce_object_id(product_id)
            if not product_oid:
                if is_ajax:
                    return jsonify({'error': 'Invalid product ID'}), 400
                flash('Invalid product ID.')
                return redirect(url_for('view_cart'))
            
            USERS_COLLECTION.update_one(
                {"_id": _current_user_object_id()},
                {"$push": {"cart": product_oid}}
            )
        elif not current_user.is_authenticated:
            guest_cart = _get_guest_cart()
            guest_cart.append(product_id)
            _save_guest_cart(guest_cart)
        
        if is_ajax:
            return jsonify({'success': True}), 200
        return redirect(url_for('view_cart'))
    except Exception as e:
        app.logger.error(f"Error incrementing cart: {e}")
        if is_ajax:
            return jsonify({'error': str(e)}), 500
        flash('Error adding item to cart.')
        return redirect(url_for('view_cart'))


@app.route('/cart/decrement/<product_id>')
def decrement_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        if current_user.is_authenticated and current_user.role == 'customer':
            # Validate product_id first
            product_oid = _coerce_object_id(product_id)
            if not product_oid:
                if is_ajax:
                    return jsonify({'error': 'Invalid product ID'}), 400
                flash('Invalid product ID.')
                return redirect(url_for('view_cart'))
            
            user_data = USERS_COLLECTION.find_one({"_id": _current_user_object_id()})
            cart_list = user_data.get('cart', [])
            # remove first matching occurrence
            for idx, val in enumerate(cart_list):
                if (isinstance(val, ObjectId) and val == product_oid) or (str(val) == str(product_oid)):
                    cart_list.pop(idx)
                    break
            USERS_COLLECTION.update_one({"_id": _current_user_object_id()}, {"$set": {"cart": cart_list}})
        elif not current_user.is_authenticated:
            guest_cart = _get_guest_cart()
            if product_id in guest_cart:
                guest_cart.remove(product_id)
                _save_guest_cart(guest_cart)
        
        if is_ajax:
            return jsonify({'success': True}), 200
        return redirect(url_for('view_cart'))
    except Exception as e:
        app.logger.error(f"Error decrementing cart: {e}")
        if is_ajax:
            return jsonify({'error': str(e)}), 500
        flash('Error removing item from cart.')
        return redirect(url_for('view_cart'))


@app.route('/checkout')
def checkout():
    # if anonymous, send to login/register with message
    if not current_user.is_authenticated:
        flash('Please log in or register before proceeding to payment.')
        return redirect(url_for('login', next=url_for('checkout')))

    if current_user.role != 'customer':
        flash("Admins manage inventory; customers manage carts!")
        return redirect(url_for('admin_dashboard'))

    # at this point customer is logged in -- normally you'd show a payment page
    # for now just confirm and keep them on cart (could render a checkout template)
    flash('Login verified. Continue to payment.')
    return redirect(url_for('view_cart'))


@app.route('/create-rzp-order', methods=['POST'])
@login_required
def create_rzp_order():
    if current_user.role != 'customer':
        return jsonify({"error": "Admin cannot place orders"}), 403
    
    user_object_id = _current_user_object_id()
    user_data = USERS_COLLECTION.find_one({"_id": user_object_id})
    cart_ids = user_data.get('cart', [])
    if not cart_ids:
        return jsonify({"error": "Cart is empty"}), 400

    total_amount = 0
    for pid in cart_ids:
        try:
            oid = pid if isinstance(pid, ObjectId) else ObjectId(pid)
        except Exception:
            continue
        prod = db.catalog.find_one({"_id": oid})
        if prod:
            total_amount += prod.get('price', 0)
    
    if total_amount == 0:
        return jsonify({"error": "Invalid cart amount"}), 400

    if not razorpay_client:
        return jsonify({"error": "Payment gateway not configured server-side"}), 500

    try:
        order_data = {
            'amount': int(total_amount * 100),
            'currency': 'INR',
            'receipt': str(user_object_id)
        }
        order = razorpay_client.order.create(data=order_data)
        
        return jsonify({
            "order_id": order['id'],
            "amount": order['amount'],
            "currency": order['currency'],
            "key": razorpay_key_id,
            "name": user_data.get('username'),
            "email": user_data.get('email')
        })
    except Exception as e:
        app.logger.error(f"Razorpay order creation failed: {e}")
        return jsonify({"error": "Failed to create payment order"}), 500


@app.route('/complete-order', methods=['POST'])
@login_required
def complete_order():
    """
    Complete the current cart order and save it to the user's order history.
    Clears the cart after order is placed.
    """
    if current_user.role != 'customer':
        flash("Only customers can place orders!", 'error')
        return redirect(url_for('admin_dashboard'))
    
    try:
        # Razorpay Verification
        razorpay_payment_id = request.form.get('razorpay_payment_id')
        razorpay_order_id = request.form.get('razorpay_order_id')
        razorpay_signature = request.form.get('razorpay_signature')
        
        if razorpay_client:
            if not (razorpay_payment_id and razorpay_order_id and razorpay_signature):
                flash("Incomplete payment details received.", 'error')
                return redirect(url_for('view_cart'))
            
            try:
                razorpay_client.utility.verify_payment_signature({
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_signature': razorpay_signature
                })
            except razorpay.errors.SignatureVerificationError:
                flash("Payment verification failed! Please contact support.", 'error')
                return redirect(url_for('view_cart'))
                
        # Get user data and cart
        user_object_id = _current_user_object_id()
        user_data = USERS_COLLECTION.find_one({"_id": user_object_id})
        cart_ids = user_data.get('cart', [])
        
        if not cart_ids:
            flash('Your cart is empty. Please add items before placing an order.', 'warning')
            return redirect(url_for('view_cart'))
        
        # Build order items with product details
        order_items = []
        total_amount = 0
        
        cart_map = {}
        for pid in cart_ids:
            try:
                oid = pid if isinstance(pid, ObjectId) else ObjectId(pid)
            except Exception:
                continue
            
            prod = db.catalog.find_one({"_id": oid})
            if not prod:
                continue
            
            key = str(oid)
            if key not in cart_map:
                cart_map[key] = {"product": prod, "quantity": 0}
            cart_map[key]["quantity"] += 1
        
        # Create order items with price snapshot
        for item in cart_map.values():
            prod = item["product"]
            qty = item["quantity"]
            item_total = prod.get('price', 0) * qty
            
            order_items.append({
                "product_id": str(prod['_id']),
                "product_name": prod.get('item', ''),
                "brand": prod.get('brand', ''),
                "quantity": qty,
                "price_per_unit": prod.get('price', 0),
                "item_total": item_total
            })
            
            total_amount += item_total
        
        # Create order object
        order = {
            "_id": ObjectId(),  # Unique order ID
            "user_id": user_object_id,
            "customer_name": user_data.get('username'),
            "customer_email": user_data.get('email'),
            "order_date": datetime.utcnow(),
            "items": order_items,
            "total_amount": total_amount,
        }
        
        ORDER_COLLECTION.insert_one(order)
        _upsert_order_status(order['_id'], user_object_id, 'placed', order['order_date'])
        USERS_COLLECTION.update_one(
            {"_id": user_object_id},
            {"$set": {"cart": []}}
        )
        
        flash(f'Order placed successfully! Order ID: {str(order["_id"])}', 'success')
        return redirect(url_for('order_history'))
    
    except Exception as e:
        app.logger.error(f"Error completing order: {e}")
        flash(f'An error occurred while placing the order: {str(e)}', 'error')
        return redirect(url_for('view_cart'))


@app.route('/orders')
@login_required
def order_history():
    """
    Display all orders for the current authenticated customer.
    """
    if current_user.role != 'customer':
        flash("Only customers can view order history!", 'error')
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('index'))
    
    try:
        orders = _get_orders_for_user(_current_user_object_id())
        
        return render_template('order_history.html', orders=orders)
    
    except Exception as e:
        app.logger.error(f"Error retrieving order history: {e}")
        flash('An error occurred while retrieving your order history.', 'error')
        return redirect(url_for('view_cart'))


# --- USER PROFILE & SECURITY ROUTES ---
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def user_profile():
    """
    User profile and security settings: change password, email, username.
    """
    if current_user.role not in ['customer', 'admin']:
        flash("Only authenticated users can access this page!", 'error')
        return redirect(url_for('index'))
    
    # Get collection based on account type
    if current_user.role == 'admin':
        collection = ADMIN_COLLECTION
    else:
        collection = USERS_COLLECTION
    
    user_id = _coerce_object_id(current_user.id)
    user_data = collection.find_one({"_id": user_id})
    
    if not user_data:
        flash("User not found!", 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        action = request.form.get('action', '')
        
        # Change Password
        if action == 'change_password':
            current_password = request.form.get('current_password', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()
            
            if not current_password or not new_password or not confirm_password:
                flash('All password fields are required!', 'error')
            elif len(new_password) < 6:
                flash('New password must be at least 6 characters long!', 'error')
            elif new_password != confirm_password:
                flash('New passwords do not match!', 'error')
            elif not check_password_hash(user_data.get('password', ''), current_password):
                flash('Current password is incorrect!', 'error')
            else:
                new_hash = generate_password_hash(new_password)
                collection.update_one(
                    {"_id": user_id},
                    {"$set": {"password": new_hash}}
                )
                flash('Password changed successfully!', 'success')
                return redirect(url_for('user_profile'))
        
        # Change Email
        elif action == 'change_email':
            new_email = request.form.get('new_email', '').strip().lower()
            password = request.form.get('password', '').strip()
            
            if not new_email or '@' not in new_email:
                flash('Please provide a valid email address!', 'error')
            elif not check_password_hash(user_data.get('password', ''), password):
                flash('Password is incorrect!', 'error')
            elif collection.find_one({"email": new_email, "_id": {"$ne": user_id}}):
                flash('This email is already registered with another account!', 'error')
            else:
                collection.update_one(
                    {"_id": user_id},
                    {"$set": {"email": new_email}}
                )
                flash('Email changed successfully!', 'success')
                return redirect(url_for('user_profile'))
        
        # Change Username
        elif action == 'change_username':
            new_username = request.form.get('new_username', '').strip()
            password = request.form.get('password', '').strip()
            
            if not new_username or len(new_username) < 3:
                flash('Username must be at least 3 characters long!', 'error')
            elif not check_password_hash(user_data.get('password', ''), password):
                flash('Password is incorrect!', 'error')
            elif collection.find_one({"username": new_username, "_id": {"$ne": user_id}}):
                flash('This username is already taken!', 'error')
            else:
                collection.update_one(
                    {"_id": user_id},
                    {"$set": {"username": new_username}}
                )
                flash('Username changed successfully!', 'success')
                return redirect(url_for('user_profile'))
    
    return render_template('user_profile.html', user=user_data)


# --- WISHLIST ROUTES ---
WISHLIST_COLLECTION = db["wishlist"]

@app.route('/wishlist')
@login_required
def user_wishlist():
    """
    Display user's wishlist items.
    """
    if current_user.role != 'customer':
        flash("Only customers can access wishlist!", 'error')
        return redirect(url_for('index'))
    
    user_id = _coerce_object_id(current_user.id)
    wishlist_doc = WISHLIST_COLLECTION.find_one({"user_id": user_id})
    
    wishlist_items = []
    if wishlist_doc and wishlist_doc.get('items'):
        # Get product details for each wishlist item
        product_ids = [_coerce_object_id(item_id) for item_id in wishlist_doc.get('items', [])]
        products = db.catalog.find({"_id": {"$in": product_ids}})
        wishlist_items = list(products)
    
    return render_template('user_wishlist.html', wishlist_items=wishlist_items)


@app.route('/add-to-wishlist/<product_id>', methods=['POST'])
@login_required
def add_to_wishlist(product_id):
    """
    Add a product to user's wishlist.
    """
    if current_user.role != 'customer':
        return jsonify({"success": False, "message": "Only customers can use wishlist"}), 403
    
    user_id = _coerce_object_id(current_user.id)
    product_id_obj = _coerce_object_id(product_id)
    
    if not product_id_obj:
        return jsonify({"success": False, "message": "Invalid product ID"}), 400
    
    WISHLIST_COLLECTION.update_one(
        {"user_id": user_id},
        {
            "$addToSet": {"items": product_id_obj},
            "$set": {"user_id": user_id, "updated_at": datetime.utcnow()}
        },
        upsert=True
    )
    
    return jsonify({"success": True, "message": "Added to wishlist"})


@app.route('/remove-from-wishlist/<product_id>', methods=['POST'])
@login_required
def remove_from_wishlist(product_id):
    """
    Remove a product from user's wishlist.
    """
    if current_user.role != 'customer':
        return jsonify({"success": False, "message": "Only customers can use wishlist"}), 403
    
    user_id = _coerce_object_id(current_user.id)
    product_id_obj = _coerce_object_id(product_id)
    
    if not product_id_obj:
        return jsonify({"success": False, "message": "Invalid product ID"}), 400
    
    WISHLIST_COLLECTION.update_one(
        {"user_id": user_id},
        {"$pull": {"items": product_id_obj}}
    )
    
    return jsonify({"success": True, "message": "Removed from wishlist"})


# --- ADDRESS MANAGEMENT ROUTES ---
ADDRESSES_COLLECTION = db["addresses"]

@app.route('/addresses', methods=['GET', 'POST'])
@login_required
def user_addresses():
    """
    Display and manage user's saved addresses.
    """
    if current_user.role not in ['customer', 'admin']:
        flash("Only authenticated users can manage addresses!", 'error')
        return redirect(url_for('index'))
    
    user_id = _coerce_object_id(current_user.id)
    addresses = list(ADDRESSES_COLLECTION.find({"user_id": user_id}).sort("is_default", -1))
    
    if request.method == 'POST':
        action = request.form.get('action', '')
        
        if action == 'add':
            address_name = request.form.get('address_name', '').strip()
            address_line = request.form.get('address_line', '').strip()
            city = request.form.get('city', '').strip()
            state = request.form.get('state', '').strip()
            pincode = request.form.get('pincode', '').strip()
            phone = request.form.get('phone', '').strip()
            
            if not all([address_name, address_line, city, state, pincode, phone]):
                flash('All fields are required!', 'error')
            elif len(pincode) != 6 or not pincode.isdigit():
                flash('Pincode must be 6 digits!', 'error')
            elif len(phone) < 10 or len(phone) > 10:
                flash('Phone number must be 10 digits!', 'error')
            else:
                is_default = len(addresses) == 0  # First address is default
                ADDRESSES_COLLECTION.insert_one({
                    "user_id": user_id,
                    "address_name": address_name,
                    "address_line": address_line,
                    "city": city,
                    "state": state,
                    "pincode": pincode,
                    "phone": phone,
                    "is_default": is_default,
                    "created_at": datetime.utcnow()
                })
                flash('Address added successfully!', 'success')
                return redirect(url_for('user_addresses'))
        
        elif action == 'set_default':
            address_id = _coerce_object_id(request.form.get('address_id'))
            if address_id:
                ADDRESSES_COLLECTION.update_many(
                    {"user_id": user_id},
                    {"$set": {"is_default": False}}
                )
                ADDRESSES_COLLECTION.update_one(
                    {"_id": address_id, "user_id": user_id},
                    {"$set": {"is_default": True}}
                )
                flash('Default address updated!', 'success')
                return redirect(url_for('user_addresses'))
        
        elif action == 'delete':
            address_id = _coerce_object_id(request.form.get('address_id'))
            if address_id:
                ADDRESSES_COLLECTION.delete_one(
                    {"_id": address_id, "user_id": user_id}
                )
                flash('Address deleted successfully!', 'success')
                return redirect(url_for('user_addresses'))
    
    return render_template('user_addresses.html', addresses=addresses)


@app.route('/address/<address_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_address(address_id):
    """
    Edit a saved address.
    """
    if current_user.role not in ['customer', 'admin']:
        flash("Only authenticated users can edit addresses!", 'error')
        return redirect(url_for('index'))
    
    user_id = _coerce_object_id(current_user.id)
    address_id_obj = _coerce_object_id(address_id)
    address = ADDRESSES_COLLECTION.find_one({"_id": address_id_obj, "user_id": user_id})
    
    if not address:
        flash('Address not found!', 'error')
        return redirect(url_for('user_addresses'))
    
    if request.method == 'POST':
        address_name = request.form.get('address_name', '').strip()
        address_line = request.form.get('address_line', '').strip()
        city = request.form.get('city', '').strip()
        state = request.form.get('state', '').strip()
        pincode = request.form.get('pincode', '').strip()
        phone = request.form.get('phone', '').strip()
        
        if not all([address_name, address_line, city, state, pincode, phone]):
            flash('All fields are required!', 'error')
        elif len(pincode) != 6 or not pincode.isdigit():
            flash('Pincode must be 6 digits!', 'error')
        elif len(phone) != 10 or not phone.isdigit():
            flash('Phone number must be 10 digits!', 'error')
        else:
            ADDRESSES_COLLECTION.update_one(
                {"_id": address_id_obj},
                {
                    "$set": {
                        "address_name": address_name,
                        "address_line": address_line,
                        "city": city,
                        "state": state,
                        "pincode": pincode,
                        "phone": phone,
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            flash('Address updated successfully!', 'success')
            return redirect(url_for('user_addresses'))
    
    return render_template('edit_address.html', address=address)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
