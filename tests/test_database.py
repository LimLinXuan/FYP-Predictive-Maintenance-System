
import sqlite3

import app as app_module


def test_get_raw_conn_uses_configured_database(tmp_path, monkeypatch):
    """Database connections should use the configured database path."""
    db_path = tmp_path / "test_database.db"
    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

    conn = app_module.get_raw_conn()

    try:
        result = conn.execute("PRAGMA database_list").fetchone()
        assert result[2] == str(db_path)
        assert db_path.exists()
    finally:
        conn.close()


def test_get_db_returns_connection_with_row_factory(tmp_path, monkeypatch):
    """Flask's request-scoped database connection should return sqlite rows."""
    db_path = tmp_path / "test_get_db.db"
    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

    with app_module.app.test_request_context("/"):
        conn = app_module.get_db()

        assert conn.row_factory is sqlite3.Row
        assert app_module.get_db() is conn

        conn.execute("CREATE TABLE test_table (id INTEGER, name TEXT)")
        conn.execute(
            "INSERT INTO test_table (id, name) VALUES (?, ?)",
            (1, "test"),
        )

        row = conn.execute(
            "SELECT id, name FROM test_table"
        ).fetchone()

        assert row["id"] == 1
        assert row["name"] == "test"


def test_init_core_tables_creates_expected_tables(tmp_path, monkeypatch):
    """Core table initialization should create the required tables."""
    db_path = tmp_path / "test_init.db"
    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

    app_module.init_users_table()
    app_module.init_notifications_table()
    app_module.init_production_lines_table()
    app_module.init_assets_table()
    app_module.init_work_orders_table()
    app_module.init_pm_schedules_table()

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        assert {
            "users",
            "notifications",
            "production_lines",
            "assets",
            "work_orders",
            "pm_schedules",
        }.issubset(tables)
    finally:
        conn.close()


def test_core_table_initialization_is_idempotent(tmp_path, monkeypatch):
    """Running core table initializers twice should not fail."""
    db_path = tmp_path / "test_idempotent.db"
    monkeypatch.setattr(app_module, "DB", str(db_path))
    monkeypatch.setattr(app_module, "_db_configured", False)

    initializers = [
        app_module.init_users_table,
        app_module.init_notifications_table,
        app_module.init_production_lines_table,
        app_module.init_assets_table,
        app_module.init_work_orders_table,
        app_module.init_pm_schedules_table,
    ]

    for initializer in initializers:
        initializer()

    # Reset only the DB configuration flag; keep the same temporary DB.
    monkeypatch.setattr(app_module, "_db_configured", False)

    for initializer in initializers:
        initializer()

    conn = sqlite3.connect(db_path)
    try:
        for table in (
            "users",
            "notifications",
            "production_lines",
            "assets",
            "work_orders",
            "pm_schedules",
        ):
            result = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            assert result is not None, f"Missing table: {table}"
    finally:
        conn.close()
