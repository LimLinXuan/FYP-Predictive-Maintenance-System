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
def work_orders_env(tmp_path, monkeypatch):
    """Use an isolated temporary SQLite database for every test."""
    db_path = tmp_path / "test_work_orders.db"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE work_orders (
            wo_id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER,
            machine_id TEXT,
            asset_id TEXT,
            title TEXT,
            description TEXT,
            recommended_action TEXT,
            risk_level TEXT,
            risk_score REAL,
            priority TEXT,
            status TEXT,
            assigned_to TEXT,
            created_by TEXT,
            created_at TEXT,
            due_date TEXT
        );

        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            machine_id TEXT,
            risk_level TEXT,
            risk_score REAL,
            pred_prob REAL,
            action TEXT,
            reasons TEXT,
            shap_explanation TEXT,
            alert_status TEXT
        );
    """)

    conn.executemany("""
        INSERT INTO work_orders (
            wo_id, record_id, machine_id, asset_id, title,
            description, recommended_action, risk_level, risk_score,
            priority, status, assigned_to, created_by, created_at, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            1, 101, "MCH-001", "AST-1001", "Critical inspection",
            "High risk detected", "Inspect machine", "HIGH", 90,
            "CRITICAL", "OPEN", "test_tech", "admin",
            "2026-10-18T10:00:00", "2026-10-21T10:00:00"
        ),
        (
            2, 102, "MCH-002", "AST-1002", "Motor inspection",
            "Check motor", "Check motor condition", "MEDIUM", 60,
            "MEDIUM", "ASSIGNED", "another_tech", "admin",
            "2026-10-18T09:00:00", "2026-10-25T10:00:00"
        ),
        (
            3, None, "MCH-003", "AST-1003", "General maintenance",
            "Routine maintenance", "Perform maintenance", "LOW", 20,
            "LOW", "OPEN", None, "admin",
            "2026-10-17T10:00:00", "2026-10-28T10:00:00"
        ),
        (
            4, 104, "MCH-004", "AST-1004", "Completed inspection",
            "Inspection done", "Review result", "LOW", 10,
            "LOW", "CLOSED", "test_tech", "admin",
            "2026-10-16T10:00:00", "2026-10-17T10:00:00"
        ),
    ])

    conn.executemany("""
        INSERT INTO decision_log (
            record_id, machine_id, risk_level, risk_score, pred_prob,
            action, reasons, shap_explanation, alert_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            101, "MCH-001", "HIGH", 90, 0.95,
            "Inspect immediately", "High failure risk",
            "Tool wear contributed to risk", "OPEN"
        ),
        (
            102, "MCH-002", "MEDIUM", 60, 0.65,
            "Monitor machine", "Moderate failure risk",
            "Torque contributed to risk", "OPEN"
        ),
        (
            103, "MCH-003", "LOW", 20, 0.15,
            "Continue monitoring", "Low failure risk",
            "No major risk factors", "OPEN"
        ),
        (
            104, "MCH-004", "LOW", 10, 0.05,
            "No action required", "Low failure risk",
            "No major risk factors", "RESOLVED"
        ),
    ])

    conn.commit()
    conn.close()

    def get_test_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(app_module, "get_db", get_test_db)

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser() if user_id == "1" else None
        ),
    )

    # Prevent real audit-log side effects.
    monkeypatch.setattr(
        app_module, "log_action",
        lambda *args, **kwargs: None
    )

    # Deterministic values for create-from-alert tests.
    monkeypatch.setattr(
        app_module,
        "machine_id_for_record",
        lambda record_id: {
            101: "MCH-001",
            102: "MCH-002",
            103: "MCH-003",
        }.get(record_id, "MCH-TEST"),
    )

    monkeypatch.setattr(
        app_module,
        "asset_id_for_machine",
        lambda machine_id: {
            "MCH-001": "AST-1001",
            "MCH-002": "AST-1002",
            "MCH-003": "AST-1003",
            "MCH-TEST": "AST-TEST",
        }.get(machine_id),
    )

    monkeypatch.setattr(
        app_module,
        "compute_due_date",
        lambda priority: "2026-11-01T00:00:00",
    )

    app_module.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, get_test_db, db_path


def make_admin(monkeypatch):
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser(is_admin=True) if user_id == "1" else None
        ),
    )


# -------------------------------------------------------------------
# GET /api/work-orders
# -------------------------------------------------------------------

def test_work_orders_returns_only_assigned_and_unassigned_for_technician(
    work_orders_env,
):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders")

    assert response.status_code == 200
    data = response.get_json()
    ids = {row["wo_id"] for row in data}

    assert ids == {1, 3, 4}
    assert 2 not in ids


def test_work_orders_admin_can_see_all_records(work_orders_env, monkeypatch):
    client, _, _ = work_orders_env
    make_admin(monkeypatch)

    response = client.get("/api/work-orders")

    assert response.status_code == 200
    assert {row["wo_id"] for row in response.get_json()} == {1, 2, 3, 4}


def test_work_orders_filters_by_status(work_orders_env):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders?status=OPEN")

    assert response.status_code == 200
    data = response.get_json()

    assert all(row["status"] == "OPEN" for row in data)
    assert {row["wo_id"] for row in data} == {1, 3}


def test_work_orders_filters_by_priority(work_orders_env):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders?priority=CRITICAL")

    assert response.status_code == 200
    data = response.get_json()

    assert len(data) == 1
    assert data[0]["wo_id"] == 1
    assert data[0]["priority"] == "CRITICAL"


def test_work_orders_all_filter_values_do_not_restrict_results(
    work_orders_env, monkeypatch
):
    client, _, _ = work_orders_env
    make_admin(monkeypatch)

    response = client.get("/api/work-orders?status=ALL&priority=ALL")

    assert response.status_code == 200
    assert len(response.get_json()) == 4


def test_work_orders_are_sorted_by_priority(work_orders_env, monkeypatch):
    client, _, _ = work_orders_env
    make_admin(monkeypatch)

    response = client.get("/api/work-orders")

    assert response.status_code == 200
    data = response.get_json()

    priorities = [row["priority"] for row in data]
    assert priorities.index("CRITICAL") < priorities.index("MEDIUM")
    assert priorities.index("MEDIUM") < priorities.index("LOW")


def test_work_orders_returns_empty_list_when_no_records_match(work_orders_env):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders?status=NON_EXISTENT_STATUS")

    assert response.status_code == 200
    assert response.get_json() == []


def test_work_orders_returns_500_on_database_error(work_orders_env, monkeypatch):
    client, _, _ = work_orders_env

    def raise_db_error():
        raise sqlite3.OperationalError("test database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/work-orders")

    assert response.status_code == 500
    assert "test database error" in response.get_json()["error"]


# -------------------------------------------------------------------
# GET /api/work-orders/<wo_id>
# -------------------------------------------------------------------

def test_work_order_detail_returns_record_and_source_alert(work_orders_env):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders/1")

    assert response.status_code == 200
    data = response.get_json()

    assert data["wo_id"] == 1
    assert data["title"] == "Critical inspection"
    assert data["source_alert"]["record_id"] == 101
    assert data["source_alert"]["risk_level"] == "HIGH"
    assert data["source_alert"]["risk_score"] == 90


def test_work_order_detail_returns_null_source_alert_when_record_missing(
    work_orders_env,
):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders/999")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Work order not found"


def test_work_order_detail_without_record_id_omits_source_alert(
    work_orders_env,
):
    client, _, _ = work_orders_env

    response = client.get("/api/work-orders/3")

    assert response.status_code == 200
    data = response.get_json()

    assert data["wo_id"] == 3
    assert "source_alert" not in data


def test_work_order_detail_returns_null_when_linked_alert_is_missing(
    work_orders_env,
):
    client, get_db, _ = work_orders_env

    conn = get_db()
    conn.execute(
        "UPDATE work_orders SET record_id=? WHERE wo_id=?",
        (999, 3),
    )
    conn.commit()
    conn.close()

    response = client.get("/api/work-orders/3")

    assert response.status_code == 200
    assert response.get_json()["source_alert"] is None


def test_work_order_detail_returns_500_on_database_error(
    work_orders_env, monkeypatch
):
    client, _, _ = work_orders_env

    def raise_db_error():
        raise sqlite3.OperationalError("test detail database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/work-orders/1")

    assert response.status_code == 500
    assert "test detail database error" in response.get_json()["error"]


# -------------------------------------------------------------------
# POST /api/work-orders/from-alert/<record_id>
# -------------------------------------------------------------------

def test_create_work_order_from_alert_success(work_orders_env):
    client, get_db, _ = work_orders_env

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 201
    data = response.get_json()

    assert data["message"] == "Work order created"
    assert "wo_id" in data

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM work_orders WHERE wo_id=?",
        (data["wo_id"],),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["record_id"] == 103
    assert row["machine_id"] == "MCH-003"
    assert row["asset_id"] == "AST-1003"
    assert row["title"] == "LOW Risk — Machine MCH-003"
    assert row["status"] == "OPEN"
    assert row["assigned_to"] is None
    assert row["created_by"] == "test_tech"


def test_create_work_order_from_alert_uses_custom_title(work_orders_env):
    client, get_db, _ = work_orders_env

    response = client.post(
        "/api/work-orders/from-alert/103",
        json={"title": "Investigate machine vibration"},
    )

    assert response.status_code == 201

    wo_id = response.get_json()["wo_id"]
    conn = get_db()
    row = conn.execute(
        "SELECT title FROM work_orders WHERE wo_id=?",
        (wo_id,),
    ).fetchone()
    conn.close()

    assert row["title"] == "Investigate machine vibration"


def test_create_work_order_from_missing_alert_returns_404(work_orders_env):
    client, get_db, _ = work_orders_env

    response = client.post("/api/work-orders/from-alert/999")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Alert not found"

    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
    conn.close()

    assert count == 4


@pytest.mark.parametrize("alert_status", ["RESOLVED", "FALSE_POSITIVE"])
def test_create_work_order_from_closed_alert_returns_400(
    work_orders_env, alert_status
):
    client, get_db, _ = work_orders_env

    conn = get_db()
    conn.execute(
        "UPDATE decision_log SET alert_status=? WHERE record_id=103",
        (alert_status,),
    )
    conn.commit()
    conn.close()

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 400
    assert "Reopen this alert" in response.get_json()["error"]


@pytest.mark.parametrize(
    "existing_status",
    ["OPEN", "ASSIGNED", "ACCEPTED", "IN_PROGRESS", "PENDING_VERIFICATION"],
)
def test_create_work_order_from_alert_rejects_existing_open_work_order(
    work_orders_env, existing_status
):
    client, get_db, _ = work_orders_env

    conn = get_db()
    conn.execute(
        "UPDATE work_orders SET record_id=103, status=? WHERE wo_id=3",
        (existing_status,),
    )
    conn.commit()
    conn.close()

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 409
    data = response.get_json()

    assert data["error"] == "An open work order already exists for this alert"
    assert data["wo_id"] == 3


def test_create_work_order_from_alert_allows_new_order_when_old_one_is_closed(
    work_orders_env,
):
    client, get_db, _ = work_orders_env

    conn = get_db()
    conn.execute(
        "UPDATE work_orders SET record_id=103, status='CLOSED' WHERE wo_id=3"
    )
    conn.commit()
    conn.close()

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 201


def test_create_work_order_from_alert_returns_500_on_database_error(
    work_orders_env, monkeypatch
):
    client, _, _ = work_orders_env

    def raise_db_error():
        raise sqlite3.OperationalError("test create database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 500
    assert "test create database error" in response.get_json()["error"]


def test_create_work_order_from_alert_returns_500_when_insert_fails(
    work_orders_env, monkeypatch
):
    client, get_test_db, _ = work_orders_env

    class FailingInsertConnection:
        """Wrap a temporary SQLite connection and fail only on work-order INSERT."""

        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            if "INSERT INTO WORK_ORDERS" in sql.upper():
                raise sqlite3.OperationalError("test insert failure")
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        app_module,
        "get_db",
        lambda: FailingInsertConnection(get_test_db()),
    )

    response = client.post("/api/work-orders/from-alert/103")

    assert response.status_code == 500
    assert "test insert failure" in response.get_json()["error"]
