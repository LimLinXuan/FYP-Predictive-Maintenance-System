
import sqlite3
from unittest.mock import MagicMock, patch

import app as app_module


def make_asset(status="RUNNING", machine_id="M001"):
    return {
        "asset_id": 1,
        "machine_id": machine_id,
        "status": status,
    }


def make_conn(open_work_orders=2):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {
        "c": open_work_orders
    }
    return conn


def test_machine_snapshot_with_prediction_data():
    """Prediction fields should be converted and included in the snapshot."""
    conn = make_conn(open_work_orders=3)
    asset = make_asset()

    decision = {
        "risk_level": "HIGH",
        "risk_score": "85.6",
        "pred_prob": "0.87654",
        "timestamp": "2026-10-21 10:00:00",
        "air_temp": 300.5,
        "process_temp": 310.2,
        "rpm": 1500,
        "torque": 40.5,
        "tool_wear": 120,
    }

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="RUNNING",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="ON_SCHEDULE",
    ):
        result = app_module._machine_snapshot(
            conn, asset, {"M001": decision}
        )

    assert result["risk_level"] == "HIGH"
    assert result["risk_score"] == 86
    assert result["pred_prob"] == 0.877
    assert result["prediction_timestamp"] == decision["timestamp"]
    assert result["decision_timestamp"] == decision["timestamp"]
    assert result["air_temp"] == 300.5
    assert result["process_temp"] == 310.2
    assert result["rpm"] == 1500
    assert result["torque"] == 40.5
    assert result["tool_wear"] == 120
    assert result["open_work_orders"] == 3
    assert result["status"] == "RUNNING"
    assert result["maintenance_status"] == "ON_SCHEDULE"


def test_machine_snapshot_without_prediction():
    """Missing prediction should produce None values for prediction fields."""
    conn = make_conn(open_work_orders=0)
    asset = make_asset()

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="RUNNING",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="ON_SCHEDULE",
    ):
        result = app_module._machine_snapshot(conn, asset, {})

    assert result["risk_level"] is None
    assert result["risk_score"] is None
    assert result["pred_prob"] is None
    assert result["prediction_timestamp"] is None
    assert result["decision_timestamp"] is None
    assert result["open_work_orders"] == 0


def test_machine_snapshot_invalid_numeric_values():
    """Invalid risk score and probability should become None."""
    conn = make_conn()
    asset = make_asset()
    decision = {
        "risk_score": "invalid",
        "pred_prob": "not-a-number",
    }

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="RUNNING",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="ON_SCHEDULE",
    ):
        result = app_module._machine_snapshot(
            conn, asset, {"M001": decision}
        )

    assert result["risk_score"] is None
    assert result["pred_prob"] is None


def test_machine_snapshot_none_numeric_values():
    """Missing numeric values should remain None."""
    conn = make_conn()
    asset = make_asset()
    decision = {
        "risk_score": None,
        "pred_prob": None,
    }

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="RUNNING",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="ON_SCHEDULE",
    ):
        result = app_module._machine_snapshot(
            conn, asset, {"M001": decision}
        )

    assert result["risk_score"] is None
    assert result["pred_prob"] is None


def test_machine_snapshot_work_order_database_error():
    """Missing work_orders table should default the count to zero."""
    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError(
        "no such table: work_orders"
    )
    asset = make_asset()

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="RUNNING",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="ON_SCHEDULE",
    ):
        result = app_module._machine_snapshot(conn, asset, {})

    assert result["open_work_orders"] == 0


def test_machine_snapshot_uses_effective_status():
    """The effective asset status should replace the original status."""
    conn = make_conn()
    asset = make_asset(status="RUNNING")

    with patch.object(
        app_module,
        "_effective_asset_status",
        return_value="DOWN",
    ), patch.object(
        app_module,
        "_machine_maintenance_status",
        return_value="OVERDUE",
    ) as mock_maintenance:
        result = app_module._machine_snapshot(conn, asset, {})

    assert result["status"] == "DOWN"
    assert result["maintenance_status"] == "OVERDUE"

    # Maintenance status should receive the asset with effective status.
    passed_asset = mock_maintenance.call_args.args[1]
    assert passed_asset["status"] == "DOWN"
