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
     "price": 495, "stock": 50, "image": "https://placehold.co/400x400/fff7e6/d48806?text=Atta+10kg"},
    {"item": "Daawat Rozana Basmati Rice", "brand": "Daawat", "category": "Atta & Rice", 
     "price": 380, "stock": 30, "image": "https://placehold.co/400x400/f6ffed/389e0d?text=Rice+5kg"},
    {"item": "Fortune Soyabean Oil", "brand": "Fortune", "category": "Oil & Ghee", 
     "price": 145, "stock": 100, "image": "https://placehold.co/400x400/fff1f0/cf1322?text=Oil+1L"},
    {"item": "Amul Butter", "brand": "Amul", "category": "Dairy & Eggs", 
     "price": 56, "stock": 200, "image": "https://placehold.co/400x400/feffe6/d4b106?text=Butter"}
]

    db.catalog.delete_many({}) # Wipe old testing data
    db.catalog.insert_many(test_products)
    print("✅ Professional testing catalog inserted with images!")

if __name__ == "__main__":
    seed_professional_data()