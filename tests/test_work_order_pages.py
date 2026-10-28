
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database for page tests."""
    test_db = tmp_path / "test_work_order_pages.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    conn = sqlite3.connect(test_db)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );
        
        
        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        )
        VALUES (
            1, 'admin_test', 'admin@test.local', 'unused',
            'admin', 1, 0, '2026-01-01'
        );
    """)
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, test_db


def authenticate(client, user_id):
    """Authenticate through Flask-Login's test session."""
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_work_orders_page_requires_login(test_env):
    client, _ = test_env

    response = client.get(
        "/work-orders",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_work_order_detail_page_requires_login(test_env):
    client, _ = test_env

    response = client.get(
        "/work-orders/1",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_work_orders_page_renders_for_logged_in_user(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.get("/work-orders")

    assert response.status_code == 200


def test_work_order_detail_page_renders_for_logged_in_user(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.get("/work-orders/1")

    assert response.status_code == 200
