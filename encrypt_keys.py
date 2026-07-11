# encrypt_keys.py
#
# ONE-OFF PROVISIONING SCRIPT — run this manually and only when you actually
# want to (re)create users.json from the plaintext `users` dict below.
#
# IMPORTANT: everything that writes to disk is now behind `if __name__ ==
# "__main__":`. This file used to run its top-level code as a side effect
# any time something imported it — including strategies.py's auto-loader,
# which scans and imports every .py file sitting next to ultimate_scanner.py
# looking for strategy modules. That meant every time ultimate_scanner.py
# started up, this script silently re-ran and overwrote users.json with a
# blank template (empty encrypted keys, no password_hash), wiping out
# whatever password you'd just set with set_password.py. Guarding it behind
# __main__ means it only executes when you explicitly run:
#     python encrypt_keys.py
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


def main():
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

    if os.path.exists("users.json"):
        confirm = input(
            "users.json already exists. Running this WILL OVERWRITE it "
            "(including any password_hash values already set) with the "
            "plaintext `users` dict hardcoded in this script. Type 'yes' "
            "to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted — nothing was written.")
            return

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
    print("⚠️  Note: password_hash was NOT set. Run set_password.py for each "
          "user before starting the service, or nobody will be able to log in.")


if __name__ == "__main__":
    main()