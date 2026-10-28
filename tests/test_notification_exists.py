import sqlite3
import app as app_module


def test_notification_exists_for_record_returns_true(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE notifications (
            record_id INTEGER,
            type TEXT
        )
    """)
    conn.execute(
        "INSERT INTO notifications (record_id, type) VALUES (?, ?)",
        (1, "HIGH_RISK"),
    )

    monkeypatch.setattr(app_module, "get_raw_conn", lambda: conn)

    try:
        assert app_module._notification_exists_for_record(
            1, "HIGH_RISK"
        ) is True
    finally:
        conn.close()


def test_notification_exists_for_record_returns_false(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE notifications (
            record_id INTEGER,
            type TEXT
        )
    """)

    monkeypatch.setattr(app_module, "get_raw_conn", lambda: conn)

    try:
        assert app_module._notification_exists_for_record(
            1, "HIGH_RISK"
        ) is False
    finally:
        conn.close()


def test_notification_exists_for_record_requires_matching_type(
    monkeypatch,
):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE notifications (
            record_id INTEGER,
            type TEXT
        )
    """)
    conn.execute(
        "INSERT INTO notifications (record_id, type) VALUES (?, ?)",
        (1, "HIGH_RISK"),
    )

    monkeypatch.setattr(app_module, "get_raw_conn", lambda: conn)

    try:
        assert app_module._notification_exists_for_record(
            1, "LOW_RISK"
        ) is False
    finally:
        conn.close()


def test_notification_exists_for_record_requires_matching_record_id(
    monkeypatch,
):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE notifications (
            record_id INTEGER,
            type TEXT
        )
    """)
    conn.execute(
        "INSERT INTO notifications (record_id, type) VALUES (?, ?)",
        (1, "HIGH_RISK"),
    )

    monkeypatch.setattr(app_module, "get_raw_conn", lambda: conn)

    try:
        assert app_module._notification_exists_for_record(
            2, "HIGH_RISK"
        ) is False
    finally:
        conn.close()