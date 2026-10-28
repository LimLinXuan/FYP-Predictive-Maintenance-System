
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def admin_username_test_db(tmp_path, monkeypatch):
    """Create an isolated SQLite database for admin username tests."""
    db_path = tmp_path / "test_is_admin_username.db"

    # Ensure the application connects to the temporary test database.
    monkeypatch.setattr(app_module, "DB", str(db_path))

    # Reset database initialization state so the temporary DB is configured.
    if hasattr(app_module, "_db_configured"):
        monkeypatch.setattr(app_module, "_db_configured", False)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL
            )
            """
        )

        conn.executemany(
            """
            INSERT INTO users (id, username, role)
            VALUES (?, ?, ?)
            """,
            [
                (1, "admin_test", "admin"),
                (2, "technician_test", "technician"),
            ],
        )

    return db_path


def test_is_admin_username_returns_true_for_admin(admin_username_test_db):
    """Return True when the username belongs to an admin."""
    result = app_module._is_admin_username("admin_test")

    assert result is True


def test_is_admin_username_returns_false_for_non_admin(
    admin_username_test_db,
):
    """Return False when the username belongs to a non-admin."""
    result = app_module._is_admin_username("technician_test")

    assert result is False


def test_is_admin_username_returns_false_for_unknown_user(
    admin_username_test_db,
):
    """Return False when the username does not exist."""
    result = app_module._is_admin_username("user_that_does_not_exist")

    assert result is False
