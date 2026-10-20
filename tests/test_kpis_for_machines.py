
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import app as app_module


def make_downtime_row(
    start_time,
    end_time=None,
    duration_minutes=None,
    reason_category="UNPLANNED_FAILURE",
):
    return {
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": duration_minutes,
        "reason_category": reason_category,
    }


def test_kpis_calculates_duration_when_duration_is_none():
    """Calculate downtime from start and end timestamps."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    end = (now - timedelta(minutes=10)).isoformat(timespec="seconds")

    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        make_downtime_row(start, end)
    ]

    result = app_module._kpis_for_machines(conn, ["M001"])

    assert result["downtime_minutes"] == 20
    assert result["failure_count"] == 1


def test_kpis_invalid_start_time_defaults_duration_to_zero():
    """An invalid start timestamp should be handled gracefully."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        make_downtime_row(
            start_time="invalid-date",
            end_time="2026-10-20T10:00:00",
        )
    ]

    result = app_module._kpis_for_machines(conn, ["M001"])

    assert result["downtime_minutes"] == 0
    assert result["failure_count"] == 1


def test_kpis_invalid_end_time_defaults_duration_to_zero():
    """An invalid end timestamp should be handled gracefully."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        make_downtime_row(
            start_time="2026-10-20T09:00:00",
            end_time="invalid-date",
        )
    ]

    result = app_module._kpis_for_machines(conn, ["M001"])

    assert result["downtime_minutes"] == 0
    assert result["failure_count"] == 1


def test_kpis_negative_duration_is_clamped_to_zero():
    """An end time before the start time should not create negative downtime."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        make_downtime_row(
            start_time="2026-10-20T10:00:00",
            end_time="2026-10-20T09:00:00",
        )
    ]

    result = app_module._kpis_for_machines(conn, ["M001"])

    assert result["downtime_minutes"] == 0
