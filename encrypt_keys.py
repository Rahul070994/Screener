# encrypt_keys.py
import json
import os
from cryptography.fernet import Fernet
import hashlib
import base64

# Load master key from .env
from dotenv import load_dotenv
load_dotenv()

def get_cipher():
    master_key = os.getenv("MASTER_SECRET_KEY")
    if not master_key:
        raise ValueError("MASTER_SECRET_KEY not set in .env file")
    key = base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest())
    return Fernet(key)

cipher = get_cipher()

# Your plaintext users
users = {
    "rahul": {
        "name": "Rahul",
        "kite_api_key": "",
        "kite_api_secret": ""
    }
    # Add more users as needed
}

# Encrypt the keys
encrypted_users = {}
for user_id, data in users.items():
    encrypted_users[user_id] = {
        "name": data["name"],
        "kite_api_key": cipher.encrypt(data["kite_api_key"].encode()).decode(),
        "kite_api_secret": cipher.encrypt(data["kite_api_secret"].encode()).decode(),
    }

# Save to users.json
with open("users.json", "w") as f:
    json.dump(encrypted_users, f, indent=2)

print("✅ users.json created with encrypted keys!")