import sqlite3

import pytest
import app as app_module


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Use an isolated temporary database for authentication tests."""
    test_db = tmp_path / "test_auth.db"

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
    """)

    password_hash = (
        app_module.bcrypt
        .generate_password_hash("TestPass123!")
        .decode("utf-8")
    )

    conn.executemany("""
        INSERT INTO users
            (id, username, email, password_hash, role,
             is_active, must_change_password, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, '2026-01-01')
    """, [
        (1, "admin_test", "admin@test.local",
         password_hash, "admin", 1),
        (2, "tech_test", "tech@test.local",
         password_hash, "technician", 1),
        (3, "disabled_test", "disabled@test.local",
         password_hash, "technician", 0),
    ])

    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, test_db

@pytest.mark.parametrize("payload", [
    "admin' OR '1'='1' --",
    "' OR 1=1 --",
    "admin'--",
    "x'; DROP TABLE users; --",
])
def test_login_rejects_sql_injection(auth_env, payload):
    client, db = auth_env

    response = client.post(
        "/login",
        data={
            "username": payload,
            "password": "anything",
        },
    )

    # The injection attempt must not authenticate the user.
    assert response.status_code == 401

    # The users table must still exist.
    users = read_db(
        db,
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'users'",
    )
    assert len(users) == 1

    # The application should still handle a subsequent login.
    again = client.post(
        "/login",
        data={
            "username": payload,
            "password": "anything",
        },
    )
    assert again.status_code == 401

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


# -------------------- LOGIN --------------------

def test_login_get_page(auth_env):
    client, _ = auth_env

    response = client.get("/login")

    assert response.status_code == 200


def test_login_success_updates_last_login_and_audit_log(auth_env):
    client, db = auth_env

    response = client.post("/login", data={
        "username": "tech_test",
        "password": "TestPass123!",
    })

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    user = read_db(
        db, "SELECT last_login FROM users WHERE id = 2"
    )[0]
    assert user["last_login"] is not None

    logs = read_db(
        db,
        "SELECT username, action, details FROM audit_log "
        "WHERE action = 'LOGIN'",
    )
    assert len(logs) == 1
    assert logs[0]["username"] == "tech_test"
    assert logs[0]["details"] == "Success"


def test_login_success_honours_next_parameter(auth_env):
    client, _ = auth_env

    response = client.post(
        "/login?next=/admin/users",
        data={
            "username": "admin_test",
            "password": "TestPass123!",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/users")

# -------------------- OPEN REDIRECT --------------------

@pytest.mark.parametrize("target", [
    "https://evil.example",
    "//evil.example",
    "/\\evil.example",
])
def test_login_rejects_external_next_parameter(auth_env, target):
    client, _ = auth_env

    response = client.post(
        f"/login?next={target}",
        data={
            "username": "tech_test",
            "password": "TestPass123!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "evil.example" not in response.headers.get("Location", "")

def test_login_wrong_password_returns_401(auth_env):
    client, db = auth_env

    response = client.post("/login", data={
        "username": "tech_test",
        "password": "WrongPassword!",
    })

    assert response.status_code == 401

    logs = read_db(
        db,
        "SELECT username, action FROM audit_log "
        "WHERE action = 'FAILED_LOGIN'",
    )
    assert len(logs) == 1
    assert logs[0]["username"] == "tech_test"


def test_login_unknown_username_returns_401(auth_env):
    client, db = auth_env

    response = client.post("/login", data={
        "username": "unknown_test",
        "password": "TestPass123!",
    })

    assert response.status_code == 401

    logs = read_db(
        db,
        "SELECT username, action FROM audit_log "
        "WHERE action = 'FAILED_LOGIN'",
    )
    assert len(logs) == 1
    assert logs[0]["username"] == "unknown_test"


def test_login_disabled_account_returns_403(auth_env):
    client, db = auth_env

    response = client.post("/login", data={
        "username": "disabled_test",
        "password": "TestPass123!",
    })

    assert response.status_code == 403

    user = read_db(
        db, "SELECT last_login FROM users WHERE id = 3"
    )[0]
    assert user["last_login"] is None

    logs = read_db(
        db,
        "SELECT details FROM audit_log "
        "WHERE action = 'FAILED_LOGIN'",
    )
    assert len(logs) == 1
    assert logs[0]["details"] == "Account disabled"


def test_authenticated_user_visiting_login_is_redirected(auth_env):
    client, _ = auth_env
    authenticate(client, 1)

    response = client.get("/login")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


# -------------------- LOGOUT --------------------

def test_logout_redirects_and_records_audit_log(auth_env):
    client, db = auth_env
    authenticate(client, 1)

    response = client.get("/logout")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    logs = read_db(
        db,
        "SELECT username, action FROM audit_log "
        "WHERE action = 'LOGOUT'",
    )
    assert len(logs) == 1
    assert logs[0]["username"] == "admin_test"


def test_logout_requires_authentication(auth_env):
    client, _ = auth_env

    response = client.get("/logout")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
