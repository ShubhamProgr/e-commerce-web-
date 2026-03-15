import os
import certifi
from pymongo import MongoClient
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
uri = os.getenv("MONGO_URL")

# Connect to your Atlas cluster
client = MongoClient(uri, tlsCAFile=certifi.where())
db = client["online_store"]

def create_first_admin():
    # Define your admin details
    admin_username = "admin"
    admin_password = "admin21944" 

    # If admin exists, repair missing fields so old records remain compatible.
    existing_admin = db.users.find_one({"username": admin_username})
    if existing_admin:
        updates = {
            "email": existing_admin.get("email") or "admin@greenfieldsfarm.com",
            "role": "admin",
            "is_verified": True,
            "cart": existing_admin.get("cart") if isinstance(existing_admin.get("cart"), list) else [],
            "orders": existing_admin.get("orders") if isinstance(existing_admin.get("orders"), list) else []
        }

        existing_password = existing_admin.get("password")
        if not isinstance(existing_password, str) or not existing_password.startswith(("pbkdf2:", "scrypt:")):
            updates["password"] = generate_password_hash(admin_password)

        db.users.update_one({"_id": existing_admin["_id"]}, {"$set": updates})
        print("Admin user already existed. Schema repaired successfully.")
        return

    # Hash the password for security and insert
    admin_user = {
        "username": admin_username,
        "email": "admin@greenfieldsfarm.com",
        "password": generate_password_hash(admin_password),
        "role": "admin",
        "is_verified": True,
        "cart": [],
        "orders": []
    }
    
    db.users.insert_one(admin_user)
    print(f"Admin '{admin_username}' created successfully!")

if __name__ == "__main__":
    create_first_admin()