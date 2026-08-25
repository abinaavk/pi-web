"""
Run this once before first launch (and any time you want to change the
password):

    python3 setup_password.py

It writes a salted hash into the config table — the plaintext password
is never stored.
"""
import getpass
import sqlite3
from werkzeug.security import generate_password_hash

DB_PATH = "piweb.db"


def main():
    pw1 = getpass.getpass("New admin password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        print("Passwords did not match. Nothing was changed.")
        return
    if len(pw1) < 8:
        print("Use at least 8 characters.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('password_hash', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (generate_password_hash(pw1),),
    )
    conn.commit()
    conn.close()
    print("Password set.")


if __name__ == "__main__":
    main()
