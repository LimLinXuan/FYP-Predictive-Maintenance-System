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

        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY,
            machine_id TEXT NOT NULL
        );

        CREATE TABLE pm_schedules (
            pm_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            pm_name TEXT NOT NULL,
            frequency_type TEXT NOT NULL,
            frequency_value INTEGER NOT NULL,
            last_performed_at TEXT,
            last_performed_tool_wear REAL,
            next_due_at TEXT,
            status TEXT NOT NULL DEFAULT 'ON_SCHEDULE',
            created_by TEXT,
            created_at TEXT
        );

        CREATE TABLE maintenance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT,
            machine_id TEXT,
            maintenance_type TEXT,
            source_pm_id INTEGER,
            source_wo_id INTEGER,
            description TEXT,
            performed_by TEXT,
            performed_at TEXT,
            downtime_minutes INTEGER,
            created_at TEXT
        );

        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            alert_status TEXT,
            resolved_by TEXT,
            resolved_at TEXT,
            resolution_note TEXT
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

        INSERT INTO assets (asset_id, machine_id)
        VALUES ('ASSET-001', 'M001');
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


# ============================================================
# WORK ORDER TESTS
# ============================================================

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
        SELECT title, machine_id, asset_id, priority, status, created_by
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
        "SELECT action, target FROM audit_log WHERE username = ?",
        ("admin_test",),
    )
    assert any(row["action"] == "CREATE_WORK_ORDER" for row in audit_rows)


def test_technician_cannot_assign_work_order_during_creation(test_env):
    client, _ = test_env
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
        _,
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

    rows = read_db(db_path, "SELECT status FROM work_orders WHERE wo_id = 1")
    assert rows[0]["status"] == "CANCELLED"


def test_technician_cannot_cancel_work_order(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CANCELLED"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "Forbidden: Admin access required"

    rows = read_db(db_path, "SELECT status FROM work_orders WHERE wo_id = 1")
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


def test_unauthenticated_user_cannot_update_work_order_status(test_env):
    client, db_path = test_env

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CANCELLED"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]

    rows = read_db(db_path, "SELECT status FROM work_orders WHERE wo_id = 1")
    assert rows[0]["status"] == "OPEN"


def test_inactive_user_cannot_update_work_order_status(test_env):
    client, db_path = test_env
    authenticate(client, 3)

    response = client.post(
        "/api/work-orders/1/status",
        json={"status": "CANCELLED"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]

    rows = read_db(db_path, "SELECT status FROM work_orders WHERE wo_id = 1")
    assert rows[0]["status"] == "OPEN"


def test_technician_cannot_accept_unassigned_work_order(test_env):
    client, _ = test_env
    authenticate(client, 2)

    response = client.post("/api/work-orders/1/accept")

    assert response.status_code == 403
    assert response.get_json()["error"] == "Forbidden: not assigned to you"

    rows = read_db(
        _,
        "SELECT status, assigned_to FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["assigned_to"] is None


def test_technician_can_save_notes_while_working(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("tech_test", "IN_PROGRESS", 1),
        )

    authenticate(client, 2)
    response = client.post(
        "/api/work-orders/1/notes",
        json={"technician_notes": "  Bearing inspection completed  "},
    )

    assert response.status_code == 200
    assert response.get_json()["technician_notes"] == "Bearing inspection completed"

    rows = read_db(
        db_path,
        "SELECT technician_notes FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["technician_notes"] == "Bearing inspection completed"


def test_complete_work_order_requires_resolution_note(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("tech_test", "IN_PROGRESS", 1),
        )

    authenticate(client, 2)
    response = client.post(
        "/api/work-orders/1/complete",
        json={"resolution_note": "   "},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "resolution_note is required to complete a work order"
    )

    rows = read_db(
        db_path,
        "SELECT status, resolution_note FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "IN_PROGRESS"
    assert rows[0]["resolution_note"] is None


def test_technician_cannot_verify_work_order(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE work_orders SET status = ? WHERE wo_id = ?",
            ("PENDING_VERIFICATION", 1),
        )

    authenticate(client, 2)
    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "approve"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == (
        "Forbidden: only an administrator can verify"
    )

    rows = read_db(
        db_path,
        "SELECT status, verified_by FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "PENDING_VERIFICATION"
    assert rows[0]["verified_by"] is None


def test_technician_can_start_accepted_work_order(test_env, monkeypatch):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("tech_test", "ACCEPTED", 1),
        )

    monkeypatch.setattr(
        app_module, "refresh_asset_status", lambda machine_id: None
    )
    authenticate(client, 2)
    response = client.post("/api/work-orders/1/start")

    assert response.status_code == 200
    assert response.get_json()["status"] == "IN_PROGRESS"

    rows = read_db(
        db_path,
        "SELECT status, started_at FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "IN_PROGRESS"
    assert rows[0]["started_at"] is not None


def test_technician_cannot_start_work_order_before_accepting(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("tech_test", "ASSIGNED", 1),
        )

    authenticate(client, 2)
    response = client.post("/api/work-orders/1/start")

    assert response.status_code == 400
    assert "Cannot start from status ASSIGNED" in response.get_json()["error"]

    rows = read_db(
        db_path,
        "SELECT status, started_at FROM work_orders WHERE wo_id = 1",
    )
    assert rows[0]["status"] == "ASSIGNED"
    assert rows[0]["started_at"] is None


def test_technician_can_complete_work_order_pending_verification(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("tech_test", "IN_PROGRESS", 1),
        )

    authenticate(client, 2)
    response = client.post(
        "/api/work-orders/1/complete",
        json={"resolution_note": "Bearing inspected and repaired"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "PENDING_VERIFICATION"

    rows = read_db(
        db_path,
        """
        SELECT status, resolution_note, completed_at
        FROM work_orders
        WHERE wo_id = 1
        """,
    )
    assert rows[0]["status"] == "PENDING_VERIFICATION"
    assert rows[0]["resolution_note"] == "Bearing inspected and repaired"
    assert rows[0]["completed_at"] is not None


def test_admin_can_reject_work_order_verification(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE work_orders SET status = ? WHERE wo_id = ?",
            ("PENDING_VERIFICATION", 1),
        )

    authenticate(client, 1)
    response = client.post(
        "/api/work-orders/1/verify",
        json={
            "decision": "reject",
            "verification_note": "Repair requires further inspection",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "IN_PROGRESS"

    rows = read_db(
        db_path,
        """
        SELECT status, verified_by, verification_note
        FROM work_orders
        WHERE wo_id = 1
        """,
    )
    assert rows[0]["status"] == "IN_PROGRESS"
    assert rows[0]["verified_by"] == "admin_test"
    assert rows[0]["verification_note"] == "Repair requires further inspection"


def test_admin_can_assign_open_work_order(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/1/assign",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 200
    assert response.get_json()["assigned_to"] == "tech_test"
    assert response.get_json()["status"] == "ASSIGNED"

    rows = read_db(
        db_path,
        "SELECT assigned_to, status FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["assigned_to"] == "tech_test"
    assert rows[0]["status"] == "ASSIGNED"


def test_admin_cannot_assign_work_order_to_inactive_technician(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/1/assign",
        json={"technician": "inactive_test"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid technician"

    rows = read_db(
        db_path,
        "SELECT assigned_to, status FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["assigned_to"] is None
    assert rows[0]["status"] == "OPEN"


def test_technician_cannot_assign_work_order_to_another_user(test_env):
    client, _ = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/work-orders/1/assign",
        json={"technician": "admin_test"},
    )

    assert response.status_code == 403

    rows = read_db(
        _,
        "SELECT assigned_to, status FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["assigned_to"] is None
    assert rows[0]["status"] == "OPEN"


def test_technician_cannot_claim_already_assigned_work_order(test_env):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET assigned_to = ?, status = ?
            WHERE wo_id = ?
            """,
            ("admin_test", "ASSIGNED", 1),
        )

    authenticate(client, 2)
    response = client.post(
        "/api/work-orders/1/assign",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 400
    assert "unassigned open work order" in response.get_json()["error"]

    rows = read_db(
        db_path,
        "SELECT assigned_to, status FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["assigned_to"] == "admin_test"
    assert rows[0]["status"] == "ASSIGNED"


# ============================================================
# METRICS API TESTS
# ============================================================

def test_api_metrics_returns_cached_response(test_env, monkeypatch):
    client, _ = test_env
    authenticate(client, 1)

    expected = {
        "total_records": 10,
        "open_alerts": 3,
        "in_progress": 2,
        "high_risk_count": 4,
        "resolved_alerts": 5,
        "false_positives": 0,
        "resolution_rate": "50.0%",
        "avg_risk_score": 60.0,
        "avg_pred_prob": 0.75,
        "distribution": [],
        "trends": {
            "record_ids": [],
            "risk_scores": [],
            "pred_probs": [],
        },
    }

    monkeypatch.setattr(
        app_module,
        "_cached_response",
        lambda key: expected if key == "metrics" else None,
    )

    response = client.get("/api/metrics")

    assert response.status_code == 200
    assert response.get_json() == expected


def test_api_metrics_calculates_metrics_from_database(test_env, monkeypatch):
    client, db_path = test_env
    authenticate(client, 1)

    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE decision_log ADD COLUMN risk_level TEXT")
        conn.execute("ALTER TABLE decision_log ADD COLUMN risk_score REAL")
        conn.execute("ALTER TABLE decision_log ADD COLUMN pred_prob REAL")

        conn.executemany(
            """
            INSERT INTO decision_log
                (record_id, alert_status, risk_level, risk_score, pred_prob)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (1, "OPEN", "High", 80, 0.8),
                (2, "IN_PROGRESS", "Critical", 90, 0.9),
                (3, "RESOLVED", "Medium", 40, 0.4),
                (4, "RESOLVED", "Low", 20, 0.2),
                (5, "FALSE_POSITIVE", "High", 70, 0.7),
            ],
        )

    monkeypatch.setattr(app_module, "_cached_response", lambda key: None)
    monkeypatch.setattr(
        app_module,
        "_store_cached_response",
        lambda key, payload: payload,
    )

    response = client.get("/api/metrics")

    assert response.status_code == 200
    data = response.get_json()

    assert data["total_records"] == 5
    assert data["open_alerts"] == 1
    assert data["in_progress"] == 1
    assert data["resolved_alerts"] == 2
    assert data["false_positives"] == 1
    assert data["high_risk_count"] == 3
    assert data["resolution_rate"] == "50.0%"
    assert data["avg_risk_score"] == 60.0
    assert data["avg_pred_prob"] == 0.6

    assert sorted(
        data["distribution"],
        key=lambda item: item["risk_level"],
    ) == [
        {"risk_level": "Critical", "count": 1},
        {"risk_level": "High", "count": 2},
        {"risk_level": "Low", "count": 1},
        {"risk_level": "Medium", "count": 1},
    ]

    assert data["trends"] == {
        "record_ids": [],
        "risk_scores": [],
        "pred_probs": [],
    }


# ============================================================
# PM SCHEDULE CREATION API TESTS
# ============================================================

def test_create_pm_schedule_success_days(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "ASSET-001",
            "pm_name": "Weekly Inspection",
            "frequency_type": "DAYS",
            "frequency_value": 7,
        },
    )

    assert response.status_code == 201
    result = response.get_json()
    assert result["message"] == "PM schedule created"

    rows = read_db(
        db_path,
        "SELECT * FROM pm_schedules WHERE pm_id = ?",
        (result["pm_id"],),
    )

    assert len(rows) == 1
    assert rows[0]["asset_id"] == "ASSET-001"
    assert rows[0]["machine_id"] == "M001"
    assert rows[0]["pm_name"] == "Weekly Inspection"
    assert rows[0]["frequency_type"] == "DAYS"
    assert rows[0]["frequency_value"] == 7
    assert rows[0]["next_due_at"] is not None


def test_create_pm_schedule_missing_required_fields(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/pm-schedules",
        json={
            "frequency_type": "DAYS",
            "frequency_value": 7,
        },
    )

    assert response.status_code == 400
    assert "required" in response.get_json()["error"]


def test_create_pm_schedule_invalid_frequency_type(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "ASSET-001",
            "pm_name": "Inspection",
            "frequency_type": "WEEKS",
            "frequency_value": 2,
        },
    )

    assert response.status_code == 400
    assert "frequency_type" in response.get_json()["error"]


@pytest.mark.parametrize("frequency_value", [0, -1, "invalid", None])
def test_create_pm_schedule_invalid_frequency_value(test_env, frequency_value):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "ASSET-001",
            "pm_name": "Inspection",
            "frequency_type": "DAYS",
            "frequency_value": frequency_value,
        },
    )

    assert response.status_code == 400
    assert "frequency_value" in response.get_json()["error"]


def test_create_pm_schedule_asset_not_found(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "ASSET-999",
            "pm_name": "Inspection",
            "frequency_type": "DAYS",
            "frequency_value": 7,
        },
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_create_pm_schedule_forbidden_for_technician(test_env):
    client, _ = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "ASSET-001",
            "pm_name": "Inspection",
            "frequency_type": "DAYS",
            "frequency_value": 7,
        },
    )

    assert response.status_code == 403


# ============================================================
# WORK ORDER VERIFICATION COVERAGE TESTS
# ============================================================

def test_admin_can_approve_work_order_and_resolve_alert(
    test_env, monkeypatch
):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET record_id = ?,
                machine_id = ?,
                asset_id = ?,
                assigned_to = ?,
                status = ?,
                resolution_note = ?,
                completed_at = ?
            WHERE wo_id = ?
            """,
            (
                100, "M001", "ASSET-001", "tech_test",
                "PENDING_VERIFICATION",
                "Bearing repaired", "2026-01-02", 1,
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_log (record_id, alert_status)
            VALUES (?, ?)
            """,
            (100, "OPEN"),
        )

    refreshed_machines = []
    notifications = []

    monkeypatch.setattr(
        app_module,
        "refresh_asset_status",
        lambda machine_id: refreshed_machines.append(machine_id),
    )
    monkeypatch.setattr(
        app_module, "_clear_response_cache", lambda: None
    )
    monkeypatch.setattr(
        app_module,
        "create_notification",
        lambda **kwargs: notifications.append(kwargs),
    )

    authenticate(client, 1)
    response = client.post(
        "/api/work-orders/1/verify",
        json={
            "decision": "approve",
            "verification_note": "Verified successfully",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "CLOSED"

    wo = read_db(
        db_path,
        """
        SELECT status, verified_by, verification_note, verified_at
        FROM work_orders WHERE wo_id = ?
        """,
        (1,),
    )[0]

    assert wo["status"] == "CLOSED"
    assert wo["verified_by"] == "admin_test"
    assert wo["verification_note"] == "Verified successfully"
    assert wo["verified_at"] is not None

    maintenance = read_db(
        db_path,
        """
        SELECT asset_id, machine_id, maintenance_type, source_wo_id,
               description, performed_by
        FROM maintenance_records
        WHERE source_wo_id = ?
        """,
        (1,),
    )

    assert len(maintenance) == 1
    assert maintenance[0]["asset_id"] == "ASSET-001"
    assert maintenance[0]["machine_id"] == "M001"
    assert maintenance[0]["maintenance_type"] == "CORRECTIVE"
    assert maintenance[0]["description"] == "Bearing repaired"
    assert maintenance[0]["performed_by"] == "tech_test"

    alert = read_db(
        db_path,
        """
        SELECT alert_status, resolved_by, resolution_note
        FROM decision_log WHERE record_id = ?
        """,
        (100,),
    )[0]

    assert alert["alert_status"] == "RESOLVED"
    assert alert["resolved_by"] == "admin_test"
    assert alert["resolution_note"] == "Verified successfully"

    audit = read_db(
        db_path,
        "SELECT action FROM audit_log WHERE username = ?",
        ("admin_test",),
    )
    actions = [row["action"] for row in audit]
    assert "VERIFY_WORK_ORDER" in actions
    assert "UPDATE_ALERT" in actions

    assert refreshed_machines == ["M001"]
    assert len(notifications) == 2


def test_verify_work_order_rejects_invalid_decision(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "close"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "decision must be 'approve' or 'reject'"
    )

    wo = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = ?",
        (1,),
    )[0]
    assert wo["status"] == "OPEN"


def test_verify_nonexistent_work_order_returns_404(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/work-orders/9999/verify",
        json={"decision": "approve"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Work order not found"

    assert read_db(db_path, "SELECT * FROM audit_log") == []


def test_verify_work_order_rejects_invalid_status(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    # Fixture work order 1 starts in OPEN status.
    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "approve"},
    )

    assert response.status_code == 400
    assert "Cannot verify from status OPEN" in (
        response.get_json()["error"]
    )

    wo = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = ?",
        (1,),
    )[0]
    assert wo["status"] == "OPEN"


def test_approve_work_order_does_not_resolve_already_resolved_alert(
    test_env, monkeypatch
):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET record_id = ?, machine_id = ?, asset_id = ?,
                status = ?, resolution_note = ?
            WHERE wo_id = ?
            """,
            (
                101, "M001", "ASSET-001",
                "PENDING_VERIFICATION", "Repair completed", 1,
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_log (record_id, alert_status)
            VALUES (?, ?)
            """,
            (101, "RESOLVED"),
        )

    monkeypatch.setattr(
        app_module, "refresh_asset_status", lambda machine_id: None
    )
    monkeypatch.setattr(
        app_module, "_clear_response_cache", lambda: None
    )
    monkeypatch.setattr(
        app_module, "create_notification", lambda **kwargs: None
    )

    authenticate(client, 1)
    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "CLOSED"

    alert = read_db(
        db_path,
        """
        SELECT alert_status, resolved_by
        FROM decision_log WHERE record_id = ?
        """,
        (101,),
    )[0]

    # Existing resolution must not be overwritten.
    assert alert["alert_status"] == "RESOLVED"
    assert alert["resolved_by"] is None

    audit = read_db(
        db_path,
        "SELECT action FROM audit_log",
    )
    actions = [row["action"] for row in audit]
    assert "VERIFY_WORK_ORDER" in actions
    assert "UPDATE_ALERT" not in actions


def test_reject_work_order_without_assignee_does_not_notify_technician(
    test_env, monkeypatch
):
    client, db_path = test_env

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET status = ?, assigned_to = NULL
            WHERE wo_id = ?
            """,
            ("PENDING_VERIFICATION", 1),
        )

    notifications = []
    monkeypatch.setattr(
        app_module, "_clear_response_cache", lambda: None
    )
    monkeypatch.setattr(
        app_module,
        "create_notification",
        lambda **kwargs: notifications.append(kwargs),
    )

    authenticate(client, 1)
    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "reject"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "IN_PROGRESS"
    assert notifications == []

    wo = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = ?",
        (1,),
    )[0]
    assert wo["status"] == "IN_PROGRESS"

def test_verify_work_order_database_error_returns_500(
    test_env, monkeypatch
):
    client, db_path = test_env
    authenticate(client, 1)

    # Put the work order in a valid state first.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE work_orders SET status = ? WHERE wo_id = ?",
            ("PENDING_VERIFICATION", 1),
        )

    original_get_db = app_module.get_db

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            if "SELECT * FROM work_orders" in sql:
                raise RuntimeError("simulated verification database error")
            return self.connection.execute(sql, params)

        def rollback(self):
            self.connection.rollback()

    def failing_get_db():
        return FailingConnection(original_get_db())

    monkeypatch.setattr(app_module, "get_db", failing_get_db)

    response = client.post(
        "/api/work-orders/1/verify",
        json={"decision": "approve"},
    )

    assert response.status_code == 500
    assert response.get_json()["error"] == (
        "simulated verification database error"
    )

    # The failed operation must not change the work-order status.
    rows = read_db(
        db_path,
        "SELECT status FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["status"] == "PENDING_VERIFICATION"

def test_approve_work_order_without_record_id(test_env, monkeypatch):
    client, db_path = test_env
    authenticate(client, 1)

    # Prevent unrelated asset-status database queries.
    monkeypatch.setattr(
        app_module,
        "refresh_asset_status",
        lambda machine_id: None,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE work_orders
            SET status = ?, record_id = NULL
            WHERE wo_id = ?
            """,
            ("PENDING_VERIFICATION", 1),
        )

    response = client.post(
        "/api/work-orders/1/verify",
        json={
            "decision": "approve",
            "verification_note": "Verified successfully",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "CLOSED"

    rows = read_db(
        db_path,
        "SELECT status, verified_by FROM work_orders WHERE wo_id = ?",
        (1,),
    )
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["verified_by"] == "admin_test"

    records = read_db(
        db_path,
        """
        SELECT source_wo_id
        FROM maintenance_records
        WHERE source_wo_id = ?
        """,
        (1,),
    )
    assert len(records) == 1

    audit_rows = read_db(
        db_path,
        """
        SELECT action, target, details
        FROM audit_log
        WHERE action = ?
        """,
        ("VERIFY_WORK_ORDER",),
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["target"] == "WO #1"
    assert audit_rows[0]["details"] == "approve -> CLOSED"