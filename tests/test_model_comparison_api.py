import sqlite3

import pandas as pd
import pytest
import app as app_module
from flask_login import UserMixin


class MockUser(UserMixin):
    id = "1"
    username = "test_user"

    def __init__(self, is_admin=False):
        self.is_admin = is_admin


@pytest.fixture
def model_comparison_env(tmp_path, monkeypatch):
    """Set up isolated SQLite data and mocked model metrics."""
    db_path = tmp_path / "test_model_comparison.db"

    conn = sqlite3.connect(db_path)

    try:
        conn.execute("""
            CREATE TABLE predictions (
                record_id INTEGER PRIMARY KEY,
                pred_prob_lr REAL,
                pred_prob_rf REAL,
                model_agreement INTEGER,
                actual_failure INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                risk_level TEXT,
                risk_score REAL,
                action TEXT,
                shap_explanation TEXT
            )
        """)

        prediction_rows = [
            # record_id, LR probability, RF probability,
            # model agreement, actual failure
            (1, 0.80, 0.90, 1, 1),
            (2, 0.20, 0.60, 0, 0),
            (3, 0.40, 0.30, 0, 1),
            (4, 0.70, 0.10, 0, 0),
        ]

        decision_rows = [
            (1, "HIGH", 90, "Inspect", "High failure risk"),
            (2, "MEDIUM", 60, "Monitor", "Model disagreement"),
            (3, "HIGH", 85, "Inspect", "Possible failure"),
            (4, "LOW", 20, "Continue", "Low failure risk"),
        ]

        conn.executemany(
            """
            INSERT INTO predictions
                (record_id, pred_prob_lr, pred_prob_rf,
                 model_agreement, actual_failure)
            VALUES (?, ?, ?, ?, ?)
            """,
            prediction_rows,
        )

        conn.executemany(
            """
            INSERT INTO decision_log
                (record_id, risk_level, risk_score,
                 action, shap_explanation)
            VALUES (?, ?, ?, ?, ?)
            """,
            decision_rows,
        )

        conn.commit()
    finally:
        conn.close()

    def connect_test_db():
        return sqlite3.connect(db_path)

    monkeypatch.setattr(app_module, "get_db", connect_test_db)

    # Mock the CSV file so the test does not depend on a real file.
    metrics_data = [
        {
            "model": "Logistic Regression",
            "accuracy": 0.97,
            "f1_score": 0.67,
        },
        {
            "model": "Random Forest",
            "accuracy": 0.99,
            "f1_score": 0.90,
        },
    ]

    monkeypatch.setattr(
        app_module.pd,
        "read_csv",
        lambda *args, **kwargs: pd.DataFrame(metrics_data),
    )

    # Start each test with an empty response cache.
    monkeypatch.setattr(
        app_module,
        "_cached_response",
        lambda key: None,
    )

    # Preserve the payload while avoiding dependence on cache internals.
    monkeypatch.setattr(
        app_module,
        "_store_cached_response",
        lambda key, payload: payload,
    )

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: (
            MockUser() if user_id == "1" else None
        ),
    )

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, db_path


# --------------------------------------------------
# Successful API response
# --------------------------------------------------

def test_model_comparison_returns_expected_metrics(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")

    assert response.status_code == 200

    data = response.get_json()

    assert data["total_records"] == 4
    assert len(data["metrics"]) == 2
    assert data["threshold"] == 0.5

    assert data["metrics"][0]["model"] == "Logistic Regression"
    assert data["metrics"][1]["model"] == "Random Forest"


def test_model_comparison_calculates_agreement_percentages(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    assert data["agreement_count"] == 1
    assert data["disagreement_count"] == 3
    assert data["agreement_pct"] == "25.0%"
    assert data["disagreement_pct"] == "75.0%"


def test_model_comparison_counts_different_model_predictions(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    # RF predicts failure while LR predicts normal for record 2.
    assert data["rf_failure_lr_normal"] == 1

    # LR predicts failure while RF predicts normal for record 4.
    assert data["rf_normal_lr_failure"] == 1


def test_model_comparison_calculates_false_negatives_and_positives(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    assert data["lr_false_negatives"] == 1
    assert data["rf_false_negatives"] == 1

    assert data["lr_false_positives"] == 1
    assert data["rf_false_positives"] == 1


def test_model_comparison_calculates_lr_confusion_matrix(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    assert data["lr_confusion"] == {
        "tp": 1,
        "tn": 1,
        "fp": 1,
        "fn": 1,
    }


def test_model_comparison_calculates_rf_confusion_matrix(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    assert data["rf_confusion"] == {
        "tp": 1,
        "tn": 1,
        "fp": 1,
        "fn": 1,
    }


# --------------------------------------------------
# Model disagreement records
# --------------------------------------------------

def test_model_comparison_sorts_disagreements_by_probability_gap(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    disagreements = data["disagreements"]

    # Absolute probability gaps:
    # record 4: 0.60
    # record 2: 0.40
    # record 3: 0.10
    assert [row["record_id"] for row in disagreements] == [4, 2, 3]

    assert [
        row["prob_gap_pct"] for row in disagreements
    ] == [60.0, 40.0, 10.0]


def test_model_comparison_includes_disagreement_details(
    model_comparison_env,
):
    client, _ = model_comparison_env

    response = client.get("/api/model-comparison")
    data = response.get_json()

    record = data["disagreements"][0]

    assert record["record_id"] == 4
    assert record["pred_prob_lr"] == pytest.approx(0.70)
    assert record["pred_prob_rf"] == pytest.approx(0.10)
    assert record["lr_pred"] == 1
    assert record["rf_pred"] == 0
    assert record["actual_failure"] == 0
    assert record["risk_level"] == "LOW"
    assert record["risk_score"] == 20
    assert record["action"] == "Continue"
    assert record["shap_explanation"] == "Low failure risk"
    assert record["prob_gap_pct"] == 60.0


def test_model_comparison_limits_disagreements_to_20(
    model_comparison_env,
):
    client, db_path = model_comparison_env

    conn = sqlite3.connect(db_path)

    try:
        # Add 25 records with model disagreements.
        for record_id in range(5, 30):
            conn.execute(
                """
                INSERT INTO predictions
                    (record_id, pred_prob_lr, pred_prob_rf,
                     model_agreement, actual_failure)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_id, 0.10, 0.90, 0, 0),
            )

            conn.execute(
                """
                INSERT INTO decision_log
                    (record_id, risk_level, risk_score,
                     action, shap_explanation)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_id, "MEDIUM", 50, "Monitor", "Test record"),
            )

        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/model-comparison")
    data = response.get_json()

    assert data["total_records"] == 29
    assert len(data["disagreements"]) == 20


# --------------------------------------------------
# Empty dataset
# --------------------------------------------------

def test_model_comparison_handles_empty_dataset(
    model_comparison_env,
):
    client, db_path = model_comparison_env

    conn = sqlite3.connect(db_path)

    try:
        conn.execute("DELETE FROM predictions")
        conn.execute("DELETE FROM decision_log")
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/model-comparison")

    assert response.status_code == 200

    data = response.get_json()

    assert data["total_records"] == 0
    assert data["agreement_count"] == 0
    assert data["disagreement_count"] == 0
    assert data["agreement_pct"] == "0%"
    assert data["disagreement_pct"] == "0%"
    assert data["disagreements"] == []

    assert data["lr_confusion"] == {
        "tp": 0,
        "tn": 0,
        "fp": 0,
        "fn": 0,
    }

    assert data["rf_confusion"] == {
        "tp": 0,
        "tn": 0,
        "fp": 0,
        "fn": 0,
    }


# --------------------------------------------------
# Response cache
# --------------------------------------------------

def test_model_comparison_returns_cached_response(
    model_comparison_env, monkeypatch
):
    client, _ = model_comparison_env

    cached_payload = {
        "total_records": 123,
        "source": "cache",
    }

    monkeypatch.setattr(
        app_module,
        "_cached_response",
        lambda key: (
            cached_payload if key == "model-comparison" else None
        ),
    )

    def unexpected_read_csv(*args, **kwargs):
        pytest.fail("CSV should not be read when the cache is hit")

    monkeypatch.setattr(
        app_module.pd,
        "read_csv",
        unexpected_read_csv,
    )

    response = client.get("/api/model-comparison")

    assert response.status_code == 200
    assert response.get_json() == cached_payload


def test_model_comparison_stores_fresh_response_in_cache(
    model_comparison_env, monkeypatch
):
    client, _ = model_comparison_env

    stored = {}

    def store_response(key, payload):
        stored["key"] = key
        stored["payload"] = payload
        return payload

    monkeypatch.setattr(
        app_module,
        "_store_cached_response",
        store_response,
    )

    response = client.get("/api/model-comparison")

    assert response.status_code == 200
    assert stored["key"] == "model-comparison"
    assert stored["payload"]["total_records"] == 4
    assert stored["payload"]["lr_confusion"]["tp"] == 1


# --------------------------------------------------
# Error handling
# --------------------------------------------------

def test_model_comparison_returns_500_when_csv_read_fails(
    model_comparison_env, monkeypatch
):
    client, _ = model_comparison_env

    def fail_read_csv(*args, **kwargs):
        raise RuntimeError("metrics CSV unavailable")

    monkeypatch.setattr(
        app_module.pd,
        "read_csv",
        fail_read_csv,
    )

    response = client.get("/api/model-comparison")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "metrics CSV unavailable"
    }


def test_model_comparison_returns_500_when_database_fails(
    model_comparison_env, monkeypatch
):
    client, _ = model_comparison_env

    def fail_get_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        app_module,
        "get_db",
        fail_get_db,
    )

    response = client.get("/api/model-comparison")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "database unavailable"
    }
