
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def factory_env(tmp_path, monkeypatch):
    """Use an isolated SQLite database for factory overview API tests."""
    test_db = tmp_path / "test_factory_overview.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)
    app_module.app.config["TESTING"] = True

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE production_lines (
            line_id TEXT PRIMARY KEY,
            line_name TEXT NOT NULL,
            location TEXT,
            status TEXT
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

        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            timestamp TEXT
        );

        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        ) VALUES (
            1, 'admin_test', 'admin@test.local', 'unused',
            'admin', 1, 0, '2026-01-01'
        );

        INSERT INTO production_lines (
            line_id, line_name, location, status
        ) VALUES (
            'LINE-01', 'Production Line 1', 'Factory', 'RUNNING'
        );

        INSERT INTO assets (
            asset_id, machine_id, machine_name, machine_type,
            line_id, criticality, status
        ) VALUES (
            'AST-1002', 'MCH-002', 'Test Machine',
            'CNC', 'LINE-01', 'HIGH', 'RUNNING'
        );
    """)
    conn.commit()
    conn.close()

    # Isolate API aggregation behavior from helper SQL dependencies.
    monkeypatch.setattr(
        app_module,
        "_latest_decisions_by_machine",
        lambda conn: {},
    )
    monkeypatch.setattr(
        app_module,
        "_machine_snapshot",
        lambda conn, asset, decisions: {
            **dict(asset),
            "status": "RUNNING",
            "risk_level": None,
            "risk_score": None,
            "pred_prob": None,
            "maintenance_status": "ON_SCHEDULE",
            "open_work_orders": 0,
        },
    )
    monkeypatch.setattr(
        app_module,
        "_kpis_for_machines",
        lambda conn, machine_ids: {
            "window_days": 30,
            "availability": 100.0 if machine_ids else None,
            "mtbf_hours": None,
            "mttr_minutes": None,
            "failure_count": 0,
            "downtime_minutes": 0,
        },
    )
    monkeypatch.setattr(
        app_module,
        "_failure_trend",
        lambda conn, machine_ids: [],
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, test_db


def test_factory_overview_returns_factory_line_and_machine(factory_env):
    client, _ = factory_env

    response = client.get("/api/factory-overview")

    assert response.status_code == 200
    payload = response.get_json()

    assert payload["factory"]["name"] == "Factory Operations Centre"
    assert payload["factory"]["machine_count"] == 1
    assert payload["factory"]["line_count"] == 1
    assert payload["factory"]["status_summary"] == {"RUNNING": 1}
    assert payload["factory"]["maintenance_summary"] == {
        "ON_SCHEDULE": 1
    }

    assert len(payload["lines"]) == 1
    line = payload["lines"][0]
    assert line["line_id"] == "LINE-01"
    assert line["status"] == "RUNNING"
    assert line["asset_count"] == 1
    assert len(line["machines"]) == 1
    assert line["machines"][0]["machine_id"] == "MCH-002"


def test_factory_overview_returns_empty_summaries_when_no_assets(
    factory_env,
):
    client, test_db = factory_env

    conn = sqlite3.connect(test_db)
    conn.execute("DELETE FROM assets")
    conn.commit()
    conn.close()

    response = client.get("/api/factory-overview")

    assert response.status_code == 200
    payload = response.get_json()

    assert payload["factory"]["machine_count"] == 0
    assert payload["factory"]["line_count"] == 1
    assert payload["factory"]["status_summary"] == {}
    assert payload["factory"]["risk_summary"] == {}
    assert payload["factory"]["maintenance_summary"] == {}
    assert payload["lines"][0]["asset_count"] == 0
    assert payload["lines"][0]["machines"] == []


def test_factory_overview_requires_login():
    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        response = client.get("/api/factory-overview")

    assert response.status_code in (302, 401)




def test_factory_overview_returns_500_on_database_error(
    factory_env, monkeypatch
):
    client, _ = factory_env

    def raise_database_error(conn):
        raise sqlite3.OperationalError("simulated database error")

    monkeypatch.setattr(
        app_module,
        "_latest_decisions_by_machine",
        raise_database_error,
    )

    response = client.get("/api/factory-overview")

    assert response.status_code == 500
    assert "simulated database error" in response.get_json()["error"]
