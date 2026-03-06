import os, certifi, pymongo
from dotenv import load_dotenv

# Load connection and connect to Atlas
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
client = pymongo.MongoClient(os.getenv("MONGO_URL"), tlsCAFile=certifi.where())
db = client["online_store"]

def seed_professional_data():
    # Modern e-commerce schema: include SKUs, brands, and high-quality placeholders
    test_products = [
    {"item": "Aashirvaad Shudh Chakki Atta", "brand": "Aashirvaad", "category": "Atta & Rice", 
     "price": 495, "stock": 50, "image": "https://images.unsplash.com/photo-1578662996442-48f60103fc96?w=400&h=400&fit=crop"},
    {"item": "Daawat Rozana Basmati Rice", "brand": "Daawat", "category": "Atta & Rice", 
     "price": 380, "stock": 30, "image": "https://images.unsplash.com/photo-1586201375761-83865001e31c?w=400&h=400&fit=crop"},
    {"item": "Fortune Soyabean Oil", "brand": "Fortune", "category": "Oil & Ghee", 
     "price": 145, "stock": 100, "image": "https://images.unsplash.com/photo-1474979266404-7eaacbcd87c5?w=400&h=400&fit=crop"},
    {"item": "Amul Butter", "brand": "Amul", "category": "Dairy & Eggs", 
     "price": 56, "stock": 200, "image": "https://images.unsplash.com/photo-1589985270826-4b7bb135bc9d?w=400&h=400&fit=crop"}
]

    db.catalog.delete_many({}) # Wipe old testing data
    db.catalog.insert_many(test_products)
    print("✅ Professional testing catalog inserted with images!")

if __name__ == "__main__":
    seed_professional_data()