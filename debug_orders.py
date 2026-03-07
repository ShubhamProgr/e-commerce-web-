import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from bson.objectid import ObjectId

load_dotenv('.env')
uri = os.getenv('MONGO_URL')
client = MongoClient(uri, tlsCAFile=certifi.where())
db = client['online_store']

# Check users
users = list(db.users.find({}, {'username': 1, 'orders': 1}))
print('Users found:', len(users))
for user in users:
    print(f'User: {user.get("username")}, Orders count: {len(user.get("orders", []))}')

# Create a sample order for testing
sample_order = {
    "_id": ObjectId(),
    "order_date": datetime.utcnow(),
    "items": [
        {
            "product_id": "sample_id",
            "product_name": "Sample Product",
            "brand": "Sample Brand",
            "quantity": 2,
            "price_per_unit": 100,
            "item_total": 200
        }
    ],
    "total_amount": 200,
    "status": "completed",
    "username": "customer",
    "email": "customer@example.com"
}

# Add sample order to a user
db.users.update_one(
    {"username": "customer"},
    {"$push": {"orders": sample_order}}
)
print("Added sample order to customer user")

# Check if aggregation works
pipeline = [
    {'$unwind': '$orders'},
    {'$project': {
        '_id': '$orders._id',
        'username': '$orders.username',
        'email': '$orders.email',
        'total_amount': '$orders.total_amount',
        'order_date': '$orders.order_date',
        'items': '$orders.items',
        'status': '$orders.status'
    }}
]
orders = list(db.users.aggregate(pipeline))
print('Orders found via aggregation:', len(orders))
for order in orders:
    print(f"Order: {order['username']} - {order['total_amount']} - {order['status']}")