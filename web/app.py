import os
import certifi
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import pymongo
from bson.objectid import ObjectId

# Load .env and Initialize Flask
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
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


def generate_otp_code() -> str:
    return f"{random.randint(100000, 999999)}"


def send_otp_email(to_email: str, otp_code: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender_email = os.getenv("EMAIL_SENDER") or smtp_user

    if not (smtp_user and smtp_password and to_email and sender_email):
        app.logger.info(f"OTP for {to_email}: {otp_code}")
        return

    body = f"Your GreenFields Farm Shop verification code is {otp_code}. It will expire in 10 minutes."
    msg = MIMEText(body)
    msg["Subject"] = "Your GreenFields verification code"
    msg["From"] = sender_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

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
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # 1. Search for existing user
        user_data = db.users.find_one({"username": username})
        
        if user_data:
            if not check_password_hash(user_data['password'], password):
                flash('Invalid password for existing user.')
                return redirect(url_for('login'))

            if user_data.get('role', 'customer') != 'admin' and not user_data.get('is_verified', False):
                flash('Please verify your account with the code sent to your email.')
                return redirect(url_for('verify_otp', username=username))

            user_obj = User(user_data)
            login_user(user_obj)
            return redirect(url_for('index'))
        else:
            flash('No account found with that username. Please register first.')
            return redirect(url_for('register'))
            
    return render_template('login.html')
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# --- ADMIN ROUTES ---
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return "Access Denied", 403
    products = list(db.catalog.find())
    return render_template('admin_dashboard.html', products=products)

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
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Prevent duplicate usernames
        if db.users.find_one({"username": username}):
            flash('Username already exists!')
            return redirect(url_for('register'))
        
        otp_code = generate_otp_code()
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        db.users.insert_one({
            "username": username,
            "email": email,
            "password": generate_password_hash(password),
            "role": "customer",
            "is_verified": False,
            "otp_code": otp_code,
            "otp_expires_at": expires_at
        })

        send_otp_email(email, otp_code)
        flash('We have sent a verification code to your email. Enter it to activate your account.')
        return redirect(url_for('verify_otp', username=username))
        
    return render_template('register.html')


@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    username = request.args.get('username') or request.form.get('username')

    if request.method == 'POST':
        otp_input = request.form.get('otp')
        username = request.form.get('username')

        user_data = db.users.find_one({"username": username})
        if not user_data:
            flash('User not found. Please register again.')
            return redirect(url_for('register'))

        if user_data.get('is_verified', False):
            flash('Account already verified. You can log in.')
            return redirect(url_for('login'))

        stored_code = user_data.get('otp_code')
        expires_at = user_data.get('otp_expires_at')

        if not stored_code or not expires_at or expires_at < datetime.utcnow():
            new_code = generate_otp_code()
            new_expires = datetime.utcnow() + timedelta(minutes=10)
            db.users.update_one(
                {"_id": user_data['_id']},
                {"$set": {"otp_code": new_code, "otp_expires_at": new_expires}}
            )
            send_otp_email(user_data.get('email'), new_code)
            flash('Your verification code expired. We have sent a new one to your email.')
            return redirect(url_for('verify_otp', username=username))

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
            flash('Your account has been verified. Welcome to GreenFields Farm Shop!')
            return redirect(url_for('index'))

        flash('Invalid verification code. Please try again.')
        return redirect(url_for('verify_otp', username=username))

    return render_template('verify_otp.html', username=username)

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
    # Redirect back to the main dashboard instead of manage_inventory
    return redirect(url_for('admin_dashboard'))

# 3. ENSURE the add function also redirects to the dashboard
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
@login_required
def add_to_cart(product_id):
    if current_user.role == 'customer':
        db.users.update_one(
            {"_id": ObjectId(current_user.id)},
            {"$push": {"cart": ObjectId(product_id)}}
        )
        flash('Item added to cart!')
    return redirect(url_for('index'))

@app.route('/cart')
@login_required
def view_cart():
    # Only customers should access this view
    if current_user.role != 'customer':
        flash("Admins manage inventory; customers manage carts!")
        return redirect(url_for('admin_dashboard'))

    # Fetch the user's document to get the list of product IDs in their cart
    user_data = db.users.find_one({"_id": ObjectId(current_user.id)})
    cart_ids = user_data.get('cart', [])
    
    # Fetch full product details for each ID in the cart
    cart_items = []
    total_price = 0
    for pid in cart_ids:
        product = db.catalog.find_one({"_id": pid})
        if product:
            cart_items.append(product)
            total_price += product.get('price', 0)
            
    return render_template('cart.html', items=cart_items, total=total_price)

@app.route('/remove_from_cart/<product_id>')
@login_required
def remove_from_cart(product_id):
    # Use $pull to remove one instance of the product ID from the cart array
    db.users.update_one(
        {"_id": ObjectId(current_user.id)},
        {"$pull": {"cart": ObjectId(product_id)}}
    )
    flash('Item removed from cart.')
    return redirect(url_for('view_cart'))

if __name__ == '__main__':
    app.run(debug=True)