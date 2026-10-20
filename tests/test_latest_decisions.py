
import sqlite3
from unittest.mock import MagicMock, patch

import app as app_module


def test_latest_decisions_by_machine_returns_mapping():
    conn = MagicMock()
    rows = [
        {"record_id": 20, "risk_score": 80},
        {"record_id": 21, "risk_score": 40},
    ]
    conn.execute.return_value.fetchall.return_value = rows

    with patch.object(
        app_module,
        "NUM_MACHINES",
        20,
    ), patch.object(
        app_module,
        "machine_id_for_record",
        side_effect=lambda record_id: f"M{record_id}",
    ):
        result = app_module._latest_decisions_by_machine(conn)

    assert result == {
        "M20": rows[0],
        "M21": rows[1],
    }


def test_latest_decisions_by_machine_empty_records():
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []

    result = app_module._latest_decisions_by_machine(conn)

    assert result == {}


def test_latest_decisions_by_machine_operational_error():
    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError(
        "no such table: decision_log"
    )

    result = app_module._latest_decisions_by_machine(conn)

    assert result == {}
