import os
import certifi
from pymongo import MongoClient
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

# Load connection string from your global .env
load_dotenv()
uri = os.getenv("MONGO_URL")

# Connect to your Atlas cluster
client = MongoClient(uri, tlsCAFile=certifi.where())
db = client["online_store"]

def create_first_admin():
    # Define your admin details
    admin_username = "admin"
    admin_password = "admin21944" 

    # Check if admin already exists to avoid duplicates
    if db.users.find_one({"username": admin_username}):
        print("Admin user already exists.")
        return

    # Hash the password for security and insert
    admin_user = {
        "username": admin_username,
        "password": generate_password_hash(admin_password),
        "role": "admin"
    }
    
    db.users.insert_one(admin_user)
    print(f"Admin '{admin_username}' created successfully!")

if __name__ == "__main__":
    create_first_admin()