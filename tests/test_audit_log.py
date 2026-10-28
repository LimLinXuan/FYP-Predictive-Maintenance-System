import csv
import io
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    """Create an isolated temporary database for audit-log tests."""
    db_path = tmp_path / "test_audit_log.db"

    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

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

        INSERT INTO users
            (id, username, email, password_hash, role,
             is_active, must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused',
             'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused',
             'technician', 1, 0, '2026-01-01');

        INSERT INTO audit_log
            (timestamp, username, action, target, details)
        VALUES
            ('2026-10-01T09:00:00', 'admin_test',
             'LOGIN', 'admin_test', 'Successful login'),
            ('2026-10-02T10:00:00', 'tech_test',
             'CREATE_WORK_ORDER', 'WO-001', 'Machine AST-001'),
            ('2026-10-03T11:00:00', 'admin_test',
             'DISABLE_USER', 'tech_test', 'Account disabled');
    """)
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, db_path


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


def test_audit_log_admin_can_view_page(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get("/admin/audit-log")

    assert response.status_code == 200
    assert b"admin_test" in response.data


def test_audit_log_filters_by_action(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get(
        "/admin/audit-log?action=LOGIN"
    )

    assert response.status_code == 200
    assert b"Successful login" in response.data
    assert b"Machine AST-001" not in response.data


def test_audit_log_filters_by_search_term(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get(
        "/admin/audit-log?search=AST-001"
    )

    assert response.status_code == 200
    assert b"Machine AST-001" in response.data
    assert b"Successful login" not in response.data


def test_audit_log_filters_by_date_range(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get(
        "/admin/audit-log"
        "?date_from=2026-10-02"
        "&date_to=2026-10-02"
    )

    assert response.status_code == 200
    assert b"Machine AST-001" in response.data
    assert b"Successful login" not in response.data
    assert b"Account disabled" not in response.data


def test_audit_log_all_action_filter(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get("/admin/audit-log?action=ALL")

    assert response.status_code == 200
    assert b"Successful login" in response.data
    assert b"Machine AST-001" in response.data
    assert b"Account disabled" in response.data


def test_audit_log_invalid_page_is_clamped(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get("/admin/audit-log?page=0")

    assert response.status_code == 200


def test_audit_log_technician_is_forbidden(audit_env):
    client, _ = audit_env
    authenticate(client, 2)

    response = client.get("/admin/audit-log")

    assert response.status_code == 403


def test_export_audit_log_returns_csv(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get("/admin/audit-log/export")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment; filename=audit_log.csv" in (
        response.headers.get("Content-Disposition", "")
    )

    rows = list(csv.DictReader(io.StringIO(
        response.get_data(as_text=True)
    )))

    assert len(rows) == 3
    assert rows[0]["action"] == "DISABLE_USER"


def test_export_audit_log_applies_action_filter(audit_env):
    client, _ = audit_env
    authenticate(client, 1)

    response = client.get(
        "/admin/audit-log/export?action=LOGIN"
    )

    assert response.status_code == 200

    rows = list(csv.DictReader(io.StringIO(
        response.get_data(as_text=True)
    )))

    assert len(rows) == 1
    assert rows[0]["action"] == "LOGIN"


def test_export_audit_log_records_export_action(audit_env):
    client, db_path = audit_env
    authenticate(client, 1)

    response = client.get("/admin/audit-log/export")

    assert response.status_code == 200

    logs = read_db(
        db_path,
        """
        SELECT username, action, target, details
        FROM audit_log
        WHERE action = 'EXPORT_AUDIT_LOG'
        """,
    )

    assert len(logs) == 1
    assert logs[0]["username"] == "admin_test"
    assert logs[0]["target"] == "Audit Log"
    assert "3 rows exported" in logs[0]["details"]


def test_export_audit_log_technician_is_forbidden(audit_env):
    client, _ = audit_env
    authenticate(client, 2)

    response = client.get("/admin/audit-log/export")

    assert response.status_code == 403

# -------------------- SQL INJECTION --------------------

import pytest


@pytest.mark.parametrize("payload", [
    "' OR 1=1 --",
    "x'; DROP TABLE audit_log; --",
])
def test_audit_log_search_rejects_injection(audit_env, payload):
    client, db_path = audit_env
    authenticate(client, 1)

    response = client.get(
        "/admin/audit-log",
        query_string={"search": payload},
    )

    assert response.status_code == 200

    # Verify that the audit_log table still exists.
    tables = read_db(
        db_path,
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'audit_log'",
    )
    assert len(tables) == 1

    # Verify that the page remains accessible.
    again = client.get("/admin/audit-log")
    assert again.status_code == 200