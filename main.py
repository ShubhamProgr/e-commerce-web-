import pymongo
import certifi
import ssl
import os
from dotenv import load_dotenv

# 1. Load .env from the parent directory (global level)
# This looks up one level from the 'web' folder to find your .env file
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

# 2. Get the URL from environment variables
url = os.getenv("MONGO_URL")

# SSL Certificate setup for Windows/macOS
ca = certifi.where()

try:
    # 3. Connect using the URL from your .env and the CA file
    client = pymongo.MongoClient(url, tlsCAFile=ca)
    
    db = client["online_store"]
    products = db["catalog"]

    sample_product = {
        "item": "Mechanical Keyboard",
        "category": "Peripherals",
        "price": 3500,
        "stock": 15
    }

    result = products.insert_one(sample_product)
    print(f"Success! Product inserted with ID: {result.inserted_id}")

except Exception as e:
    print(f"An error occurred: {e}")