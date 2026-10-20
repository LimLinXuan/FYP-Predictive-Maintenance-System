import sqlite3

import pytest
import app as app_module


@pytest.fixture
def user_management_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database for user-management tests."""
    test_db = tmp_path / "test_user_management.db"

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
            (id, username, email, password_hash, role,
             is_active, must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused', 'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (3, 'tech2_test', 'tech2@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (4, 'inactive_test', 'inactive@test.local', 'unused', 'technician', 0, 0, '2026-01-01'),
            (5, 'admin2_test', 'admin2@test.local', 'unused', 'admin', 1, 0, '2026-01-01');
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


# -------------------- CREATE USER --------------------

def test_create_user_success(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/create", data={
        "username": "new_tech",
        "email": "new_tech@test.local",
        "password": "NewPass123!",
        "role": "technician",
    })

    assert response.status_code == 302

    users = read_db(
        db,
        "SELECT username, email, role, is_active, "
        "must_change_password, password_hash "
        "FROM users WHERE username = ?",
        ("new_tech",),
    )
    assert len(users) == 1
    assert users[0]["email"] == "new_tech@test.local"
    assert users[0]["role"] == "technician"
    assert users[0]["is_active"] == 1
    assert users[0]["must_change_password"] == 1
    assert app_module.bcrypt.check_password_hash(
        users[0]["password_hash"], "NewPass123!"
    )

    logs = read_db(
        db,
        "SELECT username, action, target FROM audit_log "
        "WHERE action = 'CREATE_USER'",
    )
    assert len(logs) == 1
    assert logs[0]["username"] == "admin_test"
    assert logs[0]["target"] == "new_tech"


def test_create_user_rejects_duplicate_username(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/create", data={
        "username": "tech_test",
        "email": "another@test.local",
        "password": "NewPass123!",
        "role": "technician",
    })

    assert response.status_code == 302
    assert len(read_db(
        db, "SELECT id FROM users WHERE username = 'tech_test'"
    )) == 1
    assert read_db(
        db, "SELECT id FROM users WHERE email = 'another@test.local'"
    ) == []
    assert read_db(
        db, "SELECT id FROM audit_log WHERE action = 'CREATE_USER'"
    ) == []


def test_create_user_rejects_invalid_role(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/create", data={
        "username": "bad_role_user",
        "email": "bad_role@test.local",
        "password": "NewPass123!",
        "role": "superuser",
    })

    assert response.status_code == 302
    assert read_db(
        db, "SELECT id FROM users WHERE username = 'bad_role_user'"
    ) == []


def test_create_user_defaults_role_to_technician(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/create", data={
        "username": "default_role_user",
        "email": "default_role@test.local",
        "password": "NewPass123!",
    })

    assert response.status_code == 302

    users = read_db(
        db, "SELECT role FROM users WHERE username = ?",
        ("default_role_user",),
    )
    assert len(users) == 1
    assert users[0]["role"] == "technician"


def test_create_user_requires_admin(user_management_env):
    client, db = user_management_env
    authenticate(client, 2)

    response = client.post("/admin/users/create", data={
        "username": "unauthorized_user",
        "email": "unauthorized@test.local",
        "password": "NewPass123!",
        "role": "technician",
    })

    assert response.status_code in (302, 403)
    assert read_db(
        db, "SELECT id FROM users WHERE username = 'unauthorized_user'"
    ) == []


# -------------------- TOGGLE ACTIVE --------------------

def test_toggle_active_disables_active_user(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/2/toggle-active")

    assert response.status_code == 302

    user = read_db(
        db, "SELECT is_active FROM users WHERE id = 2"
    )[0]
    assert user["is_active"] == 0

    logs = read_db(
        db,
        "SELECT action, target FROM audit_log "
        "WHERE action = 'DISABLE_USER'",
    )
    assert len(logs) == 1
    assert logs[0]["target"] == "tech_test"


def test_toggle_active_enables_disabled_user(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/4/toggle-active")

    assert response.status_code == 302

    user = read_db(
        db, "SELECT is_active FROM users WHERE id = 4"
    )[0]
    assert user["is_active"] == 1

    logs = read_db(
        db,
        "SELECT action, target FROM audit_log "
        "WHERE action = 'ENABLE_USER'",
    )
    assert len(logs) == 1
    assert logs[0]["target"] == "inactive_test"


def test_toggle_active_cannot_toggle_self(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/1/toggle-active")

    assert response.status_code == 302

    user = read_db(
        db, "SELECT is_active FROM users WHERE id = 1"
    )[0]
    assert user["is_active"] == 1

    logs = read_db(
        db,
        "SELECT action, details FROM audit_log "
        "WHERE action = 'SECURITY_ALERT'",
    )
    assert len(logs) == 1
    assert "Attempted to toggle self active status" in logs[0]["details"]


def test_toggle_active_rejects_missing_user(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    response = client.post("/admin/users/999/toggle-active")

    assert response.status_code == 302
    assert read_db(db, "SELECT id FROM audit_log") == []


def test_toggle_active_cannot_disable_last_active_admin(user_management_env):
    client, db = user_management_env
    authenticate(client, 1)

    # Remove the second admin so admin_test is the only active admin.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE users SET is_active = 0 WHERE id = 5")
    conn.commit()
    conn.close()

    response = client.post("/admin/users/1/toggle-active")

    assert response.status_code == 302

    user = read_db(
        db, "SELECT is_active FROM users WHERE id = 1"
    )[0]
    assert user["is_active"] == 1

    logs = read_db(
        db, "SELECT id FROM audit_log WHERE action = 'DISABLE_USER'"
    )
    assert logs == []
