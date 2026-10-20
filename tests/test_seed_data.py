import sqlite3

import pytest
import app as app_module


@pytest.fixture
def seed_db(tmp_path, monkeypatch):
    """Create an isolated temporary database for seed-function tests."""
    test_db = tmp_path / "test_seed_data.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    # Create the schema using the application's own initialization functions.
    app_module.init_production_lines_table()
    app_module.init_assets_table()
    # Minimal schema required by get_latest_tool_wear().
    conn = sqlite3.connect(test_db)
    try:
        conn.execute("""
            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                tool_wear INTEGER
            )
        """)
        conn.commit()
    finally:
        conn.close()
    app_module.init_work_orders_table()
    app_module.init_pm_schedules_table()
    app_module.init_maintenance_records_table()
    app_module.init_downtime_events_table()

    yield test_db

    # Connections are closed by each application function.
    monkeypatch.setattr(app_module, "_db_configured", False)


def query_count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_seed_assets_and_lines_creates_expected_records(seed_db):
    app_module.seed_assets_and_lines()

    assert query_count(seed_db, "production_lines") == 4
    assert query_count(seed_db, "assets") == app_module.NUM_MACHINES

    conn = sqlite3.connect(seed_db)
    try:
        lines = conn.execute(
            "SELECT line_id FROM production_lines ORDER BY line_id"
        ).fetchall()
        assets = conn.execute(
            "SELECT asset_id, machine_id FROM assets ORDER BY machine_id"
        ).fetchall()

        assert [row[0] for row in lines] == [
            "LINE-01", "LINE-02", "LINE-03", "LINE-04"
        ]
        assert len({row[1] for row in assets}) == app_module.NUM_MACHINES
        assert assets[0] == ("AST-1001", "MCH-001")
    finally:
        conn.close()


def test_seed_assets_and_lines_is_idempotent(seed_db):
    app_module.seed_assets_and_lines()

    before_lines = query_count(seed_db, "production_lines")
    before_assets = query_count(seed_db, "assets")

    app_module.seed_assets_and_lines()

    assert query_count(seed_db, "production_lines") == before_lines
    assert query_count(seed_db, "assets") == before_assets


def test_seed_demo_maintenance_data_creates_pm_for_every_machine(seed_db):
    app_module.seed_assets_and_lines()
    app_module.seed_demo_maintenance_data()

    conn = sqlite3.connect(seed_db)
    try:
        pm_counts = conn.execute("""
            SELECT machine_id, COUNT(*)
            FROM pm_schedules
            GROUP BY machine_id
        """).fetchall()

        assert len(pm_counts) == app_module.NUM_MACHINES
        assert all(count >= 1 for _, count in pm_counts)

        assert conn.execute(
            "SELECT COUNT(*) FROM work_orders"
        ).fetchone()[0] > 0

        assert conn.execute(
            "SELECT COUNT(*) FROM maintenance_records"
        ).fetchone()[0] > 0

        assert conn.execute(
            "SELECT COUNT(*) FROM downtime_events"
        ).fetchone()[0] > 0
    finally:
        conn.close()


def test_seed_demo_maintenance_data_is_idempotent(seed_db):
    app_module.seed_assets_and_lines()
    app_module.seed_demo_maintenance_data()

    tables = (
        "pm_schedules",
        "work_orders",
        "maintenance_records",
        "downtime_events",
    )
    before = {table: query_count(seed_db, table) for table in tables}

    app_module.seed_demo_maintenance_data()

    after = {table: query_count(seed_db, table) for table in tables}
    assert after == before
