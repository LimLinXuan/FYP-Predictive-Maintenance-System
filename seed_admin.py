"""
seed_admin.py
-------------
One-time script to create the FIRST admin account (chicken-and-egg problem:
UC10 Manage Users requires being logged in as admin already).

Run once from the same folder as app.py:
    python seed_admin.py
"""

import sqlite3
from datetime import datetime
from flask_bcrypt import generate_password_hash

DB = r"C:\Users\limli\Inti Folder\FYP\machine_monitor.db"

conn = sqlite3.connect(DB)
conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'technician',
        is_active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login TEXT
    )
""")
try:
    conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 1")
except sqlite3.OperationalError:
    pass

existing = conn.execute("SELECT id FROM users WHERE username=?", ("admin",)).fetchone()
if existing:
    print("Admin already exists — skipping.")
else:
    pw_hash = generate_password_hash("ChangeMe123!").decode("utf-8")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, role, is_active, must_change_password, created_at) "
        "VALUES (?, ?, ?, 'admin', 1, 1, ?)",
        ("admin", "admin@example.com", pw_hash, datetime.utcnow().isoformat())
    )
    conn.commit()
    print("Admin account created -> username: admin / password: ChangeMe123!")
    print("You will be forced to set a new password on first login.")

conn.close()