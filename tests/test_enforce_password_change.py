
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def password_change_env(tmp_path, monkeypatch):
    """Create an isolated database for password-change enforcement tests."""
    db_path = tmp_path / "test_enforce_password_change.db"

    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)
    monkeypatch.setitem(app_module.app.config, "TESTING", True)

    password_hash = (
        app_module.bcrypt
        .generate_password_hash("TestPass123!")
        .decode("utf-8")
    )

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

    conn.executemany("""
        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, '2026-01-01')
    """, [
        (
            1, "must_change", "must_change@test.local",
            password_hash, "technician", 1
        ),
        (
            2, "normal_user", "normal_user@test.local",
            password_hash, "technician", 0
        ),
    ])

    conn.commit()
    conn.close()

    with app_module.app.test_client() as client:
        yield client, db_path


def authenticate(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_required_password_change_redirects_dashboard(password_change_env):
    """A user who must change their password cannot access the dashboard."""
    client, _ = password_change_env
    authenticate(client, 1)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/change-password")


def test_required_password_change_allows_change_password_page(
    password_change_env,
):
    """The password-change endpoint is exempt from enforcement."""
    client, _ = password_change_env
    authenticate(client, 1)

    response = client.get("/change-password", follow_redirects=False)

    # The request must not be redirected back to change-password
    # by enforce_password_change itself.
    assert not (
        response.status_code == 302
        and response.headers["Location"].endswith("/change-password")
    )


def test_required_password_change_allows_logout(password_change_env):
    """The logout endpoint is exempt from password-change enforcement."""
    client, _ = password_change_env
    authenticate(client, 1)

    response = client.get("/logout", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_normal_user_can_access_dashboard(password_change_env):
    """A user who does not need to change their password is not redirected."""
    client, _ = password_change_env
    authenticate(client, 2)

    response = client.get("/", follow_redirects=False)

    assert not (
        response.status_code == 302
        and response.headers["Location"].endswith("/change-password")
    )


def test_unauthenticated_user_is_not_redirected_by_enforcement(
    password_change_env,
):
    """An unauthenticated request is not redirected by this hook."""
    client, _ = password_change_env

    # /login should remain accessible without authentication.
    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 200
