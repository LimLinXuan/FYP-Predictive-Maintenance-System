import sqlite3

import pytest
import app as app_module


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database, never the real project database."""
    test_db = tmp_path / "test_users.db"

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

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT
        );

        INSERT INTO users
            (id, username, email, password_hash, role, is_active,
             must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused', 'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (3, 'tech2_test', 'tech2@test.local', 'unused', 'technician', 1, 0, '2026-01-01');
    """)
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, test_db


def authenticate(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def read_db(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


# -------------------- EDIT USER --------------------

def test_edit_user_get_page(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.get("/admin/users/2/edit")

    assert response.status_code == 200


def test_edit_user_success_updates_email_and_role(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/2/edit",
        data={"email": "updated@test.local", "role": "admin"},
    )

    assert response.status_code == 302

    user = read_db(
        db, "SELECT email, role FROM users WHERE id = 2"
    )[0]
    assert user["email"] == "updated@test.local"
    assert user["role"] == "admin"

    logs = read_db(
        db, "SELECT action, target FROM audit_log WHERE action = 'EDIT_USER'"
    )
    assert len(logs) == 1
    assert logs[0]["target"] == "tech_test"


def test_edit_user_rejects_invalid_role(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/2/edit",
        data={"email": "changed@test.local", "role": "superuser"},
    )

    assert response.status_code == 302

    user = read_db(db, "SELECT email, role FROM users WHERE id = 2")[0]
    assert user["email"] == "tech@test.local"
    assert user["role"] == "technician"


def test_edit_user_cannot_remove_own_admin_role(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/1/edit",
        data={"email": "admin@test.local", "role": "technician"},
    )

    assert response.status_code == 302

    user = read_db(db, "SELECT role FROM users WHERE id = 1")[0]
    assert user["role"] == "admin"


def test_edit_user_no_changes(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/2/edit",
        data={"email": "tech@test.local", "role": "technician"},
    )

    assert response.status_code == 302
    assert read_db(db, "SELECT * FROM audit_log") == []


def test_edit_user_not_found(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/999/edit",
        data={"email": "missing@test.local", "role": "technician"},
    )

    assert response.status_code == 302


# -------------------- DELETE USER --------------------

def test_delete_user_success(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post("/admin/users/2/delete")

    assert response.status_code == 302
    assert read_db(db, "SELECT id FROM users WHERE id = 2") == []

    logs = read_db(
        db, "SELECT action, target FROM audit_log WHERE action = 'DELETE_USER'"
    )
    assert len(logs) == 1
    assert logs[0]["target"] == "tech_test"


def test_delete_user_cannot_delete_self(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post("/admin/users/1/delete")

    assert response.status_code == 302
    assert len(read_db(db, "SELECT id FROM users WHERE id = 1")) == 1

    logs = read_db(
        db, "SELECT action FROM audit_log WHERE action = 'SECURITY_ALERT'"
    )
    assert len(logs) == 1


def test_delete_user_not_found(test_env):
    client, db = test_env
    authenticate(client, 1)

    response = client.post("/admin/users/999/delete")

    assert response.status_code == 302
    assert read_db(db, "SELECT * FROM audit_log") == []
