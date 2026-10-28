
import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class MockUser(UserMixin):
    id = "1"
    username = "test_tech"
    is_admin = False


@pytest.fixture
def export_csv_env(tmp_path, monkeypatch):
    """Create an isolated database for CSV export tests."""
    db_path = tmp_path / "test_export_csv.db"

    def get_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = get_test_db()
    conn.execute("""
        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
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
            reasons TEXT,
            shap_explanation TEXT,
            alert_status TEXT,
            timestamp TEXT
        )
    """)

    conn.executemany("""
        INSERT INTO decision_log (
            record_id, air_temp, process_temp, rpm, torque,
            tool_wear, power, pred_prob, risk_score, risk_level,
            action, reasons, shap_explanation, alert_status, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            1, 300, 310, 1500, 40, 100, 6000, 0.95, 95,
            "Critical", "Inspect machine", "High risk",
            "tool_wear contribution", "OPEN", "2026-01-01 10:00:00"
        ),
        (
            2, 295, 305, 1400, 35, 80, 4900, 0.80, 80,
            "High", "Schedule maintenance", "Elevated risk",
            "torque contribution", "OPEN", "2026-01-02 10:00:00"
        ),
        (
            3, 290, 300, 1300, 30, 50, 3900, 0.10, 10,
            "Low", "Continue monitoring", "Low risk",
            "normal operation", "CLOSED", "2026-01-03 10:00:00"
        ),
    ])

    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "get_db", get_test_db)
    monkeypatch.setattr(
        app_module,
        "log_action",
        lambda *args, **kwargs: None,
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

        yield client


def test_export_csv_returns_download_response(export_csv_env):
    client = export_csv_env

    response = client.get("/api/export/csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert (
        response.headers["Content-Disposition"]
        == "attachment; filename=high_risk_records.csv"
    )


def test_export_csv_contains_only_high_and_critical_records(
    export_csv_env,
):
    client = export_csv_env

    response = client.get("/api/export/csv")
    content = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "record_id" in content
    assert "Critical" in content
    assert "High" in content
    assert "Low" not in content


def test_export_csv_orders_records_by_risk_score_descending(
    export_csv_env,
):
    client = export_csv_env

    response = client.get("/api/export/csv")
    content = response.get_data(as_text=True)

    critical_position = content.index("Critical")
    high_position = content.index("High")

    assert critical_position < high_position


def test_export_csv_returns_500_when_database_fails(
    export_csv_env, monkeypatch,
):
    client = export_csv_env

    def raise_db_error():
        raise sqlite3.OperationalError("test export database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/export/csv")

    assert response.status_code == 500
    assert "test export database error" in response.get_json()["error"]


def test_export_csv_requires_authentication(export_csv_env):
    client = export_csv_env

    with client.session_transaction() as session:
        session.clear()

    response = client.get("/api/export/csv", follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
