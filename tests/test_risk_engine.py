
import pytest

from risk_engine import compute_risk_score


def test_no_risk_signals_returns_low_risk():
    result = compute_risk_score(0.10, {})

    assert result["risk_score"] == 5
    assert result["risk_level"] == "Low"
    assert result["action"] == "Normal operation"
    assert result["override_applied"] is False
    assert result["reasons"] == "No risk signals"


@pytest.mark.parametrize(
    ("prob", "expected_floor"),
    [
        (0.95, 90),
        (0.90, 90),
        (0.85, 80),
        (0.80, 80),
        (0.75, 70),
        (0.70, 70),
        (0.60, 50),
        (0.50, 50),
    ],
)
def test_probability_safety_floor(prob, expected_floor):
    result = compute_risk_score(prob, {})

    assert result["risk_score"] >= expected_floor
    assert result["override_applied"] is True
    assert "safety floor applied" in result["reasons"]


def test_probability_below_50_percent_has_no_floor():
    result = compute_risk_score(0.40, {})

    assert result["risk_score"] == 20
    assert result["risk_level"] == "Low"
    assert result["override_applied"] is False


def test_domain_and_anomaly_signals_are_added():
    row = {
        "hdf_risk": 1,
        "pwf_risk": 1,
        "osf_risk": 1,
        "isoforest_anomaly": 1,
        "zscore_anomaly": 1,
    }

    result = compute_risk_score(0.10, row)

    # ML 5 + domain rules 40 + anomaly signals 20
    assert result["risk_score"] == 65
    assert result["risk_level"] == "High"
    assert result["override_applied"] is False


def test_high_probability_can_reach_critical():
    row = {
        "hdf_risk": 1,
        "pwf_risk": 1,
        "osf_risk": 1,
        "isoforest_anomaly": 1,
        "zscore_anomaly": 1,
    }

    result = compute_risk_score(0.69, row)

    assert result["risk_score"] == 94
    assert result["risk_level"] == "Critical"
    assert result["action"] == "Immediate shutdown inspection required"


def test_result_contains_expected_fields():
    result = compute_risk_score(0.20, {})

    assert {
        "risk_score",
        "risk_level",
        "action",
        "reasons",
        "prob",
        "override_applied",
        "timestamp",
    }.issubset(result.keys())


def test_probability_is_rounded_in_result():
    result = compute_risk_score(0.45678, {})

    assert result["prob"] == 0.4568
