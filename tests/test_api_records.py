import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class MockUser(UserMixin):
    id = "1"
    username = "test_tech"

    def __init__(self, is_admin=False):
        self.is_admin = is_admin


@pytest.fixture
def record_api_env(tmp_path, monkeypatch):
    """Provide isolated database data for record and history API tests."""
    db_path = tmp_path / "test_api_records.db"

    def get_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = get_test_db()
    conn.executescript("""
        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            machine_id TEXT,
            risk_score REAL,
            risk_level TEXT,
            pred_prob REAL,
            action TEXT,
            timestamp TEXT
        );

        CREATE TABLE shap_values (
            record_id INTEGER,
            top_feature TEXT,
            top_shap_value REAL,
            tool_wear REAL
        );

        CREATE TABLE assets (
            machine_id TEXT PRIMARY KEY,
            asset_name TEXT,
            location TEXT
        );

        CREATE TABLE work_orders (
            wo_id INTEGER PRIMARY KEY,
            record_id INTEGER,
            status TEXT,
            assigned_to TEXT,
            created_at TEXT
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            username TEXT,
            target TEXT,
            action TEXT,
            details TEXT
        );
    """)

    conn.execute("""
        INSERT INTO decision_log
            (record_id, machine_id, risk_score, risk_level,
             pred_prob, action, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        101, "MCH-001", 85.0, "Critical",
        0.95, "Inspect machine", "2026-01-01 10:00:00"
    ))

    conn.execute("""
        INSERT INTO shap_values
            (record_id, top_feature, top_shap_value, tool_wear)
        VALUES (?, ?, ?, ?)
    """, (101, "tool_wear", 0.75, 0.75))

    conn.execute("""
        INSERT INTO assets (machine_id, asset_name, location)
        VALUES (?, ?, ?)
    """, ("MCH-001", "Machine 1", "Production Line A"))

    conn.executemany("""
        INSERT INTO work_orders
            (wo_id, record_id, status, assigned_to, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, [
        (1, 101, "OPEN", "tech_a", "2026-01-01 09:00:00"),
        (2, 101, "IN_PROGRESS", "tech_b", "2026-01-02 09:00:00"),
        (3, 101, "ASSIGNED", "tech_c", "2026-01-02 09:00:00"),
        (4, 999, "OPEN", "tech_d", "2026-01-03 09:00:00"),
    ])

    conn.executemany("""
        INSERT INTO audit_log
            (id, timestamp, username, target, action, details)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (
            1, "2026-01-01 10:00:00", "admin",
            "Record #101", "CREATE", "Created record"
        ),
        (
            2, "2026-01-02 10:00:00", "tech_a",
            "Record #101", "UPDATE", "Updated status"
        ),
        (
            3, "2026-01-02 10:00:00", "tech_b",
            "Record #101", "ASSIGN", "Assigned technician"
        ),
        (
            4, "2026-01-03 10:00:00", "admin",
            "Record #999", "UPDATE", "Different record"
        ),
    ])

    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "get_db", get_test_db)
    monkeypatch.setattr(
        app_module,
        "machine_id_for_record",
        lambda record_id: "MCH-001" if record_id == 101 else None,
    )
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: MockUser() if user_id == "1" else None,
    )

    app_module.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, get_test_db


# GET /api/record/<record_id>


def test_api_record_returns_record_and_shap_data(record_api_env):
    client, _ = record_api_env

    response = client.get("/api/record/101")

    assert response.status_code == 200
    data = response.get_json()

    assert data["record_id"] == 101
    assert data["machine_id"] == "MCH-001"
    assert data["risk_score"] == 85.0
    assert data["risk_level"] == "Critical"
    assert len(data["shap"]) == 1
    assert data["shap"][0]["top_feature"] == "tool_wear"


def test_api_record_includes_asset_information(record_api_env):
    client, _ = record_api_env

    response = client.get("/api/record/101")

    assert response.status_code == 200
    asset = response.get_json()["asset"]

    assert asset is not None
    assert asset["machine_id"] == "MCH-001"
    assert asset["asset_name"] == "Machine 1"
    assert asset["location"] == "Production Line A"


def test_api_record_returns_linked_work_orders_in_descending_order(
    record_api_env,
):
    client, _ = record_api_env

    response = client.get("/api/record/101")

    assert response.status_code == 200
    work_orders = response.get_json()["linked_work_orders"]

    assert [item["wo_id"] for item in work_orders] == [3, 2, 1]
    assert all(item["status"] != "FALSE_POSITIVE" for item in work_orders)
    assert all(item["wo_id"] != 4 for item in work_orders)


def test_api_record_returns_empty_shap_list_when_no_shap_data(
    record_api_env,
):
    client, get_test_db = record_api_env

    conn = get_test_db()
    conn.execute("DELETE FROM shap_values WHERE record_id = 101")
    conn.commit()
    conn.close()

    response = client.get("/api/record/101")

    assert response.status_code == 200
    assert response.get_json()["shap"] == []


def test_api_record_returns_none_when_asset_is_missing(record_api_env):
    client, get_test_db = record_api_env

    conn = get_test_db()
    conn.execute("DELETE FROM assets WHERE machine_id = 'MCH-001'")
    conn.commit()
    conn.close()

    response = client.get("/api/record/101")

    assert response.status_code == 200
    assert response.get_json()["asset"] is None


def test_api_record_returns_empty_work_orders_when_none_are_linked(
    record_api_env,
):
    client, get_test_db = record_api_env

    conn = get_test_db()
    conn.execute("DELETE FROM work_orders WHERE record_id = 101")
    conn.commit()
    conn.close()

    response = client.get("/api/record/101")

    assert response.status_code == 200
    assert response.get_json()["linked_work_orders"] == []


def test_api_record_returns_404_for_unknown_record(record_api_env):
    client, _ = record_api_env

    response = client.get("/api/record/999")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not found"


def test_api_record_returns_500_when_database_fails(
    record_api_env, monkeypatch,
):
    client, _ = record_api_env

    def raise_db_error():
        raise sqlite3.OperationalError("test record database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/record/101")

    assert response.status_code == 500
    assert "test record database error" in response.get_json()["error"]


# GET /api/record/<record_id>/history


def test_api_record_history_returns_matching_audit_entries(record_api_env):
    client, _ = record_api_env

    response = client.get("/api/record/101/history")

    assert response.status_code == 200
    data = response.get_json()

    assert len(data) == 3
    assert all(
        {"id", "timestamp", "username", "action", "details"}.issubset(item)
        for item in data
    )
    assert {item["username"] for item in data} == {
        "admin", "tech_a", "tech_b"
    }


def test_api_record_history_sorts_by_timestamp_then_id_descending(
    record_api_env,
):
    client, _ = record_api_env

    response = client.get("/api/record/101/history")

    assert response.status_code == 200
    data = response.get_json()

    assert [item["id"] for item in data] == [3, 2, 1]


def test_api_record_history_returns_empty_list_without_history(
    record_api_env,
):
    client, _ = record_api_env

    response = client.get("/api/record/888/history")

    assert response.status_code == 200
    assert response.get_json() == []


def test_api_record_history_returns_500_when_database_fails(
    record_api_env, monkeypatch,
):
    client, _ = record_api_env

    def raise_db_error():
        raise sqlite3.OperationalError("test history database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/record/101/history")

    assert response.status_code == 500
    assert "test history database error" in response.get_json()["error"]