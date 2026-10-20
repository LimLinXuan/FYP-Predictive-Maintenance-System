
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import app as app_module
from flask_login import UserMixin


class FakeUser(UserMixin):
    id = "1"
    username = "technician1"
    is_admin = False


class FakeAdmin(UserMixin):
    id = "1"
    username = "admin_test"
    is_admin = True


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Create an isolated database and an authenticated test client."""
    db_path = tmp_path / "test_summary.db"

    # Create only the table required by these summary endpoints.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE work_orders (
                wo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                assigned_to TEXT,
                due_date TEXT
            )
            """
        )

    def get_test_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(app_module, "get_db", get_test_db)

    # Use test users instead of querying the real users table.
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: FakeUser() if user_id == "1" else None,
    )

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()

    # Authenticate through Flask-Login's session.
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True

    return client, db_path, monkeypatch


def insert_work_orders(db_path, rows):
    """Insert work orders into the isolated test database."""
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO work_orders (status, assigned_to, due_date)
            VALUES (?, ?, ?)
            """,
            rows,
        )


def past_date():
    """Return a due date safely in the past."""
    return (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(days=2)
    ).isoformat(timespec="seconds")


def future_date():
    """Return a due date safely in the future."""
    return (
        datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(days=2)
    ).isoformat(timespec="seconds")


def test_work_orders_summary_counts_open_and_overdue(test_env):
    client, db_path, _ = test_env

    insert_work_orders(
        db_path,
        [
            ("OPEN", "technician1", past_date()),
            ("ASSIGNED", "technician1", future_date()),
            ("ACCEPTED", "technician1", past_date()),
            ("IN_PROGRESS", "technician1", future_date()),
            ("PENDING_VERIFICATION", "technician1", past_date()),
            ("CLOSED", "technician1", past_date()),
            ("CANCELLED", "technician1", past_date()),
        ],
    )

    response = client.get("/api/work-orders/summary")

    assert response.status_code == 200
    assert response.get_json() == {
        "open_work_orders": 5,
        "overdue_work_orders": 3,
    }


def test_work_orders_summary_returns_zero_when_empty(test_env):
    client, _, _ = test_env

    response = client.get("/api/work-orders/summary")

    assert response.status_code == 200
    assert response.get_json() == {
        "open_work_orders": 0,
        "overdue_work_orders": 0,
    }


def test_work_orders_summary_returns_500_on_database_error(
    test_env,
):
    client, _, monkeypatch = test_env

    def raise_database_error():
        raise sqlite3.OperationalError("Test database error")

    monkeypatch.setattr(
        app_module,
        "get_db",
        raise_database_error,
    )

    response = client.get("/api/work-orders/summary")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Test database error",
    }


def test_my_work_orders_summary_counts_assigned_orders(test_env):
    client, db_path, _ = test_env

    insert_work_orders(
        db_path,
        [
            ("OPEN", "technician1", past_date()),
            ("ASSIGNED", "technician1", future_date()),
            ("ACCEPTED", "technician1", future_date()),
            ("IN_PROGRESS", "technician1", past_date()),
            ("PENDING_VERIFICATION", "technician1", past_date()),
            ("CLOSED", "technician1", past_date()),
            ("CANCELLED", "technician1", past_date()),
            # Another technician's order must not be counted.
            ("OPEN", "technician2", past_date()),
        ],
    )

    response = client.get("/api/work-orders/my-summary")

    assert response.status_code == 200
    assert response.get_json() == {
        "my_pending": 5,
        "my_awaiting_verification": 1,
        "my_overdue": 3,
    }


def test_my_work_orders_summary_returns_zero_when_no_orders(
    test_env,
):
    client, db_path, _ = test_env

    insert_work_orders(
        db_path,
        [
            ("OPEN", "technician2", past_date()),
            ("CLOSED", "technician2", past_date()),
        ],
    )

    response = client.get("/api/work-orders/my-summary")

    assert response.status_code == 200
    assert response.get_json() == {
        "my_pending": 0,
        "my_awaiting_verification": 0,
        "my_overdue": 0,
    }


def test_my_work_orders_summary_returns_403_for_admin(
    test_env,
):
    client, _, monkeypatch = test_env

    # Make Flask-Login load an administrator for this request.
    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            FakeAdmin() if user_id == "1" else None
        ),
    )

    response = client.get("/api/work-orders/my-summary")

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "Only available to technicians",
    }
