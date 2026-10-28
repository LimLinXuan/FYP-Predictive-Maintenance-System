
import sqlite3

import app as app_module


def test_api_technicians_returns_active_technicians(monkeypatch):
    """Admin receives only active technicians, sorted by username."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)

    conn.executemany(
        """
        INSERT INTO users (
            id,
            username,
            email,
            password_hash,
            role,
            is_active,
            must_change_password,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, '2026-01-01')
        """,
        [
            (
                1,
                "admin_user",
                "admin@test.local",
                "dummy_hash",
                "admin",
                1,
            ),
            (
                2,
                "tech_b",
                "tech_b@test.local",
                "dummy_hash",
                "technician",
                1,
            ),
            (
                3,
                "tech_a",
                "tech_a@test.local",
                "dummy_hash",
                "technician",
                1,
            ),
            (
                4,
                "tech_disabled",
                "disabled@test.local",
                "dummy_hash",
                "technician",
                0,
            ),
        ],
    )

    monkeypatch.setattr(app_module, "get_db", lambda: conn)

    try:
        with app_module.app.test_client() as client:
            with client.session_transaction() as session:
                session["_user_id"] = "1"
                session["_fresh"] = True

            response = client.get("/api/technicians")

        assert response.status_code == 200
        assert response.is_json
        assert response.get_json() == ["tech_a", "tech_b"]

    finally:
        conn.close()
