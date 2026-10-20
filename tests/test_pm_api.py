
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def pm_test_env(tmp_path, monkeypatch):
    """Use an isolated temporary database for PM API tests."""
    test_db = tmp_path / "test_pm_api.db"

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

        CREATE TABLE production_lines (
            line_id TEXT PRIMARY KEY,
            line_name TEXT
        );

        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY,
            machine_id TEXT UNIQUE NOT NULL,
            machine_name TEXT NOT NULL,
            machine_type TEXT,
            line_id TEXT NOT NULL,
            location TEXT,
            manufacturer TEXT,
            model TEXT,
            criticality TEXT NOT NULL DEFAULT 'MEDIUM',
            status TEXT NOT NULL DEFAULT 'RUNNING'
        );

        CREATE TABLE pm_schedules (
            pm_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            pm_name TEXT NOT NULL,
            frequency_type TEXT NOT NULL,
            frequency_value INTEGER NOT NULL,
            last_performed_at TEXT,
            last_performed_tool_wear INTEGER,
            next_due_at TEXT,
            status TEXT NOT NULL DEFAULT 'ON_SCHEDULE',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

                CREATE TABLE maintenance_records (
            maint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            maintenance_type TEXT NOT NULL,
            source_wo_id INTEGER,
            source_pm_id INTEGER,
            description TEXT NOT NULL,
            performed_by TEXT NOT NULL,
            performed_at TEXT NOT NULL,
            downtime_minutes INTEGER,
            created_at TEXT NOT NULL
        );


        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        ) VALUES
            (1, 'admin_test', 'admin@test.local', 'unused',
             'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused',
             'technician', 1, 0, '2026-01-01');

        INSERT INTO production_lines (line_id, line_name)
        VALUES ('LINE-01', 'Test Line');

        INSERT INTO assets (
            asset_id, machine_id, machine_name, line_id
        ) VALUES (
            'AST-1001', 'MCH-001', 'Test Machine', 'LINE-01'
        );
    """)



    conn.commit()
    conn.close()

    monkeypatch.setattr(
        app_module, "refresh_asset_status", lambda machine_id: None
    )

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, test_db


def authenticate(client, user_id=1):
    """Authenticate using Flask-Login's test session."""
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


def test_create_pm_schedule_requires_asset_and_name(pm_test_env):
    client, _ = pm_test_env
    authenticate(client)

    response = client.post("/api/pm-schedules", json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "asset_id and pm_name are required"
    )


def test_create_pm_schedule_rejects_invalid_frequency_type(pm_test_env):
    client, _ = pm_test_env
    authenticate(client)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "AST-1001",
            "pm_name": "Routine Inspection",
            "frequency_type": "WEEKS",
            "frequency_value": 7,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "frequency_type must be 'DAYS' or 'TOOL_WEAR'"
    )


@pytest.mark.parametrize("frequency_value", [None, 0, -1, "invalid"])
def test_create_pm_schedule_rejects_invalid_frequency_value(
    pm_test_env, frequency_value
):
    client, _ = pm_test_env
    authenticate(client)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "AST-1001",
            "pm_name": "Routine Inspection",
            "frequency_type": "DAYS",
            "frequency_value": frequency_value,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "frequency_value must be a positive integer"
    )


def test_create_pm_schedule_rejects_unknown_asset(pm_test_env):
    client, _ = pm_test_env
    authenticate(client)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "AST-9999",
            "pm_name": "Routine Inspection",
            "frequency_type": "DAYS",
            "frequency_value": 30,
        },
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_admin_can_create_days_pm_schedule(pm_test_env):
    client, db_path = pm_test_env
    authenticate(client)

    response = client.post(
        "/api/pm-schedules",
        json={
            "asset_id": "AST-1001",
            "pm_name": "Monthly Inspection",
            "frequency_type": "DAYS",
            "frequency_value": 30,
        },
    )

    assert response.status_code == 201

    result = response.get_json()
    assert result["message"] == "PM schedule created"
    assert isinstance(result["pm_id"], int)

    rows = read_db(
        db_path,
        "SELECT * FROM pm_schedules WHERE pm_id = ?",
        (result["pm_id"],),
    )

    assert len(rows) == 1
    assert rows[0]["asset_id"] == "AST-1001"
    assert rows[0]["machine_id"] == "MCH-001"
    assert rows[0]["pm_name"] == "Monthly Inspection"
    assert rows[0]["frequency_type"] == "DAYS"
    assert rows[0]["frequency_value"] == 30
    assert rows[0]["status"] == "ON_SCHEDULE"
    assert rows[0]["created_by"] == "admin_test"
    assert rows[0]["next_due_at"] is not None


def test_complete_pm_schedule_not_found(pm_test_env):
    client, _ = pm_test_env
    authenticate(client)

    response = client.post("/api/pm-schedules/999/complete", json={})

    assert response.status_code == 404
    assert response.get_json()["error"] == "PM schedule not found"


def test_complete_pm_schedule_creates_maintenance_record(pm_test_env):
    client, db_path = pm_test_env
    authenticate(client)

    # Create a PM schedule directly for this test.
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO pm_schedules (
            asset_id, machine_id, pm_name, frequency_type,
            frequency_value, next_due_at, status, created_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "AST-1001",
            "MCH-001",
            "Monthly Inspection",
            "DAYS",
            30,
            "2026-01-01T00:00:00",
            "OVERDUE",
            "admin_test",
            "2026-01-01T00:00:00",
        ),
    )
    pm_id = cursor.lastrowid
    conn.commit()
    conn.close()

    response = client.post(
        f"/api/pm-schedules/{pm_id}/complete",
        json={"note": "Inspection completed"},
    )

    assert response.status_code == 200
    assert response.get_json()["message"] == "PM marked complete"

    schedules = read_db(
        db_path,
        "SELECT * FROM pm_schedules WHERE pm_id = ?",
        (pm_id,),
    )
    assert schedules[0]["last_performed_at"] is not None
    assert schedules[0]["next_due_at"] is not None
    assert schedules[0]["status"] == "ON_SCHEDULE"

    records = read_db(
        db_path,
        "SELECT * FROM maintenance_records WHERE source_pm_id = ?",
        (pm_id,),
    )
    assert len(records) == 1
    assert records[0]["maintenance_type"] == "PREVENTIVE"
    assert records[0]["description"] == "Inspection completed"
    assert records[0]["performed_by"] == "admin_test"



def test_delete_pm_schedule_not_found(pm_test_env):
    client, _ = pm_test_env
    authenticate(client)

    response = client.delete("/api/pm-schedules/999")

    assert response.status_code == 404
    assert response.get_json()["error"] == "PM schedule not found"


def test_admin_can_delete_pm_schedule(pm_test_env):
    client, db_path = pm_test_env
    authenticate(client)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO pm_schedules (
            asset_id, machine_id, pm_name, frequency_type,
            frequency_value, status, created_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "AST-1001",
            "MCH-001",
            "Monthly Inspection",
            "DAYS",
            30,
            "ON_SCHEDULE",
            "admin_test",
            "2026-01-01T00:00:00",
        ),
    )
    pm_id = cursor.lastrowid
    conn.commit()
    conn.close()

    response = client.delete(f"/api/pm-schedules/{pm_id}")

    assert response.status_code == 200
    assert response.get_json()["message"] == "PM schedule deleted"

    remaining = read_db(
        db_path,
        "SELECT * FROM pm_schedules WHERE pm_id = ?",
        (pm_id,),
    )
    assert remaining == []


def test_technician_cannot_delete_pm_schedule(pm_test_env):
    client, _ = pm_test_env
    authenticate(client, user_id=2)

    response = client.delete("/api/pm-schedules/1")

    assert response.status_code == 403
