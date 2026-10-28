
import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class TestUser(UserMixin):
    id = "1"
    username = "test_user"


@pytest.fixture
def maintenance_env(tmp_path, monkeypatch):
    """Set up an isolated database and authenticated test client."""
    test_db = tmp_path / "test_maintenance_records.db"

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE assets (
            asset_id TEXT PRIMARY KEY,
            machine_id TEXT NOT NULL
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

        INSERT INTO assets (asset_id, machine_id)
        VALUES ('AST-1001', 'MACHINE-001');
    """)
    conn.commit()
    conn.close()

    # Redirect database access to the isolated test database.
    monkeypatch.setattr(app_module, "DB", str(test_db))

    connections = []

    def connect_test_db():
        db = sqlite3.connect(test_db)
        db.row_factory = sqlite3.Row
        connections.append(db)
        return db

    monkeypatch.setattr(app_module, "get_db", connect_test_db)

    # Avoid querying a real user table during login.
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: TestUser() if user_id == "1" else None,
    )

    # Prevent audit logging from affecting these endpoint tests.
    monkeypatch.setattr(app_module, "log_action", lambda *args, **kwargs: None)

    monkeypatch.setitem(app_module.app.config, "TESTING", True)

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, test_db

    for db in connections:
        db.close()


def test_create_maintenance_record_success(maintenance_env):
    """A valid request creates a maintenance record."""
    client, test_db = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={
            "asset_id": "AST-1001",
            "description": "Replaced worn belt",
            "maintenance_type": "CORRECTIVE",
            "downtime_minutes": 30,
        },
    )

    assert response.status_code == 201

    body = response.get_json()
    assert body["message"] == "Maintenance record created"
    assert body["maint_id"] > 0

    with sqlite3.connect(test_db) as conn:
        row = conn.execute(
            """
            SELECT asset_id, machine_id, maintenance_type,
                   description, performed_by, downtime_minutes
            FROM maintenance_records
            WHERE maint_id = ?
            """,
            (body["maint_id"],),
        ).fetchone()

    assert row is not None
    assert row[0] == "AST-1001"
    assert row[1] == "MACHINE-001"
    assert row[2] == "CORRECTIVE"
    assert row[3] == "Replaced worn belt"
    assert row[4] == "test_user"
    assert row[5] == 30


def test_create_maintenance_record_missing_asset_id(maintenance_env):
    """Missing asset_id returns 400."""
    client, _ = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={"description": "Inspect machine"},
    )

    assert response.status_code == 400


def test_create_maintenance_record_missing_description(maintenance_env):
    """Missing description returns 400."""
    client, _ = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={"asset_id": "AST-1001"},
    )

    assert response.status_code == 400


def test_create_maintenance_record_invalid_type(maintenance_env):
    """An unsupported maintenance type returns 400."""
    client, _ = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={
            "asset_id": "AST-1001",
            "description": "Inspect machine",
            "maintenance_type": "INVALID",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid maintenance_type"


def test_create_maintenance_record_asset_not_found(maintenance_env):
    """An unknown asset returns 404."""
    client, _ = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={
            "asset_id": "AST-9999",
            "description": "Inspect machine",
        },
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_create_maintenance_record_defaults_to_corrective(maintenance_env):
    """Omitting maintenance_type defaults to CORRECTIVE."""
    client, test_db = maintenance_env

    response = client.post(
        "/api/maintenance-records",
        json={
            "asset_id": "AST-1001",
            "description": "Repair machine",
        },
    )

    assert response.status_code == 201

    with sqlite3.connect(test_db) as conn:
        row = conn.execute(
            "SELECT maintenance_type FROM maintenance_records WHERE maint_id = ?",
            (response.get_json()["maint_id"],),
        ).fetchone()

    assert row[0] == "CORRECTIVE"


def test_create_maintenance_record_requires_login():
    """An unauthenticated request is rejected."""
    with app_module.app.test_client() as client:
        response = client.post(
            "/api/maintenance-records",
            json={
                "asset_id": "AST-1001",
                "description": "Inspect machine",
            },
            follow_redirects=False,
        )

    assert response.status_code in (302, 401)
