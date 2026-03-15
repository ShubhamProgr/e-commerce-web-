import os
import certifi
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import pymongo
from bson.objectid import ObjectId

# Load .env from project root (parent of web/) so SMTP and MONGO_URL are always found
_env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(_env_path)
app = Flask(__name__)
app.secret_key = "your_secret_key_here" # Required for sessions

uri = os.getenv("MONGO_URL")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where())
db = client["online_store"]
USERS_COLLECTION = db["users"]
ADMIN_COLLECTION = db["admin"]
ORDER_COLLECTION = db["order"]
STATUS_COLLECTION = db["status"]

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


def _build_customer_order_dashboard(customers):
    dashboard_customers = []
    orders_by_user = _get_orders_for_users([customer.get('_id') for customer in customers])
    summary = {
        "total_customers": 0,
        "total_orders": 0,
        "placed_orders": 0,
        "on_the_way_orders": 0,
        "delivered_orders": 0,
        "cancelled_orders": 0,
        "total_revenue": 0,
    }

    for customer in customers:
        customer_data = dict(customer)
        orders = orders_by_user.get(str(customer_data.get('_id')), [])
        order_counts = {status: 0 for status in ORDER_STATUS_FLOW}
        total_spent = 0

        for order in orders:
            order_counts[order['status']] += 1
            try:
                total_spent += float(order.get('total_amount', 0) or 0)
            except (TypeError, ValueError):
                continue

        customer_data['orders'] = orders
        customer_data['order_counts'] = {
            "all": len(orders),
            **order_counts,
        }
        customer_data['total_spent'] = total_spent
        customer_data['latest_order_at'] = orders[0].get('order_date') if orders else None

        dashboard_customers.append(customer_data)

        summary['total_customers'] += 1
        summary['total_orders'] += len(orders)
        summary['placed_orders'] += order_counts['placed']
        summary['on_the_way_orders'] += order_counts['on_the_way']
        summary['delivered_orders'] += order_counts['delivered']
        summary['cancelled_orders'] += order_counts['cancelled']
        summary['total_revenue'] += total_spent

    dashboard_customers.sort(
        key=lambda customer: (
            customer.get('latest_order_at') is not None,
            customer.get('latest_order_at') or datetime.min,
            (customer.get('username') or '').lower(),
        ),
        reverse=True,
    )

    return dashboard_customers, summary


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


def send_otp_email(to_email: str, otp_code: str) -> bool:
    """
    Send OTP via email using SMTP credentials from .env.
    Returns True if the email was sent successfully, False otherwise.
    In production, set SMTP_USER and SMTP_PASSWORD (and optionally EMAIL_SENDER) in .env.
    """
    if not to_email:
        return False

    to_email = to_email.strip().lower()
    cfg = _get_smtp_config()

    if not cfg["user"] or not cfg["password"]:
        app.logger.warning(
            "SMTP not configured: set SMTP_USER and SMTP_PASSWORD in .env. "
            f"SMTP_USER set: {bool(cfg['user'])}, SMTP_PASSWORD set: {bool(cfg['password'])}, "
            f".env path: {_env_path}. OTP for {to_email}: {otp_code}"
        )
        return False

    body = f"Your GreenFields Farm Shop verification code is {otp_code}. It will expire in 10 minutes."
    msg = MIMEText(body)
    msg["Subject"] = "Your GreenFields verification code"
    msg["From"] = cfg["sender"] or cfg["user"]
    msg["To"] = to_email

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        app.logger.info(f"OTP email sent to {to_email}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(f"SMTP login failed: {e}. Check SMTP_USER and SMTP_PASSWORD (use App Password for Gmail).")
        return False
    except smtplib.SMTPException as e:
        app.logger.error(f"SMTP error sending to {to_email}: {e}")
        return False
    except Exception as e:
        app.logger.exception(f"Failed to send OTP email to {to_email}: {e}")
        return False


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


# --- PUBLIC ROUTES ---
@app.route('/')
def index():
    products = list(db.catalog.find())
    return render_template('index.html', products=products)

@app.route('/product/<id>')
def product_detail(id):
    product = db.catalog.find_one({"_id": ObjectId(id)})
    return render_template('product_detail.html', product=product)

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
        
        # 1. Search for existing user
        user_data = db.users.find_one(
            {
                "$or": [
                    {"username": username},
                    {"email": username.lower()}
                ]
            }
        )
        
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
            db.users.update_one(
                {"_id": user_data['_id']},
                {"$set": {"otp_code": otp_code, "otp_expires_at": expires_at}}
            )
            if send_otp_email(email, otp_code):
                masked = mask_email(email)
                flash(f'We have emailed a verification code to {masked}. Enter it to sign in.', 'success')
            else:
                flash('We could not send the verification email. Please try again later or contact support.', 'error')
            # propagate the redirect target through the verification step
            return redirect(url_for('verify_otp', username=username, next=post_login_redirect))
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

# --- ADMIN ROUTES ---
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return "Access Denied", 403
    products = list(db.catalog.find())
    active_tab = request.args.get('tab', 'inventory')
    if active_tab not in {'inventory', 'customers'}:
        active_tab = 'inventory'

    customers = list(db.users.find({"role": {"$ne": "admin"}}, {"username": 1, "email": 1, "orders": 1}))
    customers, customer_metrics = _build_customer_order_dashboard(customers)
    status_options = [
        {"value": status, "label": ORDER_STATUS_LABELS[status]}
        for status in ORDER_STATUS_FLOW
    ]
    return render_template(
        'admin_dashboard.html',
        products=products,
        customers=customers,
        customer_metrics=customer_metrics,
        status_options=status_options,
        active_tab=active_tab,
    )


@app.route('/admin/orders/<user_id>/<order_id>/status', methods=['POST'])
@login_required
def update_order_status(user_id, order_id):
    if current_user.role != 'admin':
        return "Access Denied", 403

    new_status = _normalize_order_status(request.form.get('status'))

    try:
        result = db.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": {
                    "orders.$[order].status": new_status,
                    "orders.$[order].status_updated_at": datetime.utcnow(),
                }
            },
            array_filters=[{"order._id": ObjectId(order_id)}],
        )
    except Exception as exc:
        app.logger.error(f"Error updating order status: {exc}")
        flash('Could not update that order status.', 'error')
        return redirect(url_for('admin_dashboard', tab='customers'))

    if result.modified_count:
        flash(f"Order status updated to {ORDER_STATUS_LABELS[new_status]}.", 'success')
    else:
        flash('Order not found or status was already set to that value.', 'warning')

    return redirect(url_for('admin_dashboard', tab='customers'))

@app.route('/admin/users')
@login_required
def manage_users():
    if current_user.role != 'admin':
        return "Access Denied", 403
    all_users = list(db.users.find())
    return render_template('manage_users.html', users=all_users)

@app.route('/admin/make_admin/<user_id>')
@login_required
def make_admin(user_id):
    if current_user.role == 'admin':
        db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"role": "admin"}})
        flash("User promoted to Admin")
    return redirect(url_for('manage_users'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    # preserve next parameter so we can send the user back after verifying
    next_url = request.args.get('next') or request.form.get('next')

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Prevent duplicate usernames
        if db.users.find_one({"username": username}):
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
            "role": "customer",
            "is_verified": False,
            "otp_code": otp_code,
            "otp_expires_at": expires_at,
            "cart": [],  # Initialize empty cart
            "orders": []  # Initialize empty order history
        }
        db.users.insert_one(data)
        flash('We have sent a verification code to your email. Enter it to activate your account.', 'success')
        args = {"username": username}
        if next_url:
            args["next"] = next_url
        return redirect(url_for('verify_otp', **args))
        
    return render_template('register.html')


@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    username = request.args.get('username') or request.form.get('username')
    post_login_redirect = _get_post_login_redirect()

    if request.method == 'POST':
        otp_input = request.form.get('otp')
        username = request.form.get('username')

        user_data = db.users.find_one({"username": username})
        if not user_data:
            flash('User not found. Please register again.', 'error')
            return redirect(url_for('register'))

        stored_code = str(user_data.get('otp_code', '')).strip()
        expires_at = _normalize_datetime(user_data.get('otp_expires_at'))
        now_utc = datetime.utcnow()

        if not stored_code or not expires_at or expires_at < now_utc:
            new_code = generate_otp_code()
            new_expires = datetime.utcnow() + timedelta(minutes=10)
            db.users.update_one(
                {"_id": user_data['_id']},
                {"$set": {"otp_code": new_code, "otp_expires_at": new_expires}}
            )
            if send_otp_email(user_data.get('email') or '', new_code):
                flash('Your verification code expired. We have sent a new one to your email.', 'success')
            else:
                flash('We could not send the new code. Please check your email or try again later.', 'error')
            args = {"username": username}
            if post_login_redirect:
                args["next"] = post_login_redirect
            return redirect(url_for('verify_otp', **args))

        if str(otp_input or '').strip() == str(stored_code).strip():
            db.users.update_one(
                {"_id": user_data['_id']},
                {
                    "$set": {"is_verified": True},
                    "$unset": {"otp_code": "", "otp_expires_at": ""}
                }
            )
            user_obj = User(user_data)
            login_user(user_obj)
            _merge_guest_cart_into_user(user_data['_id'])
            flash('Your account has been verified. Welcome to GreenFields Farm Shop!', 'success')
            # Redirect admins to dashboard, customers to post_login_redirect
            if user_data.get('role') == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(post_login_redirect)

        flash('Wrong OTP entered. Please try again, resend, or go back to login.', 'error')
        email_mask = mask_email(user_data.get('email') or '')
        return render_template('verify_otp.html', username=username, email_mask=email_mask, next_url=post_login_redirect)

    # GET: show the verify form and display which email we're sending codes to
    email_mask = ""
    if username:
        user_data = db.users.find_one({"username": username})
        if user_data and user_data.get('email'):
            email_mask = mask_email(user_data['email'])

    return render_template('verify_otp.html', username=username, email_mask=email_mask, next_url=post_login_redirect)

@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    username = request.form.get('username')
    post_login_redirect = _get_post_login_redirect()

    user_data = db.users.find_one({"username": username})
    if not user_data:
        flash('User not found.', 'error')
        return redirect(url_for('login'))

    email = user_data.get('email')
    if not email:
        flash('No email found.', 'error')
        return redirect(url_for('login'))

    otp_code = generate_otp_code()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.users.update_one(
        {"_id": user_data['_id']},
        {"$set": {"otp_code": otp_code, "otp_expires_at": expires_at}}
    )
    if send_otp_email(email, otp_code):
        flash('New OTP sent to your email.', 'success')
    else:
        flash('Failed to send OTP.', 'error')
    return redirect(url_for('verify_otp', username=username, next=post_login_redirect))

@app.route('/admin/edit/<id>', methods=['POST'])
@login_required
def edit_product(id):
    if current_user.role == 'admin':
        new_price = int(request.form.get('price'))
        new_stock = int(request.form.get('stock'))
        db.catalog.update_one(
            {"_id": ObjectId(id)},
            {"$set": {"price": new_price, "stock": new_stock}}
        )
        flash('Product updated successfully')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add-product', methods=['POST'])
@login_required
def add_product():
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    new_item = {
        "item": request.form.get('item'),
        "brand": request.form.get('brand'),
        "category": request.form.get('category'),
        "price": int(request.form.get('price')),
        "stock": int(request.form.get('stock')),
        "image": request.form.get('image') or "https://placehold.co/400x400?text=No+Image"
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
            db.users.update_one(
                {"_id": ObjectId(current_user.id)},
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
        user_data = db.users.find_one({"_id": ObjectId(current_user.id)})
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
        # remove all occurrences (delete item completely)
        if current_user.is_authenticated and current_user.role == 'customer':
            db.users.update_one(
                {"_id": ObjectId(current_user.id)},
                {"$pull": {"cart": ObjectId(product_id)}}
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
        return redirect(url_for('view_cart'))


@app.route('/cart/increment/<product_id>')
def increment_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        if current_user.is_authenticated and current_user.role == 'customer':
            db.users.update_one(
                {"_id": ObjectId(current_user.id)},
                {"$push": {"cart": ObjectId(product_id)}}
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
        return redirect(url_for('view_cart'))


@app.route('/cart/decrement/<product_id>')
def decrement_cart(product_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    try:
        if current_user.is_authenticated and current_user.role == 'customer':
            user_data = db.users.find_one({"_id": ObjectId(current_user.id)})
            cart_list = user_data.get('cart', [])
            # remove first matching occurrence
            for idx, val in enumerate(cart_list):
                if (isinstance(val, ObjectId) and str(val) == product_id) or (str(val) == product_id):
                    cart_list.pop(idx)
                    break
            db.users.update_one({"_id": ObjectId(current_user.id)}, {"$set": {"cart": cart_list}})
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
        # Get user data and cart
        user_data = db.users.find_one({"_id": ObjectId(current_user.id)})
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
            "order_date": datetime.utcnow(),
            "items": order_items,
            "total_amount": total_amount,
            "status": "placed",
            "status_updated_at": datetime.utcnow(),
            "username": user_data.get('username'),
            "email": user_data.get('email')
        }
        
        # Save order to user's orders array
        db.users.update_one(
            {"_id": ObjectId(current_user.id)},
            {
                "$push": {"orders": order},
                "$set": {"cart": []}  # Clear the cart
            }
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
        user_data = db.users.find_one({"_id": ObjectId(current_user.id)})
        orders = _sort_orders_for_display(user_data.get('orders', []))
        
        return render_template('order_history.html', orders=orders)
    
    except Exception as e:
        app.logger.error(f"Error retrieving order history: {e}")
        flash('An error occurred while retrieving your order history.', 'error')
        return redirect(url_for('view_cart'))


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
