import sqlite3

import pytest
import app as app_module


@pytest.fixture
def password_env(tmp_path, monkeypatch):
    """Use an isolated temporary SQLite database."""
    db_path = tmp_path / "test_password_management.db"

    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

    password_hash = app_module.bcrypt.generate_password_hash(
        "TestPass123!"
    ).decode("utf-8")

    conn = sqlite3.connect(db_path)
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
    """)

    conn.executemany(
        """
        INSERT INTO users
            (id, username, email, password_hash, role,
             is_active, must_change_password, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "admin_test", "admin@test.local",
             password_hash, "admin", 1, 0, "2026-01-01"),
            (2, "tech_test", "tech@test.local",
             password_hash, "technician", 1, 0, "2026-01-01"),
        ],
    )
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, db_path


def authenticate(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def read_user(db_path, user_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


# ---------------- ADMIN PASSWORD RESET ----------------

def test_admin_reset_password_success(password_env):
    client, db_path = password_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/2/reset-password",
        data={
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        },
    )

    assert response.status_code == 302

    user = read_user(db_path, 2)
    assert app_module.bcrypt.check_password_hash(
        user["password_hash"], "NewPass123!"
    )
    assert user["must_change_password"] == 1


def test_admin_reset_password_too_short(password_env):
    client, db_path = password_env
    authenticate(client, 1)

    old_hash = read_user(db_path, 2)["password_hash"]

    response = client.post(
        "/admin/users/2/reset-password",
        data={
            "new_password": "short",
            "confirm_password": "short",
        },
    )

    assert response.status_code == 302
    assert read_user(db_path, 2)["password_hash"] == old_hash


def test_admin_reset_password_confirmation_mismatch(password_env):
    client, db_path = password_env
    authenticate(client, 1)

    old_hash = read_user(db_path, 2)["password_hash"]

    response = client.post(
        "/admin/users/2/reset-password",
        data={
            "new_password": "NewPass123!",
            "confirm_password": "DifferentPass123!",
        },
    )

    assert response.status_code == 302
    assert read_user(db_path, 2)["password_hash"] == old_hash


def test_admin_reset_password_missing_user(password_env):
    client, _ = password_env
    authenticate(client, 1)

    response = client.post(
        "/admin/users/9999/reset-password",
        data={
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        },
    )

    assert response.status_code == 302


# ---------------- SELF-SERVICE PASSWORD CHANGE ----------------

def test_change_password_success(password_env):
    client, db_path = password_env
    authenticate(client, 2)

    response = client.post(
        "/change-password",
        data={
            "current_password": "TestPass123!",
            "new_password": "UpdatedPass123!",
            "confirm_password": "UpdatedPass123!",
        },
    )

    assert response.status_code == 302

    user = read_user(db_path, 2)
    assert app_module.bcrypt.check_password_hash(
        user["password_hash"], "UpdatedPass123!"
    )
    assert user["must_change_password"] == 0


def test_change_password_wrong_current_password(password_env):
    client, db_path = password_env
    authenticate(client, 2)

    old_hash = read_user(db_path, 2)["password_hash"]

    response = client.post(
        "/change-password",
        data={
            "current_password": "WrongPass123!",
            "new_password": "UpdatedPass123!",
            "confirm_password": "UpdatedPass123!",
        },
    )

    assert response.status_code == 401
    assert read_user(db_path, 2)["password_hash"] == old_hash


def test_change_password_new_password_too_short(password_env):
    client, db_path = password_env
    authenticate(client, 2)

    old_hash = read_user(db_path, 2)["password_hash"]

    response = client.post(
        "/change-password",
        data={
            "current_password": "TestPass123!",
            "new_password": "short",
            "confirm_password": "short",
        },
    )

    assert response.status_code == 400
    assert read_user(db_path, 2)["password_hash"] == old_hash


def test_change_password_confirmation_mismatch(password_env):
    client, db_path = password_env
    authenticate(client, 2)

    old_hash = read_user(db_path, 2)["password_hash"]

    response = client.post(
        "/change-password",
        data={
            "current_password": "TestPass123!",
            "new_password": "UpdatedPass123!",
            "confirm_password": "DifferentPass123!",
        },
    )

    assert response.status_code == 400
    assert read_user(db_path, 2)["password_hash"] == old_hash
