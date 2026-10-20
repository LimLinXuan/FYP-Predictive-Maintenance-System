
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def health_test_env(tmp_path, monkeypatch):
    """Create an isolated temporary database for System Health tests."""
    test_db = tmp_path / "test_system_health.db"

    # Make the application use the same database as the test.
    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY,
            timestamp TEXT
        );

        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            overall_status TEXT NOT NULL,
            api_latency_ms REAL,
            db_latency_ms REAL
        );

        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        ) VALUES
            (1, 'admin_test', 'admin@test.local', 'unused',
             'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused',
             'technician', 1, 0, '2026-01-01');
    """)

    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    yield test_db


def make_db():
    """Create an isolated in-memory database for helper-function tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY,
            timestamp TEXT
        );

        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            overall_status TEXT NOT NULL,
            api_latency_ms REAL,
            db_latency_ms REAL
        );

        INSERT INTO users (
            id, username, email, password_hash, role,
            is_active, must_change_password, created_at
        ) VALUES
            (1, 'admin_test', 'admin@test.local', 'unused',
             'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused',
             'technician', 1, 0, '2026-01-01');
    """)

    conn.commit()
    return conn


def authenticate(client, user_id=1):
    """Authenticate using Flask-Login's test session."""
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_database_health_returns_healthy(monkeypatch):
    conn = make_db()
    monkeypatch.setattr(app_module, "get_db", lambda: conn)

    try:
        result = app_module.check_database_health()

        assert result["status"] == "healthy"
        assert result["latency_ms"] is not None
        assert result["error"] is None
    finally:
        conn.close()


def test_database_health_returns_down_on_error(monkeypatch):
    def fail_db():
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(app_module, "get_db", fail_db)

    result = app_module.check_database_health()

    assert result["status"] == "down"
    assert result["latency_ms"] is None
    assert "Database unavailable" in result["error"]


def test_model_health_detects_missing_metrics_file(monkeypatch):
    conn = make_db()
    monkeypatch.setattr(app_module, "get_db", lambda: conn)
    monkeypatch.setattr(
        app_module, "MODEL_METRICS_CSV", "nonexistent_metrics_file.csv"
    )
    monkeypatch.setattr(app_module.os.path, "exists", lambda path: False)

    try:
        result = app_module.check_model_health()

        assert result["status"] == "down"
        assert result["metrics_file_exists"] is False
        assert result["decision_log_rows"] == 0
        assert result["predictions_rows"] == 0
        assert result["row_count_match"] is True
    finally:
        conn.close()


def test_model_health_detects_row_count_mismatch(monkeypatch, tmp_path):
    conn = make_db()
    conn.execute("INSERT INTO decision_log DEFAULT VALUES")
    conn.commit()

    metrics_file = tmp_path / "metrics.csv"
    metrics_file.write_text("metric,value\naccuracy,0.9\n", encoding="utf-8")

    monkeypatch.setattr(app_module, "get_db", lambda: conn)
    monkeypatch.setattr(app_module, "MODEL_METRICS_CSV", str(metrics_file))

    try:
        result = app_module.check_model_health()

        assert result["status"] == "degraded"
        assert result["metrics_file_exists"] is True
        assert result["decision_log_rows"] == 1
        assert result["predictions_rows"] == 0
        assert result["row_count_match"] is False
    finally:
        conn.close()


def test_model_health_returns_down_on_database_error(monkeypatch, tmp_path):
    metrics_file = tmp_path / "metrics.csv"
    metrics_file.write_text("metric,value\n", encoding="utf-8")

    monkeypatch.setattr(
        app_module, "MODEL_METRICS_CSV", str(metrics_file)
    )

    def fail_db():
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(app_module, "get_db", fail_db)

    result = app_module.check_model_health()

    assert result["status"] == "down"
    assert "Database unavailable" in result["error"]


def test_system_resources_returns_healthy(monkeypatch):
    class FakeMemory:
        percent = 50
        used = 4 * (1024 ** 3)
        total = 8 * (1024 ** 3)

    monkeypatch.setattr(
        app_module.psutil, "cpu_percent", lambda interval=None: 30
    )
    monkeypatch.setattr(
        app_module.psutil, "virtual_memory", lambda: FakeMemory()
    )

    result = app_module.check_system_resources()

    assert result["status"] == "healthy"
    assert result["cpu_percent"] == 30
    assert result["ram_percent"] == 50
    assert result["ram_used_gb"] == 4
    assert result["ram_total_gb"] == 8


def test_system_resources_returns_degraded_when_cpu_is_high(monkeypatch):
    class FakeMemory:
        percent = 50
        used = 4 * (1024 ** 3)
        total = 8 * (1024 ** 3)

    monkeypatch.setattr(
        app_module.psutil, "cpu_percent", lambda interval=None: 90
    )
    monkeypatch.setattr(
        app_module.psutil, "virtual_memory", lambda: FakeMemory()
    )

    result = app_module.check_system_resources()

    assert result["status"] == "degraded"


def test_system_resources_returns_down_on_error(monkeypatch):
    def fail_cpu(interval=None):
        raise RuntimeError("Resource check failed")

    monkeypatch.setattr(app_module.psutil, "cpu_percent", fail_cpu)

    result = app_module.check_system_resources()

    assert result["status"] == "down"
    assert "Resource check failed" in result["error"]


def test_last_incident_when_no_incident_exists(monkeypatch):
    conn = make_db()
    conn.execute(
        """
        INSERT INTO health_log
            (timestamp, overall_status, api_latency_ms, db_latency_ms)
        VALUES (?, ?, ?, ?)
        """,
        ("2026-10-20T10:00:00", "healthy", 10, 2),
    )
    conn.commit()

    monkeypatch.setattr(app_module, "get_db", lambda: conn)

    try:
        result = app_module.get_last_incident()
        assert result == {"has_incident": False}
    finally:
        conn.close()


def test_last_incident_detects_recovery(monkeypatch):
    conn = make_db()
    conn.executemany(
        """
        INSERT INTO health_log
            (timestamp, overall_status, api_latency_ms, db_latency_ms)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("2026-10-20T10:00:00", "degraded", 20, 3),
            ("2026-10-20T10:01:00", "healthy", 10, 2),
        ],
    )
    conn.commit()

    monkeypatch.setattr(app_module, "get_db", lambda: conn)

    try:
        result = app_module.get_last_incident()

        assert result["has_incident"] is True
        assert result["status"] == "degraded"
        assert result["timestamp"] == "2026-10-20T10:00:00"
        assert result["recovered"] is True
    finally:
        conn.close()


def test_health_api_returns_status_and_logs_check(
    health_test_env, monkeypatch
):
    conn = sqlite3.connect(health_test_env)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO decision_log (timestamp) VALUES (?)",
        ("2026-10-20T10:00:00",),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        app_module,
        "check_database_health",
        lambda: {"status": "healthy", "latency_ms": 2.0, "error": None},
    )
    monkeypatch.setattr(
        app_module,
        "check_model_health",
        lambda: {
            "status": "healthy",
            "metrics_file_exists": True,
            "decision_log_rows": 1,
            "predictions_rows": 0,
            "row_count_match": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        app_module,
        "check_system_resources",
        lambda: {
            "status": "healthy",
            "cpu_percent": 20,
            "ram_percent": 40,
        },
    )
    monkeypatch.setattr(
        app_module, "compute_health_score", lambda *args: 80
    )
    monkeypatch.setattr(
        app_module, "get_uptime_string", lambda: "1 day"
    )

    with app_module.app.test_client() as client:
        authenticate(client)
        response = client.get("/api/health")

    assert response.status_code == 200
    data = response.get_json()
    assert data["overall_status"] == "healthy"
    assert data["health_score"] == 80
    assert data["database"]["engine"] == "SQLite"
    assert data["database"]["record_count"] == 1
    assert data["last_incident"]["has_incident"] is False


def test_health_api_returns_down_when_a_component_is_down(
    health_test_env, monkeypatch
):
    monkeypatch.setattr(
        app_module,
        "check_database_health",
        lambda: {
            "status": "down",
            "latency_ms": None,
            "error": "Unavailable",
        },
    )
    monkeypatch.setattr(
        app_module,
        "check_model_health",
        lambda: {"status": "healthy", "decision_log_rows": 0},
    )
    monkeypatch.setattr(
        app_module,
        "check_system_resources",
        lambda: {"status": "healthy"},
    )
    monkeypatch.setattr(
        app_module, "compute_health_score", lambda *args: 20
    )
    monkeypatch.setattr(
        app_module, "get_uptime_string", lambda: "1 day"
    )

    with app_module.app.test_client() as client:
        authenticate(client)
        response = client.get("/api/health")

    assert response.status_code == 200
    data = response.get_json()
    assert data["overall_status"] == "down"
    assert data["checks"]["failed"] == 1


def test_health_history_returns_records(health_test_env):
    conn = sqlite3.connect(health_test_env)
    conn.executemany(
        """
        INSERT INTO health_log
            (timestamp, overall_status, api_latency_ms, db_latency_ms)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("2026-10-20T10:00:00", "degraded", 20.0, 3.0),
            ("2026-10-20T10:01:00", "healthy", 10.0, 2.0),
        ],
    )
    conn.commit()
    conn.close()

    with app_module.app.test_client() as client:
        authenticate(client)
        response = client.get("/api/health/history")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 2
    assert data[0]["overall_status"] == "degraded"
    assert data[1]["overall_status"] == "healthy"
