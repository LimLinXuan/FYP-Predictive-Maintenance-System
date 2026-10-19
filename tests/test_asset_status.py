
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def asset_env(tmp_path, monkeypatch):
    """Use an isolated temporary database for asset status tests."""
    test_db = tmp_path / "test_asset_status.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    conn = sqlite3.connect(test_db)
    conn.executescript("""
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

        CREATE TABLE downtime_events (
            downtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_minutes INTEGER,
            reason_category TEXT NOT NULL,
            reason_detail TEXT,
            linked_wo_id INTEGER,
            reported_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE work_orders (
            wo_id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN'
        );

        CREATE TABLE pm_schedules (
            pm_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            pm_name TEXT NOT NULL,
            frequency_type TEXT NOT NULL,
            frequency_value INTEGER NOT NULL,
            last_performed_at TEXT,
            last_performed_tool_wear INTEGER,
            next_due_at TEXT,
            status TEXT NOT NULL DEFAULT 'ON_SCHEDULE',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        INSERT INTO assets (
            asset_id, machine_id, machine_name, line_id, status
        ) VALUES (
            'AST-TEST-001', 'MCH-TEST-001',
            'Test Machine', 'LINE-TEST-01', 'RUNNING'
        );
    """)
    conn.commit()
    conn.close()

    return test_db


def read_asset_status(db_path, machine_id="MCH-TEST-001"):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM assets WHERE machine_id = ?",
            (machine_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def add_downtime(db_path, end_time=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO downtime_events (
                asset_id, machine_id, start_time, end_time,
                reason_category, reported_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "AST-TEST-001",
            "MCH-TEST-001",
            "2026-01-01T10:00:00",
            end_time,
            "UNPLANNED_FAILURE",
            "test_user",
            "2026-01-01T10:00:00",
        ))
        conn.commit()
    finally:
        conn.close()


def add_work_order(db_path, status):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO work_orders (machine_id, status) VALUES (?, ?)",
            ("MCH-TEST-001", status),
        )
        conn.commit()
    finally:
        conn.close()


def add_pm_schedule(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO pm_schedules (
                asset_id, machine_id, pm_name,
                frequency_type, frequency_value,
                next_due_at, status, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "AST-TEST-001",
            "MCH-TEST-001",
            "Test preventive maintenance",
            "DAYS",
            30,
            "2020-01-01T00:00:00",
            "ON_SCHEDULE",
            "test_user",
            "2026-01-01T00:00:00",
        ))
        conn.commit()
    finally:
        conn.close()


def set_pm_result(monkeypatch, result):
    monkeypatch.setattr(
        app_module,
        "compute_pm_status",
        lambda pm: (result, None),
    )


def test_asset_defaults_to_running(asset_env):
    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "RUNNING"
    assert read_asset_status(asset_env) == "RUNNING"


def test_open_downtime_sets_asset_down(asset_env):
    add_downtime(asset_env)

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "DOWN"
    assert read_asset_status(asset_env) == "DOWN"


def test_closed_downtime_does_not_set_asset_down(asset_env):
    add_downtime(asset_env, end_time="2026-01-01T11:00:00")

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "RUNNING"
    assert read_asset_status(asset_env) == "RUNNING"


@pytest.mark.parametrize("wo_status", ["ACCEPTED", "IN_PROGRESS"])
def test_active_work_order_sets_maintenance(
    asset_env, wo_status
):
    add_work_order(asset_env, wo_status)

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "MAINTENANCE"
    assert read_asset_status(asset_env) == "MAINTENANCE"


@pytest.mark.parametrize("wo_status", ["OPEN", "ASSIGNED", "CLOSED"])
def test_non_active_work_order_does_not_set_maintenance(
    asset_env, wo_status
):
    add_work_order(asset_env, wo_status)

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "RUNNING"
    assert read_asset_status(asset_env) == "RUNNING"


def test_overdue_pm_sets_warning(asset_env, monkeypatch):
    add_pm_schedule(asset_env)
    set_pm_result(monkeypatch, "OVERDUE")

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "WARNING"
    assert read_asset_status(asset_env) == "WARNING"


def test_on_schedule_pm_keeps_asset_running(asset_env, monkeypatch):
    add_pm_schedule(asset_env)
    set_pm_result(monkeypatch, "ON_SCHEDULE")

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "RUNNING"
    assert read_asset_status(asset_env) == "RUNNING"


def test_downtime_takes_priority_over_work_order_and_pm(
    asset_env, monkeypatch
):
    add_downtime(asset_env)
    add_work_order(asset_env, "IN_PROGRESS")
    add_pm_schedule(asset_env)
    set_pm_result(monkeypatch, "OVERDUE")

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "DOWN"
    assert read_asset_status(asset_env) == "DOWN"


def test_work_order_takes_priority_over_overdue_pm(
    asset_env, monkeypatch
):
    add_work_order(asset_env, "IN_PROGRESS")
    add_pm_schedule(asset_env)
    set_pm_result(monkeypatch, "OVERDUE")

    result = app_module.refresh_asset_status("MCH-TEST-001")

    assert result == "MAINTENANCE"
    assert read_asset_status(asset_env) == "MAINTENANCE"


def test_refresh_nonexistent_machine_does_not_create_asset(asset_env):
    result = app_module.refresh_asset_status("MCH-NONEXISTENT")

    assert result == "RUNNING"
    assert read_asset_status(asset_env, "MCH-NONEXISTENT") is None
