import sqlite3

import pytest
import app as app_module


@pytest.fixture
def init_db(tmp_path, monkeypatch):
    """Use an isolated temporary database for initialization tests."""
    test_db = tmp_path / "test_db_initialization.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    yield test_db

    monkeypatch.setattr(app_module, "_db_configured", False)


def get_columns(db_path, table_name):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[1]
            for row in conn.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
        }
    finally:
        conn.close()


def get_index_names(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
            """).fetchall()
        }
    finally:
        conn.close()


def create_index_dependency_tables(db_path):
    """Create only the columns required by init_indexes()."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                risk_score REAL,
                alert_status TEXT,
                risk_level TEXT
            );

            CREATE TABLE work_orders (
                wo_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                status TEXT
            );

            CREATE TABLE notifications (
                notification_id INTEGER PRIMARY KEY,
                username TEXT
            );

            CREATE TABLE pm_schedules (
                pm_id INTEGER PRIMARY KEY,
                machine_id TEXT
            );

            CREATE TABLE maintenance_records (
                maint_id INTEGER PRIMARY KEY,
                machine_id TEXT
            );

            CREATE TABLE downtime_events (
                event_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                end_time TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()


def test_init_workflow_columns_adds_all_seven_columns(init_db):
    conn = sqlite3.connect(init_db)
    try:
        conn.execute("""
            CREATE TABLE work_orders (
                wo_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()
    finally:
        conn.close()

    app_module.init_workflow_columns()

    columns = get_columns(init_db, "work_orders")

    expected_columns = {
        "accepted_at",
        "started_at",
        "technician_notes",
        "resolution_note",
        "verification_note",
        "verified_by",
        "verified_at",
    }

    assert expected_columns.issubset(columns)


def test_init_workflow_columns_is_safe_to_run_twice(init_db):
    conn = sqlite3.connect(init_db)
    try:
        conn.execute("""
            CREATE TABLE work_orders (
                wo_id INTEGER PRIMARY KEY,
                accepted_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()

    app_module.init_workflow_columns()
    app_module.init_workflow_columns()

    columns = get_columns(init_db, "work_orders")

    assert {
        "accepted_at",
        "started_at",
        "technician_notes",
        "resolution_note",
        "verification_note",
        "verified_by",
        "verified_at",
    }.issubset(columns)


def test_init_workflow_columns_handles_missing_table(init_db):
    # The function catches OperationalError for each ALTER statement.
    # A missing table should therefore not crash the function.
    app_module.init_workflow_columns()


def test_init_indexes_creates_all_expected_indexes(init_db):
    create_index_dependency_tables(init_db)

    app_module.init_indexes()

    indexes = get_index_names(init_db)

    expected_indexes = {
        "idx_decision_log_risk_score",
        "idx_decision_log_alert_status",
        "idx_decision_log_risk_level",
        "idx_work_orders_machine_id",
        "idx_work_orders_status",
        "idx_notifications_username",
        "idx_pm_schedules_machine_id",
        "idx_maintenance_records_machine_id",
        "idx_downtime_events_open",
    }

    assert expected_indexes.issubset(indexes)


def test_init_indexes_is_safe_to_run_twice(init_db):
    create_index_dependency_tables(init_db)

    app_module.init_indexes()
    indexes_before = get_index_names(init_db)

    app_module.init_indexes()
    indexes_after = get_index_names(init_db)

    assert indexes_after == indexes_before


def test_init_indexes_continues_when_some_tables_are_missing(init_db):
    # Only decision_log exists; other index statements should fail
    # individually and be skipped by the function.
    conn = sqlite3.connect(init_db)
    try:
        conn.execute("""
            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                risk_score REAL,
                alert_status TEXT,
                risk_level TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()

    app_module.init_indexes()

    indexes = get_index_names(init_db)

    assert "idx_decision_log_risk_score" in indexes
    assert "idx_decision_log_alert_status" in indexes
    assert "idx_decision_log_risk_level" in indexes
