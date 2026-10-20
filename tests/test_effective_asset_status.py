
import sqlite3

import pytest
import app as app_module


class FakeCursor:
    def __init__(self, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(
        self,
        downtime=None,
        work_order=None,
        schedules=None,
        fail_on=None,
    ):
        self.downtime = downtime
        self.work_order = work_order
        self.schedules = schedules or []
        self.fail_on = fail_on

    def execute(self, sql, params=()):
        if self.fail_on and self.fail_on in sql:
            raise sqlite3.OperationalError("Simulated database error")

        if "FROM downtime_events" in sql:
            return FakeCursor(one=self.downtime)

        if "FROM work_orders" in sql:
            return FakeCursor(one=self.work_order)

        if "FROM pm_schedules" in sql:
            return FakeCursor(rows=self.schedules)

        raise AssertionError(f"Unexpected SQL: {sql}")


@pytest.fixture
def asset():
    return {"machine_id": "M001", "status": "RUNNING"}


def test_effective_status_returns_down_when_downtime_exists(asset):
    conn = FakeConnection(downtime=(1,))

    result = app_module._effective_asset_status(conn, asset)

    assert result == "DOWN"


def test_effective_status_returns_maintenance_when_work_order_active(asset):
    conn = FakeConnection(work_order=(1,))

    result = app_module._effective_asset_status(conn, asset)

    assert result == "MAINTENANCE"


def test_effective_status_returns_warning_when_pm_overdue(
    asset, monkeypatch
):
    conn = FakeConnection(schedules=[{"schedule_id": 1}])

    monkeypatch.setattr(
        app_module,
        "compute_pm_status",
        lambda schedule: ("OVERDUE", None),
    )

    result = app_module._effective_asset_status(conn, asset)

    assert result == "WARNING"


def test_effective_status_returns_running_when_no_issues(
    asset, monkeypatch
):
    conn = FakeConnection(schedules=[{"schedule_id": 1}])

    monkeypatch.setattr(
        app_module,
        "compute_pm_status",
        lambda schedule: ("UPCOMING", None),
    )

    result = app_module._effective_asset_status(conn, asset)

    assert result == "RUNNING"


def test_effective_status_prioritises_downtime_over_work_order(asset):
    conn = FakeConnection(
        downtime=(1,),
        work_order=(1,),
    )

    result = app_module._effective_asset_status(conn, asset)

    assert result == "DOWN"


def test_effective_status_prioritises_work_order_over_overdue_pm(
    asset, monkeypatch
):
    conn = FakeConnection(
        work_order=(1,),
        schedules=[{"schedule_id": 1}],
    )

    monkeypatch.setattr(
        app_module,
        "compute_pm_status",
        lambda schedule: ("OVERDUE", None),
    )

    result = app_module._effective_asset_status(conn, asset)

    assert result == "MAINTENANCE"


def test_effective_status_returns_existing_status_on_database_error(asset):
    conn = FakeConnection(fail_on="downtime_events")

    result = app_module._effective_asset_status(conn, asset)

    assert result == "RUNNING"


def test_effective_status_falls_back_to_running_when_status_is_empty(asset):
    asset["status"] = None
    conn = FakeConnection(fail_on="downtime_events")

    result = app_module._effective_asset_status(conn, asset)

    assert result == "RUNNING"
