
import pytest
import app as app_module


def test_machine_id_for_record_returns_expected_id():
    result = app_module.machine_id_for_record(1)

    assert result == "MCH-002"


def test_machine_id_for_record_wraps_after_num_machines():
    result = app_module.machine_id_for_record(
        app_module.NUM_MACHINES + 1
    )

    assert result == "MCH-002"


def test_asset_id_for_machine_returns_expected_id():
    result = app_module.asset_id_for_machine("MCH-001")

    assert result == "AST-1001"


@pytest.mark.parametrize(
    "machine_id",
    [
        "MCH-ABC",  # ValueError
        "MCH-",     # ValueError
        "INVALID",  # IndexError
    ],
)
def test_asset_id_for_machine_returns_none_for_invalid_id(machine_id):
    result = app_module.asset_id_for_machine(machine_id)

    assert result is None
