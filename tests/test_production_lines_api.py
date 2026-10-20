import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class TestUser(UserMixin):
    id = "1"


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """Create an isolated database for production-line API tests."""
    test_db = tmp_path / "test_production_lines_api.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    # Bypass the real database-backed user loader in these isolated API tests.
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: TestUser() if user_id == "1" else None,
    )

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript("""
            CREATE TABLE production_lines (
                line_id TEXT PRIMARY KEY,
                name TEXT
            );

            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                machine_id TEXT,
                line_id TEXT
            );

            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                timestamp TEXT,
                risk_score REAL,
                risk_level TEXT,
                alert_status TEXT
            );

            CREATE TABLE downtime_events (
                event_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                start_time TEXT,
                end_time TEXT
            );
        """)

        conn.execute(
            "INSERT INTO production_lines (line_id, name) VALUES (?, ?)",
            ("LINE-01", "Assembly Line 1"),
        )
        conn.execute(
            "INSERT INTO assets (asset_id, machine_id, line_id) VALUES (?, ?, ?)",
            ("AST-1001", "MCH-001", "LINE-01"),
        )
        conn.execute(
            "INSERT INTO assets (asset_id, machine_id, line_id) VALUES (?, ?, ?)",
            ("AST-1002", "MCH-002", "LINE-01"),
        )
        conn.commit()
    finally:
        conn.close()

    def connect_test_db():
        connection = sqlite3.connect(test_db)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(app_module, "get_db", connect_test_db)

    def fake_latest_decisions(conn):
        return {}

    def fake_machine_snapshot(conn, asset, latest_decisions):
        machine_id = asset["machine_id"]

        snapshots = {
            "MCH-001": {
                "machine_id": "MCH-001",
                "status": "RUNNING",
                "risk_level": "LOW",
                "maintenance_status": "ON_SCHEDULE",
            },
            "MCH-002": {
                "machine_id": "MCH-002",
                "status": "WARNING",
                "risk_level": "HIGH",
                "maintenance_status": "OVERDUE",
            },
        }

        return snapshots[machine_id]

    monkeypatch.setattr(
        app_module,
        "_latest_decisions_by_machine",
        fake_latest_decisions,
    )
    monkeypatch.setattr(
        app_module,
        "_machine_snapshot",
        fake_machine_snapshot,
    )
    monkeypatch.setattr(
        app_module,
        "_kpis_for_machines",
        lambda conn, machine_ids: {
            "machine_count": len(machine_ids)
        },
    )
    monkeypatch.setattr(
        app_module,
        "_failure_trend",
        lambda conn, machine_ids: [],
    )

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, test_db

    monkeypatch.setattr(app_module, "_db_configured", False)


def test_api_production_lines_returns_line_data(api_env):
    client, _ = api_env

    response = client.get("/api/production-lines")

    assert response.status_code == 200

    data = response.get_json()

    assert len(data) == 1
    assert data[0]["line_id"] == "LINE-01"
    assert data[0]["status"] == "WARNING"
    assert data[0]["asset_count"] == 2
    assert data[0]["status_counts"] == {
        "RUNNING": 1,
        "WARNING": 1,
    }
    assert data[0]["risk_counts"] == {
        "LOW": 1,
        "HIGH": 1,
    }
    assert data[0]["kpis"]["machine_count"] == 2


def test_api_production_lines_returns_empty_list(api_env):
    client, test_db = api_env

    conn = sqlite3.connect(test_db)
    try:
        conn.execute("DELETE FROM assets")
        conn.execute("DELETE FROM production_lines")
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/production-lines")

    assert response.status_code == 200
    assert response.get_json() == []


def test_api_production_lines_returns_500_on_database_error(
    api_env, monkeypatch
):
    client, _ = api_env

    def raise_database_error():
        raise RuntimeError("Test database error")

    monkeypatch.setattr(
        app_module,
        "get_db",
        raise_database_error,
    )

    response = client.get("/api/production-lines")

    assert response.status_code == 500
    assert response.get_json()["error"] == "Test database error"


def test_api_production_line_detail_returns_line_and_machines(api_env):
    client, _ = api_env

    response = client.get("/api/production-lines/LINE-01")

    assert response.status_code == 200

    data = response.get_json()

    assert data["line_id"] == "LINE-01"
    assert data["status"] == "WARNING"
    assert len(data["assets"]) == 2
    assert data["machines"] == data["assets"]
    assert data["status_counts"] == {
        "RUNNING": 1,
        "WARNING": 1,
    }
    assert data["risk_counts"] == {
        "LOW": 1,
        "HIGH": 1,
    }
    assert data["maintenance_counts"] == {
        "ON_SCHEDULE": 1,
        "OVERDUE": 1,
    }
    assert data["kpis"]["machine_count"] == 2
    assert data["failure_trend"] == []
    assert data["data_source"].startswith("Persisted SQLite")
    assert data["last_updated"] is None


def test_api_production_line_detail_returns_404_for_unknown_line(api_env):
    client, _ = api_env

    response = client.get("/api/production-lines/LINE-UNKNOWN")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": "Production line not found"
    }


def test_api_production_line_detail_handles_line_without_assets(
    api_env, monkeypatch
):
    client, test_db = api_env

    conn = sqlite3.connect(test_db)
    try:
        conn.execute(
            "INSERT INTO production_lines (line_id, name) VALUES (?, ?)",
            ("LINE-EMPTY", "Empty Line"),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/production-lines/LINE-EMPTY")

    assert response.status_code == 200

    data = response.get_json()

    assert data["line_id"] == "LINE-EMPTY"
    assert data["status"] == "RUNNING"
    assert data["assets"] == []
    assert data["machines"] == []
    assert data["status_counts"] == {}
    assert data["risk_counts"] == {}
    assert data["maintenance_counts"] == {}
    assert data["kpis"]["machine_count"] == 0


def test_api_production_line_detail_returns_500_on_database_error(
    api_env, monkeypatch
):
    client, _ = api_env

    def raise_database_error():
        raise RuntimeError("Test database error")

    monkeypatch.setattr(
        app_module,
        "get_db",
        raise_database_error,
    )

    response = client.get("/api/production-lines/LINE-01")

    assert response.status_code == 500
    assert response.get_json()["error"] == "Test database error"
