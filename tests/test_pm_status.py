
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module


def make_pm(
    frequency_type="DAYS",
    next_due_at=None,
    machine_id="MCH-001",
    last_performed_tool_wear=0,
    frequency_value=100,
):
    """Create an isolated sqlite3.Row for PM unit tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    pm = conn.execute(
        """
        SELECT
            ? AS frequency_type,
            ? AS next_due_at,
            ? AS machine_id,
            ? AS last_performed_tool_wear,
            ? AS frequency_value
        """,
        (
            frequency_type,
            next_due_at,
            machine_id,
            last_performed_tool_wear,
            frequency_value,
        ),
    ).fetchone()

    # The returned Row remains readable after closing the connection.
    conn.close()
    return pm


# DAYS-based schedule tests

def test_pm_overdue():
    due_date = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).isoformat()
    pm = make_pm(next_due_at=due_date)

    status, info = app_module.compute_pm_status(pm)

    assert status == "OVERDUE"
    assert info["days_overdue"] >= 1


def test_pm_due_soon():
    due_date = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)).isoformat()
    pm = make_pm(next_due_at=due_date)

    status, info = app_module.compute_pm_status(pm)

    assert status == "DUE_SOON"
    assert "days_remaining" in info


def test_pm_on_schedule():
    due_date = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)).isoformat()
    pm = make_pm(next_due_at=due_date)

    status, info = app_module.compute_pm_status(pm)

    assert status == "ON_SCHEDULE"
    assert info["days_remaining"] > 7


def test_pm_missing_due_date():
    pm = make_pm(next_due_at=None)

    status, info = app_module.compute_pm_status(pm)

    assert status == "ON_SCHEDULE"
    assert info == {}


# TOOL_WEAR-based schedule tests

def test_tool_wear_pm_overdue(monkeypatch):
    pm = make_pm(
        frequency_type="TOOL_WEAR",
        last_performed_tool_wear=20,
        frequency_value=100,
    )
    monkeypatch.setattr(
        app_module, "get_latest_tool_wear", lambda machine_id: 125
    )

    status, info = app_module.compute_pm_status(pm)

    assert status == "OVERDUE"
    assert info["wear_over"] == 5


def test_tool_wear_pm_due_soon(monkeypatch):
    pm = make_pm(
        frequency_type="TOOL_WEAR",
        last_performed_tool_wear=20,
        frequency_value=100,
    )
    monkeypatch.setattr(
        app_module, "get_latest_tool_wear", lambda machine_id: 105
    )

    status, info = app_module.compute_pm_status(pm)

    assert status == "DUE_SOON"
    assert info["wear_remaining"] == 15


def test_tool_wear_pm_on_schedule(monkeypatch):
    pm = make_pm(
        frequency_type="TOOL_WEAR",
        last_performed_tool_wear=20,
        frequency_value=100,
    )
    monkeypatch.setattr(
        app_module, "get_latest_tool_wear", lambda machine_id: 40
    )

    status, info = app_module.compute_pm_status(pm)

    assert status == "ON_SCHEDULE"
    assert info["wear_remaining"] == 80


@pytest.mark.parametrize("current_wear, baseline", [
    (None, 20),
    (50, None),
])
def test_tool_wear_missing_data(
    monkeypatch, current_wear, baseline
):
    pm = make_pm(
        frequency_type="TOOL_WEAR",
        last_performed_tool_wear=baseline,
        frequency_value=100,
    )
    monkeypatch.setattr(
        app_module, "get_latest_tool_wear",
        lambda machine_id: current_wear,
    )

    status, info = app_module.compute_pm_status(pm)

    assert status == "ON_SCHEDULE"
    assert info == {}
