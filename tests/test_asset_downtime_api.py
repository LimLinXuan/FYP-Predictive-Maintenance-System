import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class TestUser(UserMixin):
    id = "1"
    username = "test_user"


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """Isolated SQLite database and mocked external side effects."""
    test_db = tmp_path / "test_asset_downtime_api.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

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
                machine_id TEXT NOT NULL,
                line_id TEXT,
                name TEXT,
                status TEXT
            );

            CREATE TABLE downtime_events (
                downtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT,
                machine_id TEXT,
                start_time TEXT,
                end_time TEXT,
                reason_category TEXT,
                reason_detail TEXT,
                linked_wo_id INTEGER,
                reported_by TEXT,
                created_at TEXT,
                duration_minutes INTEGER
            );

            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                timestamp TEXT,
                air_temp REAL,
                process_temp REAL,
                rpm REAL,
                torque REAL,
                tool_wear REAL,
                power REAL,
                pred_prob REAL,
                risk_score REAL,
                risk_level TEXT,
                action TEXT,
                alert_status TEXT,
                assigned_to TEXT,
                actual_failure INTEGER,
                hdf_risk REAL,
                pwf_risk REAL,
                osf_risk REAL,
                reasons TEXT
            );

            CREATE TABLE work_orders (
                wo_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                title TEXT,
                priority TEXT,
                status TEXT,
                assigned_to TEXT,
                due_date TEXT,
                created_at TEXT,
                record_id INTEGER
            );
        """)

        conn.execute(
            "INSERT INTO production_lines (line_id, name) VALUES (?, ?)",
            ("LINE-01", "Assembly Line 1"),
        )
        conn.executemany(
            """
            INSERT INTO assets
                (asset_id, machine_id, line_id, name, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("AST-1001", "MCH-001", "LINE-01", "Machine 1", "RUNNING"),
                ("AST-1002", "MCH-002", "LINE-01", "Machine 2", "RUNNING"),
                ("AST-BAD", "MACHINE-X", "LINE-01", "Malformed ID", "RUNNING"),
            ],
        )

        conn.executemany(
            """
            INSERT INTO decision_log (
                record_id, timestamp, air_temp, process_temp, rpm,
                torque, tool_wear, power, pred_prob, risk_score,
                risk_level, action, alert_status, assigned_to,
                actual_failure, hdf_risk, pwf_risk, osf_risk, reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    20, "2026-10-19T10:00:00", 300.0, 310.0, 1500.0,
                    40.0, 100.0, 600.0, 0.8, 90.0, "HIGH", "INSPECT",
                    "OPEN", "test_user", 1, 0.1, 0.2, 0.3, "Test failure",
                ),
                (
                    40, "2026-10-19T11:00:00", 301.0, 311.0, 1550.0,
                    42.0, 110.0, 650.0, 0.7, 70.0, "HIGH", "MONITOR",
                    "OPEN", "test_user", 1, 0.2, 0.3, 0.4, "Second failure",
                ),
                (
                    21, "2026-10-19T12:00:00", 302.0, 312.0, 1600.0,
                    43.0, 120.0, 660.0, 0.2, 20.0, "LOW", "NONE",
                    "OPEN", None, 0, 0.0, 0.0, 0.0, "Normal record",
                ),
            ],
        )

        conn.execute(
            """
            INSERT INTO work_orders (
                wo_id, machine_id, title, priority, status,
                assigned_to, due_date, created_at, record_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1, "MCH-001", "Inspect machine", "HIGH", "OPEN",
                "test_user", "2026-10-25", "2026-10-19T09:00:00", 20,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    def connect_test_db():
        db = sqlite3.connect(test_db)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(app_module, "get_db", connect_test_db)

    # Avoid querying the real user table in login_required.
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: TestUser() if user_id == "1" else None,
    )

    monkeypatch.setattr(
        app_module,
        "_latest_decisions_by_machine",
        lambda conn: {},
    )

    def fake_snapshot(conn, asset, latest_decisions):
        return {
            "asset_id": asset["asset_id"],
            "machine_id": asset["machine_id"],
            "line_id": asset["line_id"],
            "status": asset["status"],
            "risk_level": "HIGH" if asset["machine_id"] == "MCH-001" else "LOW",
        }

    monkeypatch.setattr(app_module, "_machine_snapshot", fake_snapshot)
    monkeypatch.setattr(
        app_module, "refresh_asset_status", lambda machine_id: "RUNNING"
    )
    monkeypatch.setattr(app_module, "log_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_module, "create_notification", lambda *args, **kwargs: None
    )

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, test_db

    monkeypatch.setattr(app_module, "_db_configured", False)


def test_start_downtime_success(api_env):
    client, test_db = api_env

    response = client.post(
        "/api/downtime/start",
        json={
            "asset_id": "AST-1001",
            "reason_category": "UNPLANNED_FAILURE",
            "reason_detail": "Unexpected stop",
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["message"] == "Downtime started"
    assert data["downtime_id"] == 1

    conn = sqlite3.connect(test_db)
    try:
        row = conn.execute(
            "SELECT asset_id, machine_id, reason_category, reported_by "
            "FROM downtime_events WHERE downtime_id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "AST-1001", "MCH-001", "UNPLANNED_FAILURE", "test_user"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"asset_id": "AST-1001"},
        {"asset_id": "AST-1001", "reason_category": "INVALID"},
    ],
)
def test_start_downtime_rejects_invalid_payload(api_env, payload):
    client, _ = api_env

    response = client.post("/api/downtime/start", json=payload)

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_start_downtime_asset_not_found(api_env):
    client, _ = api_env

    response = client.post(
        "/api/downtime/start",
        json={
            "asset_id": "UNKNOWN",
            "reason_category": "OTHER",
        },
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_start_downtime_rejects_already_open_event(api_env):
    client, test_db = api_env

    conn = sqlite3.connect(test_db)
    try:
        conn.execute(
            """
            INSERT INTO downtime_events
                (asset_id, machine_id, start_time, reason_category)
            VALUES (?, ?, ?, ?)
            """,
            ("AST-1001", "MCH-001", "2026-10-19T08:00:00", "OTHER"),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        "/api/downtime/start",
        json={
            "asset_id": "AST-1001",
            "reason_category": "OTHER",
        },
    )

    assert response.status_code == 400
    assert "already has an open downtime event" in response.get_json()["error"]


def test_end_downtime_success(api_env):
    client, test_db = api_env

    conn = sqlite3.connect(test_db)
    try:
        conn.execute(
            """
            INSERT INTO downtime_events
                (asset_id, machine_id, start_time, reason_category)
            VALUES (?, ?, ?, ?)
            """,
            ("AST-1001", "MCH-001", "2026-10-19T08:00:00", "OTHER"),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post("/api/downtime/1/end")

    assert response.status_code == 200
    data = response.get_json()
    assert data["message"] == "Downtime ended"
    assert isinstance(data["duration_minutes"], int)
    assert data["duration_minutes"] >= 0
    assert data["asset_status"] == "RUNNING"

    conn = sqlite3.connect(test_db)
    try:
        end_time, duration = conn.execute(
            "SELECT end_time, duration_minutes FROM downtime_events "
            "WHERE downtime_id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert end_time is not None
    assert duration == data["duration_minutes"]


def test_end_downtime_not_found(api_env):
    client, _ = api_env

    response = client.post("/api/downtime/999/end")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Downtime event not found"


def test_end_downtime_already_closed(api_env):
    client, test_db = api_env

    conn = sqlite3.connect(test_db)
    try:
        conn.execute(
            """
            INSERT INTO downtime_events
                (asset_id, machine_id, start_time, end_time, reason_category)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "AST-1001", "MCH-001", "2026-10-19T08:00:00",
                "2026-10-19T09:00:00", "OTHER",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post("/api/downtime/1/end")

    assert response.status_code == 400
    assert "already closed" in response.get_json()["error"]


def test_asset_failure_history_returns_failures(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001/failure-history")

    assert response.status_code == 200
    data = response.get_json()
    assert data["asset_id"] == "AST-1001"
    assert data["machine_id"] == "MCH-001"
    assert data["count"] == 2
    assert all("record_id" in row for row in data["failures"])
    assert all("risk_score" in row for row in data["failures"])
    assert {row["record_id"] for row in data["failures"]} == {20, 40}


def test_asset_failure_history_asset_not_found(api_env):
    client, _ = api_env

    response = client.get("/api/assets/UNKNOWN/failure-history")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_asset_failure_history_malformed_machine_id(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-BAD/failure-history")

    assert response.status_code == 500
    assert "Malformed machine_id" in response.get_json()["error"]


def test_api_assets_returns_all_assets(api_env):
    client, _ = api_env

    response = client.get("/api/assets")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 3
    assert {item["asset_id"] for item in data} == {
        "AST-1001", "AST-1002", "AST-BAD"
    }


def test_api_assets_filters_by_line(api_env):
    client, _ = api_env

    response = client.get("/api/assets?line_id=LINE-01")

    assert response.status_code == 200
    assert len(response.get_json()) == 3


def test_api_asset_detail_returns_asset_and_line(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001")

    assert response.status_code == 200
    data = response.get_json()
    assert data["asset_id"] == "AST-1001"
    assert data["machine_id"] == "MCH-001"
    assert data["line"]["line_id"] == "LINE-01"


def test_api_asset_detail_not_found(api_env):
    client, _ = api_env

    response = client.get("/api/assets/UNKNOWN")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_api_asset_observations_returns_records(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001/observations")

    assert response.status_code == 200
    data = response.get_json()
    assert data["asset_id"] == "AST-1001"
    assert data["machine_id"] == "MCH-001"
    assert data["count"] == 2
    assert len(data["observations"]) == 2


def test_api_asset_observations_respects_limit(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001/observations?limit=1")

    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 1
    assert len(data["observations"]) == 1


def test_api_asset_observations_clamps_limit_to_minimum(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001/observations?limit=0")

    assert response.status_code == 200
    assert response.get_json()["count"] == 1


def test_api_asset_observations_asset_not_found(api_env):
    client, _ = api_env

    response = client.get("/api/assets/UNKNOWN/observations")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"


def test_api_asset_observations_malformed_machine_id(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-BAD/observations")

    assert response.status_code == 500
    assert "Malformed machine_id" in response.get_json()["error"]


def test_api_asset_work_orders_returns_records(api_env):
    client, _ = api_env

    response = client.get("/api/assets/AST-1001/work-orders")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 1
    assert data[0]["wo_id"] == 1
    assert data[0]["title"] == "Inspect machine"
    assert data[0]["status"] == "OPEN"
    assert data[0]["priority"] == "HIGH"


def test_api_asset_work_orders_asset_not_found(api_env):
    client, _ = api_env

    response = client.get("/api/assets/UNKNOWN/work-orders")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Asset not found"
