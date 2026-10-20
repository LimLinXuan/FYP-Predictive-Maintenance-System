import json
import sqlite3

import pytest
import app as app_module
from flask_login import UserMixin


class MockUser(UserMixin):
    id = "1"
    username = "test_tech"

    def __init__(self, is_admin=False):
        self.is_admin = is_admin


@pytest.fixture
def shap_api_env(tmp_path, monkeypatch):
    """Create an isolated database for SHAP and explainability API tests."""
    db_path = tmp_path / "test_shap_api.db"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            machine_id TEXT,
            risk_score REAL,
            risk_level TEXT,
            pred_prob REAL,
            action TEXT
        );

        CREATE TABLE shap_values (
            record_id INTEGER PRIMARY KEY,
            top_feature TEXT,
            top_shap_value REAL,
            tool_wear REAL,
            torque REAL,
            temperature REAL
        );

        CREATE TABLE predictions (
            record_id INTEGER PRIMARY KEY,
            actual_failure INTEGER,
            pred_prob_lr REAL,
            pred_prob_rf REAL
        );
    """)

    conn.executemany("""
        INSERT INTO decision_log (
            record_id, machine_id, risk_score, risk_level, pred_prob, action
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        (101, "MCH-001", 65.0, "HIGH", 0.82, "Inspect machine"),
        (102, "MCH-002", 90.0, "CRITICAL", 0.97, "Stop and inspect"),
        (103, "MCH-003", 45.0, "MEDIUM", 0.60, "Monitor machine"),
    ])

    conn.execute("""
        INSERT INTO shap_values (
            record_id, top_feature, top_shap_value,
            tool_wear, torque, temperature
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (102, "tool_wear", 0.75, 0.75, -0.20, 0.10))

    conn.executemany("""
        INSERT INTO predictions (
            record_id, actual_failure, pred_prob_lr, pred_prob_rf
        )
        VALUES (?, ?, ?, ?)
    """, [
        (1, 0, 0.10, 0.05),
        (2, 0, 0.20, 0.15),
        (3, 1, 0.80, 0.90),
        (4, 1, 0.95, 0.98),
    ])

    conn.commit()
    conn.close()

    def get_test_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(app_module, "get_db", get_test_db)

    monkeypatch.setattr(
        app_module.login_manager,
        "_user_callback",
        lambda user_id: MockUser() if user_id == "1" else None,
    )

    app_module.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret-key",
    )

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = "1"
            session["_fresh"] = True

        yield client, get_test_db, db_path, tmp_path


# -------------------------------------------------------------------
# GET /api/shap/global
# -------------------------------------------------------------------

def test_shap_global_returns_json_data(shap_api_env, monkeypatch):
    client, _, _, tmp_path = shap_api_env

    shap_file = tmp_path / "shap_importance.json"
    expected = {
        "features": [
            {"feature": "tool_wear", "importance": 0.45},
            {"feature": "torque", "importance": 0.30},
        ]
    }
    shap_file.write_text(json.dumps(expected), encoding="utf-8")

    monkeypatch.setattr(app_module, "SHAP_IMPORTANCE_JSON", str(shap_file))

    response = client.get("/api/shap/global")

    assert response.status_code == 200
    assert response.get_json() == expected


def test_shap_global_returns_404_when_file_does_not_exist(
    shap_api_env, monkeypatch
):
    client, _, _, tmp_path = shap_api_env

    missing_file = tmp_path / "missing_shap_importance.json"
    monkeypatch.setattr(
        app_module, "SHAP_IMPORTANCE_JSON", str(missing_file)
    )

    response = client.get("/api/shap/global")

    assert response.status_code == 404
    assert "SHAP importance data not available" in response.get_json()["error"]


def test_shap_global_returns_500_when_json_is_invalid(
    shap_api_env, monkeypatch
):
    client, _, _, tmp_path = shap_api_env

    shap_file = tmp_path / "invalid_shap_importance.json"
    shap_file.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(app_module, "SHAP_IMPORTANCE_JSON", str(shap_file))

    response = client.get("/api/shap/global")

    assert response.status_code == 500
    assert "error" in response.get_json()


# -------------------------------------------------------------------
# GET /api/shap/highest-risk
# -------------------------------------------------------------------

def test_shap_highest_risk_returns_highest_risk_record(shap_api_env):
    client, _, _, _ = shap_api_env

    response = client.get("/api/shap/highest-risk")

    assert response.status_code == 200
    data = response.get_json()

    assert data["record_id"] == 102
    assert data["risk_score"] == 90.0
    assert data["risk_level"] == "CRITICAL"
    assert data["pred_prob"] == 0.97
    assert data["action"] == "Stop and inspect"


def test_shap_highest_risk_sorts_features_by_absolute_value(shap_api_env):
    client, _, _, _ = shap_api_env

    response = client.get("/api/shap/highest-risk")

    assert response.status_code == 200
    features = response.get_json()["features"]

    assert [item["feature"] for item in features] == [
        "tool_wear",
        "torque",
        "temperature",
    ]
    assert [item["shap_value"] for item in features] == [
        0.75,
        -0.2,
        0.1,
    ]


def test_shap_highest_risk_returns_404_when_no_records_exist(
    shap_api_env, monkeypatch
):
    client, _, _, _ = shap_api_env

    def empty_database():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE decision_log (
                record_id INTEGER PRIMARY KEY,
                machine_id TEXT,
                risk_score REAL,
                risk_level TEXT,
                pred_prob REAL,
                action TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE shap_values (
                record_id INTEGER PRIMARY KEY,
                top_feature TEXT,
                top_shap_value REAL
            )
        """)
        return conn

    monkeypatch.setattr(app_module, "get_db", empty_database)

    response = client.get("/api/shap/highest-risk")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No records found"


def test_shap_highest_risk_returns_404_when_shap_data_is_missing(
    shap_api_env, monkeypatch
):
    client, get_test_db, _, _ = shap_api_env

    conn = get_test_db()
    conn.execute("DELETE FROM shap_values WHERE record_id=102")
    conn.commit()
    conn.close()

    response = client.get("/api/shap/highest-risk")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No SHAP data for this record"


def test_shap_highest_risk_returns_500_on_database_error(
    shap_api_env, monkeypatch
):
    client, _, _, _ = shap_api_env

    def raise_db_error():
        raise sqlite3.OperationalError("test SHAP database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/shap/highest-risk")

    assert response.status_code == 500
    assert "test SHAP database error" in response.get_json()["error"]


# -------------------------------------------------------------------
# GET /api/explainability
# -------------------------------------------------------------------

def test_explainability_returns_lr_rf_roc_data(shap_api_env):
    client, _, _, _ = shap_api_env

    response = client.get("/api/explainability")

    assert response.status_code == 200
    data = response.get_json()

    assert data["actual"] == [0, 0, 1, 1]
    assert data["total_records"] == 4
    assert "lr" in data
    assert "rf" in data

    for model_name in ("lr", "rf"):
        model_data = data[model_name]

        assert "fpr" in model_data
        assert "tpr" in model_data
        assert "thresholds" in model_data
        assert "auc" in model_data
        assert "probs" in model_data

        assert len(model_data["fpr"]) == len(model_data["tpr"])
        assert len(model_data["fpr"]) == len(model_data["thresholds"])
        assert len(model_data["probs"]) == 4
        assert 0 <= model_data["auc"] <= 1


def test_explainability_returns_auc_for_both_models(shap_api_env):
    client, _, _, _ = shap_api_env

    response = client.get("/api/explainability")

    assert response.status_code == 200
    data = response.get_json()

    assert data["lr"]["auc"] == 1.0
    assert data["rf"]["auc"] == 1.0


def test_explainability_returns_500_on_database_error(
    shap_api_env, monkeypatch
):
    client, _, _, _ = shap_api_env

    def raise_db_error():
        raise sqlite3.OperationalError("test explainability database error")

    monkeypatch.setattr(app_module, "get_db", raise_db_error)

    response = client.get("/api/explainability")

    assert response.status_code == 500
    assert "test explainability database error" in response.get_json()["error"]


def test_explainability_returns_500_when_predictions_table_is_missing(
    shap_api_env, monkeypatch
):
    client, _, _, _ = shap_api_env

    def database_without_predictions():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(
        app_module, "get_db", database_without_predictions
    )

    response = client.get("/api/explainability")

    assert response.status_code == 500
    assert "error" in response.get_json()
