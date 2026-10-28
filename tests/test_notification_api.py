import json
import queue
import sqlite3
import threading

import pytest
import app as app_module
from flask_login import UserMixin


class MockUser(UserMixin):
    id = "1"
    username = "test_user"

    def __init__(self, is_admin=False):
        self.is_admin = is_admin


@pytest.fixture
def notification_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_notifications.db"

    # Create a temporary test database; never access the real database.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                title TEXT,
                message TEXT,
                level TEXT,
                type TEXT,
                created_at TEXT,
                read_at TEXT
            )
        """)

        conn.executemany(
            """
            INSERT INTO notifications
                (id, username, title, message, level, type, created_at, read_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1, "test_user", "Personal", "Personal message",
                    "info", "general", "2026-10-19T10:00:00", None
                ),
                (
                    2, None, "Public", "Public message",
                    "info", "general", "2026-10-19T10:01:00", None
                ),
                (
                    3, "other_user", "Other", "Other user's message",
                    "info", "general", "2026-10-19T10:02:00", None
                ),
                (
                    4, "test_user", "Already read", "Old message",
                    "info", "general", "2026-10-19T10:03:00",
                    "2026-10-19T10:04:00"
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    def connect_test_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(app_module, "get_db", connect_test_db)

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser() if user_id == "1" else None
        ),
    )

    # Prevent subscribers and locks from leaking between tests.
    monkeypatch.setattr(app_module, "_notification_subscribers", {})
    monkeypatch.setattr(app_module, "_notification_lock", threading.Lock())

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, db_path


def read_notifications(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            """
            SELECT id, username, read_at
            FROM notifications
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------
# Mark a single notification as read
# --------------------------------------------------

def test_notification_read_marks_own_notification(notification_env):
    client, db_path = notification_env

    response = client.post("/api/notifications/1/read")

    assert response.status_code == 200
    assert response.get_json() == {"message": "Marked as read"}

    rows = read_notifications(db_path)
    assert rows[0][2] is not None
    assert rows[2][2] is None


def test_notification_read_allows_public_notification(notification_env):
    client, db_path = notification_env

    response = client.post("/api/notifications/2/read")

    assert response.status_code == 200
    assert read_notifications(db_path)[1][2] is not None


def test_notification_read_does_not_mark_other_users_notification(
    notification_env,
):
    client, db_path = notification_env

    response = client.post("/api/notifications/3/read")

    assert response.status_code == 200
    assert read_notifications(db_path)[2][2] is None


def test_notification_read_missing_id_returns_success_without_changing_rows(
    notification_env,
):
    client, db_path = notification_env

    response = client.post("/api/notifications/999/read")

    assert response.status_code == 200
    assert response.get_json() == {"message": "Marked as read"}

    rows = read_notifications(db_path)
    assert all(row[2] is None for row in rows[:3])


def test_notification_read_returns_500_when_database_fails(
    notification_env, monkeypatch
):
    client, _ = notification_env

    def fail_get_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "get_db", fail_get_db)

    response = client.post("/api/notifications/1/read")

    assert response.status_code == 500
    assert response.get_json()["error"] == "database unavailable"


# --------------------------------------------------
# Mark all notifications as read
# --------------------------------------------------

def test_read_all_marks_only_own_notifications_for_regular_user(
    notification_env,
):
    client, db_path = notification_env

    response = client.post("/api/notifications/read-all")

    assert response.status_code == 200
    assert response.get_json() == {"message": "All marked as read"}

    rows = read_notifications(db_path)

    # Own unread notification is marked as read.
    assert rows[0][2] is not None

    # Regular users do not mark public or other users' notifications.
    assert rows[1][2] is None
    assert rows[2][2] is None

    # Previously read notification remains read.
    assert rows[3][2] is not None


def test_read_all_admin_marks_own_and_public_notifications(
    notification_env, monkeypatch
):
    client, db_path = notification_env

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser(is_admin=True) if user_id == "1" else None
        ),
    )

    response = client.post("/api/notifications/read-all")

    assert response.status_code == 200
    assert response.get_json() == {"message": "All marked as read"}

    rows = read_notifications(db_path)

    assert rows[0][2] is not None
    assert rows[1][2] is not None
    assert rows[2][2] is None


def test_read_all_returns_500_when_database_fails(
    notification_env, monkeypatch
):
    client, _ = notification_env

    def fail_get_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "get_db", fail_get_db)

    response = client.post("/api/notifications/read-all")

    assert response.status_code == 500
    assert response.get_json()["error"] == "database unavailable"


# --------------------------------------------------
# Server-Sent Events (SSE) notification stream
# --------------------------------------------------

def test_notifications_stream_sends_initial_ping_and_cleans_subscription(
    notification_env,
):
    client, _ = notification_env

    response = client.get(
        "/api/notifications/stream",
        buffered=False,
    )

    try:
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"

        first_event = next(response.response)
        assert first_event.decode("utf-8") == (
            "event: ping\ndata: connected\n\n"
        )

        assert "test_user" in app_module._notification_subscribers
        assert len(app_module._notification_subscribers["test_user"]) == 1
    finally:
        response.close()

    assert app_module._notification_subscribers["test_user"] == []


def test_notifications_stream_sends_queued_message(notification_env):
    client, _ = notification_env

    response = client.get(
        "/api/notifications/stream",
        buffered=False,
    )

    try:
        # Start the generator and consume its initial connection event.
        next(response.response)

        subscribers = app_module._notification_subscribers["test_user"]
        assert len(subscribers) == 1

        subscribers[0].put({
            "type": "notification",
            "id": 123,
        })

        event = next(response.response).decode("utf-8")

        assert event.startswith("data: ")
        assert event.endswith("\n\n")

        payload = json.loads(event[len("data: "):-2])
        assert payload == {
            "type": "notification",
            "id": 123,
        }
    finally:
        response.close()

    assert app_module._notification_subscribers["test_user"] == []


def test_notifications_stream_keepalive_on_empty_queue(
    notification_env, monkeypatch
):
    client, _ = notification_env

    # Exercise queue.Empty without waiting for the real 25-second timeout.
    original_queue = app_module.queue.Queue

    class EmptyQueue(original_queue):
        def get(self, timeout=None):
            raise queue.Empty

    monkeypatch.setattr(app_module.queue, "Queue", EmptyQueue)

    response = client.get(
        "/api/notifications/stream",
        buffered=False,
    )

    try:
        # Consume the initial connection event.
        next(response.response)

        keepalive = next(response.response).decode("utf-8")

        assert keepalive == "event: ping\ndata: keepalive\n\n"
    finally:
        response.close()

    assert app_module._notification_subscribers["test_user"] == []


# --------------------------------------------------
# Get notification list
# --------------------------------------------------

def test_api_notifications_regular_user_sees_only_own_notifications(
    notification_env,
):
    client, _ = notification_env

    response = client.get("/api/notifications")

    assert response.status_code == 200

    data = response.get_json()

    assert "notifications" in data
    assert data["unread_count"] == 1

    notifications = data["notifications"]
    assert [item["id"] for item in notifications] == [4, 1]
    assert all(
        item["username"] == "test_user"
        for item in notifications
    )


def test_api_notifications_admin_sees_own_and_public_notifications(
    notification_env, monkeypatch
):
    client, _ = notification_env

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser(is_admin=True) if user_id == "1" else None
        ),
    )

    response = client.get("/api/notifications")

    assert response.status_code == 200

    data = response.get_json()

    assert data["unread_count"] == 2

    notifications = data["notifications"]
    assert [item["id"] for item in notifications] == [4, 2, 1]

    usernames = [item["username"] for item in notifications]
    assert usernames == ["test_user", None, "test_user"]


def test_api_notifications_returns_empty_list_when_no_notifications(
    notification_env, monkeypatch
):
    client, db_path = notification_env

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM notifications")
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/notifications")

    assert response.status_code == 200
    assert response.get_json() == {
        "notifications": [],
        "unread_count": 0,
    }


def test_api_notifications_returns_500_when_database_fails(
    notification_env, monkeypatch
):
    client, _ = notification_env

    def fail_get_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "get_db", fail_get_db)

    response = client.get("/api/notifications")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "database unavailable",
    }
