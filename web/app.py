import os
import certifi
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

# --- PUBLIC ROUTES ---
@app.route('/')
@login_required
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
        user_data = db.users.find_one({"username": username})
        
        if user_data and check_password_hash(user_data['password'], password):
            user_obj = User(user_data)
            login_user(user_obj)
            return redirect(url_for('index'))
        flash('Invalid username or password')
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

if __name__ == '__main__':
    app.run(debug=True)