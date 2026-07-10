#!/usr/bin/env python3
"""
Set or reset the login password for a user in users.json.

Run this ON THE VPS, from /opt/alpha_scanner (same directory as users.json).

Usage:
    python3 set_password.py <user_id>

You'll be prompted for the new password (input is hidden, not echoed to the
terminal or saved in shell history). Do this once for every existing user
before restarting the service, otherwise nobody will be able to log in
(the new login code fails closed if password_hash is missing/empty).
"""
import json
import sys
import getpass

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("werkzeug not found. Activate the venv first:")
    print("  source venv/bin/activate")
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 set_password.py <user_id>")
        sys.exit(1)
    user_id = sys.argv[1]

    try:
        with open("users.json", "r") as f:
            users = json.load(f)
    except FileNotFoundError:
        print("users.json not found in current directory. cd into /opt/alpha_scanner first.")
        sys.exit(1)

    if user_id not in users:
        print(f"User '{user_id}' not found. Existing users: {list(users.keys())}")
        sys.exit(1)

    pw1 = getpass.getpass(f"New password for '{user_id}': ")
    pw2 = getpass.getpass("Confirm password: ")

    if pw1 != pw2:
        print("Passwords do not match. Aborted — nothing was written.")
        sys.exit(1)
    if len(pw1) < 8:
        print("Password must be at least 8 characters. Aborted — nothing was written.")
        sys.exit(1)

    users[user_id]["password_hash"] = generate_password_hash(pw1)

    with open("users.json", "w") as f:
        json.dump(users, f, indent=2)

    print(f"\n✅ Password set for '{user_id}'.")
    print("Repeat this for every other user, then restart the service:")
    print("  sudo systemctl restart alpha_scanner")


if __name__ == "__main__":
    main()
