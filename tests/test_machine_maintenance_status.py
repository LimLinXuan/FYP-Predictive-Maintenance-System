
import sqlite3
from unittest.mock import MagicMock, patch

import app as app_module


def make_asset(status="RUNNING", machine_id="M001"):
    return {
        "status": status,
        "machine_id": machine_id,
    }


def test_machine_maintenance_status_down():
    """DOWN status should take priority over PM status."""
    conn = MagicMock()

    result = app_module._machine_maintenance_status(
        conn, make_asset(status="DOWN")
    )

    assert result == "DOWN"
    conn.execute.assert_not_called()


def test_machine_maintenance_status_maintenance():
    """MAINTENANCE should return IN_PROGRESS."""
    conn = MagicMock()

    result = app_module._machine_maintenance_status(
        conn, make_asset(status="MAINTENANCE")
    )

    assert result == "IN_PROGRESS"
    conn.execute.assert_not_called()


def test_machine_maintenance_status_overdue():
    """An overdue PM schedule should return OVERDUE."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        {"schedule_id": 1}
    ]

    with patch.object(
        app_module,
        "compute_pm_status",
        return_value=("OVERDUE", None),
    ):
        result = app_module._machine_maintenance_status(
            conn, make_asset()
        )

    assert result == "OVERDUE"


def test_machine_maintenance_status_due_soon():
    """A due-soon PM schedule should return DUE_SOON."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        {"schedule_id": 1}
    ]

    with patch.object(
        app_module,
        "compute_pm_status",
        return_value=("DUE_SOON", None),
    ):
        result = app_module._machine_maintenance_status(
            conn, make_asset()
        )

    assert result == "DUE_SOON"


def test_overdue_takes_priority_over_due_soon():
    """OVERDUE should take priority if both statuses exist."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        {"schedule_id": 1},
        {"schedule_id": 2},
    ]

    with patch.object(
        app_module,
        "compute_pm_status",
        side_effect=[
            ("DUE_SOON", None),
            ("OVERDUE", None),
        ],
    ):
        result = app_module._machine_maintenance_status(
            conn, make_asset()
        )

    assert result == "OVERDUE"


def test_machine_maintenance_status_on_schedule():
    """No overdue or due-soon schedules should return ON_SCHEDULE."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        {"schedule_id": 1}
    ]

    with patch.object(
        app_module,
        "compute_pm_status",
        return_value=("ON_SCHEDULE", None),
    ):
        result = app_module._machine_maintenance_status(
            conn, make_asset()
        )

    assert result == "ON_SCHEDULE"


def test_machine_maintenance_status_no_schedules():
    """No PM schedules should return ON_SCHEDULE."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []

    result = app_module._machine_maintenance_status(
        conn, make_asset()
    )

    assert result == "ON_SCHEDULE"


def test_machine_maintenance_status_database_error():
    """A database OperationalError should be handled gracefully."""
    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError(
        "no such table: pm_schedules"
    )

    result = app_module._machine_maintenance_status(
        conn, make_asset()
    )

    assert result == "ON_SCHEDULE"


def test_machine_maintenance_status_invalid_schedule():
    """A handled ValueError from PM status computation should not crash."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        {"schedule_id": 1}
    ]

    with patch.object(
        app_module,
        "compute_pm_status",
        side_effect=ValueError("invalid schedule"),
    ):
        result = app_module._machine_maintenance_status(
            conn, make_asset()
        )

    assert result == "ON_SCHEDULE"
