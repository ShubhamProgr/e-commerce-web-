import os
import certifi
import random
import smtplib
from datetime import datetime, timedelta
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

# Database Setup
uri = os.getenv("MONGO_URL")
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where())
db = client["online_store"]

# Login Manager Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data['_id'])
        self.username = user_data.get('username')
        self.role = user_data.get('role', 'customer')

@login_manager.user_loader
def load_user(user_id):
    user_data = db.users.find_one({"_id": ObjectId(user_id)})
    return User(user_data) if user_data else None


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
        db.users.update_one(
            {"_id": ObjectId(user_id)},
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
        username = request.form.get('username')
        password = request.form.get('password')
        
        # 1. Search for existing user
        user_data = db.users.find_one({"username": username})
        
        if user_data:
            if not check_password_hash(user_data['password'], password):
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
    # Fetch all customers with their orders
    customers = list(db.users.find({"role": {"$ne": "admin"}}, {"username": 1, "email": 1, "orders": 1}))
    return render_template('admin_dashboard.html', products=products, customers=customers)

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

        stored_code = user_data.get('otp_code')
        expires_at = user_data.get('otp_expires_at')

        if not stored_code or not expires_at or expires_at < datetime.utcnow():
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

        if otp_input == stored_code:
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
            "status": "completed",  # Could be extended to 'shipped', 'delivered', etc.
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
        orders = user_data.get('orders', [])
        
        # Sort orders by date (newest first)
        orders = sorted(orders, key=lambda x: x.get('order_date', datetime.min), reverse=True)
        
        return render_template('order_history.html', orders=orders)
    
    except Exception as e:
        app.logger.error(f"Error retrieving order history: {e}")
        flash('An error occurred while retrieving your order history.', 'error')
        return redirect(url_for('view_cart'))


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
