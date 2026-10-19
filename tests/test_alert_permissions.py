import sqlite3

import pytest
import app as app_module

from datetime import datetime, timezone

datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
    timespec="seconds"
)

@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database, never the real project database."""
    test_db = tmp_path / "test_alert_permissions.db"

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

        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            alert_status TEXT NOT NULL,
            assigned_to TEXT
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            type TEXT NOT NULL DEFAULT 'system',
            record_id INTEGER,
            created_at TEXT NOT NULL,
            read_at TEXT
        );

        INSERT INTO users
            (id, username, email, password_hash, role, is_active,
             must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused', 'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (3, 'tech2_test', 'tech2@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (4, 'inactive_test', 'inactive@test.local', 'unused', 'technician', 0, 0, '2026-01-01');

        INSERT INTO decision_log (record_id, alert_status, assigned_to)
        VALUES
            (101, 'OPEN', NULL),
            (102, 'OPEN', 'tech_test'),
            (103, 'RESOLVED', NULL),
            (104, 'FALSE_POSITIVE', NULL);
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


def read_db(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_unauthenticated_user_cannot_assign_alert(test_env):
    client, _ = test_env

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_admin_can_assign_alert_to_active_technician(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 200
    assert response.get_json()["assigned_to"] == "tech_test"
    assert response.get_json()["alert_status"] == "ASSIGNED"

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] == "tech_test"
    assert row["alert_status"] == "ASSIGNED"


def test_technician_can_claim_unassigned_alert(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post("/api/assign/101", json={})

    assert response.status_code == 200

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] == "tech_test"
    assert row["alert_status"] == "ASSIGNED"


def test_technician_cannot_reassign_claimed_alert(test_env):
    client, db_path = test_env
    authenticate(client, 3)

    response = client.post("/api/assign/102", json={})

    assert response.status_code == 403
    assert read_db(
        db_path,
        "SELECT assigned_to FROM decision_log WHERE record_id = 102",
    )[0]["assigned_to"] == "tech_test"


@pytest.mark.parametrize("record_id", [103, 104])
def test_closed_alert_cannot_be_assigned(test_env, record_id):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        f"/api/assign/{record_id}",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 400

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = ?",
        (record_id,),
    )[0]
    assert row["assigned_to"] is None
    assert row["alert_status"] in ("RESOLVED", "FALSE_POSITIVE")


def test_nonexistent_alert_returns_404(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/999",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({}, "technician username required"),
        ({"technician": "unknown_test"}, "Invalid technician"),
        ({"technician": "inactive_test"}, "Invalid technician"),
    ],
)
def test_admin_must_select_active_technician(
    test_env, payload, expected_message
):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post("/api/assign/101", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_message

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] is None
    assert row["alert_status"] == "OPEN"


def test_assignment_creates_audit_log_and_notification(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
    )
    assert response.status_code == 200

    audit_rows = read_db(
        db_path,
        "SELECT username, action, target FROM audit_log",
    )
    assert any(
        row["username"] == "admin_test"
        and row["action"] == "ASSIGN_ALERT"
        and row["target"] == "Record #101"
        for row in audit_rows
    )

    notifications = read_db(
        db_path,
        "SELECT username, title, record_id FROM notifications",
    )
    assert any(
        row["username"] == "tech_test"
        and row["record_id"] == 101
        for row in notifications
    )