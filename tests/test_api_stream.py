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
def stream_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database; never touch the real project DB."""
    db_path = tmp_path / "test_api_stream.db"

    def get_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = get_test_db()
    conn.execute("""
        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            risk_score REAL,
            risk_level TEXT,
            action TEXT,
            timestamp TEXT
        )
    """)
    conn.executemany("""
        INSERT INTO decision_log
            (record_id, risk_score, risk_level, action, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, [
        (101, 20.0, "Low", "Monitor", "2026-01-01 10:00:00"),
        (102, 75.0, "High", "Inspect", "2026-01-01 11:00:00"),
        (103, 90.0, "Critical", "Stop machine", "2026-01-01 12:00:00"),
    ])
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "get_db", get_test_db)
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: MockUser() if user_id == "1" else None,
    )

    # Keep notification behavior isolated from the real notification system.
    notification_calls = []
    existing_notifications = set()

    def notification_exists(record_id, notif_type):
        return (record_id, notif_type) in existing_notifications

    def create_test_notification(**kwargs):
        notification_calls.append(kwargs)
        existing_notifications.add(
            (kwargs["record_id"], kwargs["notif_type"])
        )

    monkeypatch.setattr(
        app_module,
        "_notification_exists_for_record",
        notification_exists,
    )
    monkeypatch.setattr(
        app_module,
        "create_notification",
        create_test_notification,
    )

    app_module.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, get_test_db, notification_calls, existing_notifications


def test_api_stream_returns_record_and_stream_metadata(stream_env):
    client, _, _, _ = stream_env

    response = client.get("/api/stream?cursor=0")

    assert response.status_code == 200
    data = response.get_json()
    assert data["record_id"] == 101
    assert data["stream_index"] == 0
    assert data["total_records"] == 3
    assert data["next_stream_index"] == 1


def test_api_stream_returns_record_at_requested_cursor(stream_env):
    client, _, _, _ = stream_env

    response = client.get("/api/stream?cursor=1")

    assert response.status_code == 200
    data = response.get_json()
    assert data["record_id"] == 102
    assert data["stream_index"] == 1
    assert data["next_stream_index"] == 2


def test_api_stream_wraps_cursor_when_it_exceeds_record_count(stream_env):
    client, _, _, _ = stream_env

    response = client.get("/api/stream?cursor=4")

    assert response.status_code == 200
    data = response.get_json()
    assert data["record_id"] == 102
    assert data["stream_index"] == 1
    assert data["next_stream_index"] == 2


@pytest.mark.parametrize("cursor", ["invalid", "1.5"])
def test_api_stream_defaults_invalid_cursor_to_zero(stream_env, cursor):
    client, _, _, _ = stream_env

    response = client.get(f"/api/stream?cursor={cursor}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["record_id"] == 101
    assert data["stream_index"] == 0


def test_api_stream_returns_404_when_no_records_exist(
    stream_env, monkeypatch
):
    client, get_test_db, _, _ = stream_env

    conn = get_test_db()
    conn.execute("DELETE FROM decision_log")
    conn.commit()
    conn.close()

    response = client.get("/api/stream")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No data"


def test_api_stream_creates_notification_for_high_risk_record(stream_env):
    client, _, notification_calls, _ = stream_env

    response = client.get("/api/stream?cursor=1")

    assert response.status_code == 200
    assert len(notification_calls) == 1
    notification = notification_calls[0]
    assert notification["record_id"] == 102
    assert notification["notif_type"] == "risk_escalated"
    assert notification["level"] == "warning"


def test_api_stream_creates_critical_notification(stream_env):
    client, _, notification_calls, _ = stream_env

    response = client.get("/api/stream?cursor=2")

    assert response.status_code == 200
    assert len(notification_calls) == 1
    notification = notification_calls[0]
    assert notification["record_id"] == 103
    assert notification["notif_type"] == "critical_alert"
    assert notification["level"] == "critical"


def test_api_stream_does_not_duplicate_existing_notification(stream_env):
    client, _, notification_calls, existing_notifications = stream_env
    existing_notifications.add((102, "risk_escalated"))

    response = client.get("/api/stream?cursor=1")

    assert response.status_code == 200
    assert notification_calls == []


def test_api_stream_returns_500_when_database_fails(
    stream_env, monkeypatch
):
    client, _, _, _ = stream_env

    def raise_db_error():
        raise sqlite3.OperationalError("test stream database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/stream")

    assert response.status_code == 500
    assert "test stream database error" in response.get_json()["error"]