import os, certifi, pymongo
from dotenv import load_dotenv

# Load connection and connect to Atlas
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
client = pymongo.MongoClient(os.getenv("MONGO_URL"), tlsCAFile=certifi.where())
db = client["online_store"]

def seed_professional_data():
    # Modern e-commerce schema: include SKUs, brands, and high-quality placeholders
    test_products = [
        {"sku": "LPT-MB-M3", "item": "MacBook Air M3", "brand": "Apple", "category": "Laptops", 
         "price": 114900, "stock": 5, "image": "https://placehold.co/600x400/f8f9fa/0d6efd?text=MacBook+Air+M3"},
        {"sku": "PHN-IP-15", "item": "iPhone 15 Pro", "brand": "Apple", "category": "Phones", 
         "price": 134900, "stock": 8, "image": "https://placehold.co/600x400/f8f9fa/0d6efd?text=iPhone+15+Pro"},
        {"sku": "ACC-MX-3S", "item": "Logitech MX Master 3S", "brand": "Logitech", "category": "Accessories", 
         "price": 10995, "stock": 15, "image": "https://placehold.co/600x400/f8f9fa/0d6efd?text=MX+Master+3S"},
        {"sku": "MON-SAM-G7", "item": "Samsung Odyssey G7", "brand": "Samsung", "category": "Monitors", 
         "price": 45000, "stock": 3, "image": "https://placehold.co/600x400/f8f9fa/0d6efd?text=Odyssey+G7"}
    ]

    db.catalog.delete_many({}) # Wipe old testing data
    db.catalog.insert_many(test_products)
    print("✅ Professional testing catalog inserted with images!")

if __name__ == "__main__":
    seed_professional_data()