
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database, never the real project database."""
    test_db = tmp_path / "test_work_orders.db"

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

        CREATE TABLE work_orders (
            wo_id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER,
            machine_id TEXT,
            asset_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            recommended_action TEXT,
            risk_level TEXT,
            risk_score INTEGER,
            priority TEXT NOT NULL DEFAULT 'MEDIUM',
            status TEXT NOT NULL DEFAULT 'OPEN',
            assigned_to TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            due_date TEXT,
            completed_at TEXT,
            completion_note TEXT,
            accepted_at TEXT,
            started_at TEXT,
            technician_notes TEXT,
            resolution_note TEXT,
            verification_note TEXT,
            verified_by TEXT,
            verified_at TEXT
        );

        INSERT INTO users
            (id, username, email, password_hash, role, is_active,
             must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused',
             'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused',
             'technician', 1, 0, '2026-01-01'),
            (3, 'inactive_test', 'inactive@test.local', 'unused',
             'technician', 0, 0, '2026-01-01');

        INSERT INTO work_orders
            (wo_id, title, priority, status, created_by, created_at)
        VALUES
            (1, 'Test maintenance task', 'MEDIUM', 'OPEN',
             'admin_test', '2026-01-01');
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


def test_unauthenticated_user_cannot_view_work_orders(test_env):
    client, _ = test_env

    response = client.get("/api/work-orders", follow_redirects=False)

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_unauthenticated_user_cannot_create_work_order(test_env):
    client, _ = test_env

    response = client.post(
        "/api/work-orders",
        json={"title": "Unauthorized task"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_create_work_order_requires_title(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post("/api/work-orders", json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title is required"


def test_create_work_order_rejects_blank_title(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders",
        json={"title": "   "},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "title is required"


def test_create_work_order_rejects_invalid_priority(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders",
        json={
            "title": "Invalid priority task",
            "priority": "URGENT_UNKNOWN",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid priority"


def test_admin_can_create_work_order(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders",
        json={
            "title": "Inspect machine",
            "machine_id": "MCH-005",
            "asset_id": "AST-1005",
            "description": "Inspect abnormal vibration",
            "recommended_action": "Check the bearings",
            "priority": "HIGH",
        },
    )

    assert response.status_code == 201
    result = response.get_json()
    assert result["message"] == "Work order created"
    assert isinstance(result["wo_id"], int)

    rows = read_db(
        db_path,
        """
        SELECT title, machine_id, asset_id, priority, status,
               created_by
        FROM work_orders
        WHERE wo_id = ?
        """,
        (result["wo_id"],),
    )

    assert len(rows) == 1
    assert rows[0]["title"] == "Inspect machine"
    assert rows[0]["machine_id"] == "MCH-005"
    assert rows[0]["asset_id"] == "AST-1005"
    assert rows[0]["priority"] == "HIGH"
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["created_by"] == "admin_test"

    audit_rows = read_db(
        db_path,
        """
        SELECT action, target
        FROM audit_log
        WHERE username = ?
        """,
        ("admin_test",),
    )
    assert any(row["action"] == "CREATE_WORK_ORDER" for row in audit_rows)


def test_technician_cannot_assign_work_order_during_creation(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/work-orders",
        json={
            "title": "Technician-created task",
            "assigned_to": "tech_test",
        },
    )

    assert response.status_code == 403
    assert "Admin access required" in response.get_json()["error"]

    rows = read_db(
        db_path,
        "SELECT wo_id FROM work_orders WHERE title = ?",
        ("Technician-created task",),
    )
    assert rows == []


def test_admin_can_cancel_work_order(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 200
    assert "updated to CANCELLED" in response.get_json()["message"]

    rows = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "CANCELLED"


def test_technician_cannot_cancel_work_order(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == (
        "Forbidden: Admin access required"
    )

    rows = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "OPEN"


def test_work_order_status_rejects_invalid_value(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CLOSED"},
    )

    assert response.status_code == 400
    assert "Invalid status" in response.get_json()["error"]


def test_cancelling_nonexistent_work_order_returns_404(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/9999/status",
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Work order not found"
