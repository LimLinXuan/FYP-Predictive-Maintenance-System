
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module


@pytest.fixture
def kpi_db():
    """Create an isolated database for KPI calculation tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE downtime_events (
            machine_id TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            duration_minutes INTEGER,
            reason_category TEXT
        )
    """)

    yield conn
    conn.close()


def test_kpis_return_empty_values_without_machines(kpi_db):
    result = app_module._kpis_for_machines(
        kpi_db, [], days=30
    )

    assert result == {
        "window_days": 30,
        "availability": None,
        "mtbf_hours": None,
        "mttr_minutes": None,
        "failure_count": 0,
        "downtime_minutes": 0,
    }


def test_kpis_calculate_availability_mtbf_and_mttr(kpi_db):
    start = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(days=1)
    ).isoformat(timespec="seconds")

    kpi_db.executemany(
        """
        INSERT INTO downtime_events (
            machine_id, start_time, end_time,
            duration_minutes, reason_category
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                "MCH-001", start, None, 60,
                "UNPLANNED_FAILURE",
            ),
            (
                "MCH-001", start, None, 30,
                "PLANNED_MAINTENANCE",
            ),
        ],
    )

    result = app_module._kpis_for_machines(
        kpi_db, ["MCH-001"], days=30
    )

    assert result["window_days"] == 30
    assert result["failure_count"] == 1
    assert result["downtime_minutes"] == 90
    assert result["availability"] == 99.8
    assert result["mtbf_hours"] == 718.5
    assert result["mttr_minutes"] == 60.0


def test_kpis_handle_missing_downtime_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    try:
        result = app_module._kpis_for_machines(
            conn, ["MCH-001"], days=30
        )

        assert result["availability"] == 100.0
        assert result["mtbf_hours"] is None
        assert result["mttr_minutes"] is None
        assert result["failure_count"] == 0
        assert result["downtime_minutes"] == 0
    finally:
        conn.close()


def test_failure_trend_returns_requested_number_of_days(kpi_db):
    result = app_module._failure_trend(
        kpi_db, ["MCH-001"], days=7
    )

    assert len(result) == 7
    assert all("date" in item for item in result)
    assert all("failures" in item for item in result)
    assert all(item["failures"] == 0 for item in result)


def test_failure_trend_counts_unplanned_failures(kpi_db):
    start = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(days=1)
    ).isoformat(timespec="seconds")

    kpi_db.executemany(
        """
        INSERT INTO downtime_events (
            machine_id, start_time, end_time,
            duration_minutes, reason_category
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                "MCH-001", start, None, 60,
                "UNPLANNED_FAILURE",
            ),
            (
                "MCH-001", start, None, 30,
                "PLANNED_MAINTENANCE",
            ),
            (
                "MCH-002", start, None, 45,
                "UNPLANNED_FAILURE",
            ),
        ],
    )

    result = app_module._failure_trend(
        kpi_db, ["MCH-001"], days=14
    )

    total_failures = sum(
        item["failures"] for item in result
    )

    assert len(result) == 14
    assert total_failures == 1


def test_failure_trend_returns_empty_series_without_machines(kpi_db):
    result = app_module._failure_trend(
        kpi_db, [], days=7
    )

    assert len(result) == 7
    assert all(item["failures"] == 0 for item in result)
