import sqlite3

import pytest
import app as app_module

from datetime import datetime, timezone

datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
    timespec="seconds"
)

@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Use a temporary SQLite database, never the real project database."""
    test_db = tmp_path / "test_alert_permissions.db"

    monkeypatch.setattr(app_module, "DB", str(test_db))
    monkeypatch.setattr(app_module, "_db_configured", False)

    conn = sqlite3.connect(test_db)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE decision_log (
            record_id INTEGER PRIMARY KEY,
            alert_status TEXT NOT NULL,
            assigned_to TEXT,
            resolved_by TEXT,
            resolved_at TEXT,
            resolution_note TEXT
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            type TEXT NOT NULL DEFAULT 'system',
            record_id INTEGER,
            created_at TEXT NOT NULL,
            read_at TEXT
        );

        INSERT INTO users
            (id, username, email, password_hash, role, is_active,
             must_change_password, created_at)
        VALUES
            (1, 'admin_test', 'admin@test.local', 'unused', 'admin', 1, 0, '2026-01-01'),
            (2, 'tech_test', 'tech@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (3, 'tech2_test', 'tech2@test.local', 'unused', 'technician', 1, 0, '2026-01-01'),
            (4, 'inactive_test', 'inactive@test.local', 'unused', 'technician', 0, 0, '2026-01-01');

        INSERT INTO decision_log (record_id, alert_status, assigned_to)
        VALUES
            (101, 'OPEN', NULL),
            (102, 'OPEN', 'tech_test'),
            (103, 'RESOLVED', NULL),
            (104, 'FALSE_POSITIVE', NULL);
    """)
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        yield client, test_db


def authenticate(client, user_id):
    """Authenticate through Flask-Login's test session."""
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def read_db(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def test_unauthenticated_user_cannot_assign_alert(test_env):
    client, _ = test_env

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_admin_can_assign_alert_to_active_technician(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 200
    assert response.get_json()["assigned_to"] == "tech_test"
    assert response.get_json()["alert_status"] == "ASSIGNED"

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] == "tech_test"
    assert row["alert_status"] == "ASSIGNED"


def test_technician_can_claim_unassigned_alert(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post("/api/assign/101", json={})

    assert response.status_code == 200

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] == "tech_test"
    assert row["alert_status"] == "ASSIGNED"


def test_technician_cannot_reassign_claimed_alert(test_env):
    client, db_path = test_env
    authenticate(client, 3)

    response = client.post("/api/assign/102", json={})

    assert response.status_code == 403
    assert read_db(
        db_path,
        "SELECT assigned_to FROM decision_log WHERE record_id = 102",
    )[0]["assigned_to"] == "tech_test"


@pytest.mark.parametrize("record_id", [103, 104])
def test_closed_alert_cannot_be_assigned(test_env, record_id):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        f"/api/assign/{record_id}",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 400

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = ?",
        (record_id,),
    )[0]
    assert row["assigned_to"] is None
    assert row["alert_status"] in ("RESOLVED", "FALSE_POSITIVE")


def test_nonexistent_alert_returns_404(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/999",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({}, "technician username required"),
        ({"technician": "unknown_test"}, "Invalid technician"),
        ({"technician": "inactive_test"}, "Invalid technician"),
    ],
)
def test_admin_must_select_active_technician(
    test_env, payload, expected_message
):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post("/api/assign/101", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_message

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] is None
    assert row["alert_status"] == "OPEN"


def test_assignment_creates_audit_log_and_notification(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/101",
        json={"technician": "tech_test"},
    )
    assert response.status_code == 200

    audit_rows = read_db(
        db_path,
        "SELECT username, action, target FROM audit_log",
    )
    assert any(
        row["username"] == "admin_test"
        and row["action"] == "ASSIGN_ALERT"
        and row["target"] == "Record #101"
        for row in audit_rows
    )

    notifications = read_db(
        db_path,
        "SELECT username, title, record_id FROM notifications",
    )
    assert any(
        row["username"] == "tech_test"
        and row["record_id"] == 101
        for row in notifications
    )

def test_inactive_user_cannot_assign_alert(test_env):
    client, db_path = test_env
    authenticate(client, 4)

    response = client.post(
        "/api/assign/101",
        json={},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 101",
    )[0]
    assert row["assigned_to"] is None
    assert row["alert_status"] == "OPEN"



def test_reassigning_alert_to_same_technician_updates_status_without_duplicate_assignment(
    test_env,
):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/assign/102",
        json={"technician": "tech_test"},
    )

    assert response.status_code == 200
    assert response.get_json()["assigned_to"] == "tech_test"
    assert response.get_json()["alert_status"] == "ASSIGNED"

    row = read_db(
        db_path,
        "SELECT assigned_to, alert_status FROM decision_log WHERE record_id = 102",
    )[0]
    assert row["assigned_to"] == "tech_test"
    assert row["alert_status"] == "ASSIGNED"

    audit_rows = read_db(
        db_path,
        """
        SELECT action
        FROM audit_log
        WHERE target = 'Record #102'
        """,
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["action"] == "UPDATE_ALERT"

    notifications = read_db(
        db_path,
        "SELECT id FROM notifications WHERE record_id = 102",
    )
    assert notifications == []



def test_unauthenticated_user_cannot_update_alert_status(test_env):
    client, _ = test_env

    response = client.post(
        "/api/update_status/102",
        json={"status": "RESOLVED", "note": "Completed"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "login" in response.headers["Location"]


def test_technician_cannot_update_another_technicians_alert(test_env):
    client, db_path = test_env
    authenticate(client, 3)

    response = client.post(
        "/api/update_status/102",
        json={"status": "RESOLVED", "note": "Not my alert"},
    )

    assert response.status_code == 403
    row = read_db(
        db_path,
        "SELECT alert_status, resolved_by FROM decision_log WHERE record_id = 102",
    )[0]
    assert row["alert_status"] == "OPEN"
    assert row["resolved_by"] is None


def test_update_status_rejects_invalid_status(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/update_status/101",
        json={"status": "INVALID"},
    )

    assert response.status_code == 400


def test_update_status_returns_404_for_missing_record(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/update_status/999",
        json={"status": "RESOLVED"},
    )

    assert response.status_code == 404


def test_technician_can_resolve_own_alert_and_save_resolution_details(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/update_status/102",
        json={"status": "RESOLVED", "note": "Inspection completed"},
    )

    assert response.status_code == 200
    row = read_db(
        db_path,
        """
        SELECT alert_status, resolved_by, resolved_at, resolution_note
        FROM decision_log WHERE record_id = 102
        """,
    )[0]
    assert row["alert_status"] == "RESOLVED"
    assert row["resolved_by"] == "tech_test"
    assert row["resolved_at"]
    assert row["resolution_note"] == "Inspection completed"


def test_false_positive_resolution_saves_status_and_note(test_env):
    client, db_path = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/update_status/102",
        json={"status": "FALSE_POSITIVE", "note": "Sensor reading verified"},
    )

    assert response.status_code == 200
    row = read_db(
        db_path,
        """
        SELECT alert_status, resolved_by, resolved_at, resolution_note
        FROM decision_log WHERE record_id = 102
        """,
    )[0]
    assert row["alert_status"] == "FALSE_POSITIVE"
    assert row["resolved_by"] == "tech_test"
    assert row["resolved_at"]
    assert row["resolution_note"] == "Sensor reading verified"


def test_closed_alert_cannot_change_to_another_status(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/update_status/103",
        json={"status": "OPEN"},
    )

    assert response.status_code == 400
    row = read_db(
        db_path,
        "SELECT alert_status FROM decision_log WHERE record_id = 103",
    )[0]
    assert row["alert_status"] == "RESOLVED"


def test_reopen_alert_forbidden_to_technician(test_env):
    client, _ = test_env
    authenticate(client, 2)

    response = client.post(
        "/api/reopen_alert/103",
        json={"reason": "Recheck required"},
    )

    assert response.status_code == 403


def test_reopen_alert_requires_reason(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/reopen_alert/103",
        json={"reason": "   "},
    )

    assert response.status_code == 400


def test_reopen_alert_returns_404_for_missing_record(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/reopen_alert/999",
        json={"reason": "Investigate missing record"},
    )

    assert response.status_code == 404


def test_reopen_alert_rejects_non_closed_alert(test_env):
    client, _ = test_env
    authenticate(client, 1)

    response = client.post(
        "/api/reopen_alert/101",
        json={"reason": "Try reopening an open alert"},
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("record_id", "expected_status", "expected_assignee"),
    [
        (103, "OPEN", None),
        (104, "OPEN", None),
    ],
)
def test_admin_can_reopen_closed_alert_and_clear_resolution_details(
    test_env, record_id, expected_status, expected_assignee
):
    client, db_path = test_env
    authenticate(client, 1)

    # Populate resolution fields to verify reopening clears them.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE decision_log
            SET resolved_by = ?, resolved_at = ?, resolution_note = ?
            WHERE record_id = ?
            """,
            ("tech_test", "2026-10-01T10:00:00", "Previously closed", record_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/api/reopen_alert/{record_id}",
        json={"reason": "Further investigation required"},
    )

    assert response.status_code == 200
    assert response.get_json()["alert_status"] == expected_status

    row = read_db(
        db_path,
        """
        SELECT alert_status, assigned_to, resolved_by, resolved_at, resolution_note
        FROM decision_log WHERE record_id = ?
        """,
        (record_id,),
    )[0]
    assert row["alert_status"] == expected_status
    assert row["assigned_to"] == expected_assignee
    assert row["resolved_by"] is None
    assert row["resolved_at"] is None
    assert row["resolution_note"] is None

    audit_rows = read_db(
        db_path,
        """
        SELECT username, action, target, details
        FROM audit_log
        WHERE action = 'REOPEN_ALERT' AND target = ?
        """,
        (f"Record #{record_id}",),
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["username"] == "admin_test"
    assert "Further investigation required" in audit_rows[0]["details"]


def test_api_alerts_returns_risk_records_sorted_by_score(test_env, monkeypatch):
    client, db_path = test_env

    # Add the columns selected by /api/alerts to the temporary test database.
    conn = sqlite3.connect(db_path)
    try:
        for column, column_type in [
            ("pred_prob", "REAL"),
            ("risk_score", "REAL"),
            ("risk_level", "TEXT"),
            ("action", "TEXT"),
            ("reasons", "TEXT"),
            ("shap_explanation", "TEXT"),
            ("timestamp", "TEXT"),
        ]:
            conn.execute(
                f"ALTER TABLE decision_log ADD COLUMN {column} {column_type}"
            )

        conn.executemany(
            """
            UPDATE decision_log
            SET pred_prob = ?, risk_score = ?, risk_level = ?,
                action = ?, reasons = ?, shap_explanation = ?, timestamp = ?
            WHERE record_id = ?
            """,
            [
                (0.8, 80, "High", "Inspect", "High risk", "{}", "2026-01-01", 101),
                (0.9, 95, "Critical", "Stop", "Critical risk", "{}", "2026-01-02", 102),
                (0.4, 40, "Medium", "Monitor", "Medium risk", "{}", "2026-01-03", 103),
                (0.2, 20, "Low", "Observe", "Low risk", "{}", "2026-01-04", 104),
            ],
        )

        # This record must be excluded by the API's risk-level filter.
        conn.execute(
            """
            INSERT INTO decision_log
                (record_id, alert_status, pred_prob, risk_score, risk_level,
                 action, reasons, shap_explanation, timestamp)
            VALUES (105, 'OPEN', 0.1, 10, 'Unknown',
                    'Ignore', 'Unknown level', '{}', '2026-01-05')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Keep the test independent of the project's real machine/asset data.
    monkeypatch.setattr(
        app_module, "machine_id_for_record",
        lambda record_id: f"M{record_id}"
    )
    monkeypatch.setattr(
        app_module, "asset_id_for_machine",
        lambda machine_id: f"A{machine_id}"
    )

    authenticate(client, 1)
    response = client.get("/api/alerts")

    assert response.status_code == 200
    data = response.get_json()

    assert isinstance(data, list)
    assert [item["record_id"] for item in data] == [102, 101, 103, 104]
    assert [item["risk_score"] for item in data] == [95, 80, 40, 20]

    assert all(item["risk_level"] in {"Low", "Medium", "High", "Critical"}
               for item in data)

    assert data[0]["machine_id"] == "M102"
    assert data[0]["asset_id"] == "AM102"


def test_api_alerts_returns_empty_list_when_no_risk_records(test_env):
    client, db_path = test_env

    authenticate(client, 1)

    conn = sqlite3.connect(db_path)
    try:
        existing_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(decision_log)"
            ).fetchall()
        }

        required_columns = {
            "pred_prob": "REAL",
            "risk_score": "REAL",
            "risk_level": "TEXT",
            "action": "TEXT",
            "reasons": "TEXT",
            "shap_explanation": "TEXT",
            "timestamp": "TEXT",
        }

        for column, column_type in required_columns.items():
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE decision_log "
                    f"ADD COLUMN {column} {column_type}"
                )

        conn.execute("DELETE FROM decision_log")
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/alerts")

    assert response.status_code == 200
    assert response.get_json() == []


def test_api_alerts_returns_500_when_database_query_fails(test_env, monkeypatch):
    client, _ = test_env

    def raise_database_error(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(app_module.pd, "read_sql", raise_database_error)

    authenticate(client, 1)
    response = client.get("/api/alerts")

    assert response.status_code == 500
    assert response.get_json()["error"] == "simulated database failure"

def test_api_records_returns_records_sorted_by_risk_score(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    conn = sqlite3.connect(db_path)
    try:
        for column, column_type in [
            ("pred_prob", "REAL"),
            ("risk_score", "REAL"),
            ("risk_level", "TEXT"),
            ("action", "TEXT"),
            ("reasons", "TEXT"),
            ("shap_explanation", "TEXT"),
            ("timestamp", "TEXT"),
        ]:
            conn.execute(
                f"ALTER TABLE decision_log ADD COLUMN {column} {column_type}"
            )

        conn.executemany(
            """
            UPDATE decision_log
            SET risk_score = ?, risk_level = ?
            WHERE record_id = ?
            """,
            [
                (80, "High", 101),
                (95, "Critical", 102),
                (40, "Medium", 103),
                (20, "Low", 104),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/records")

    assert response.status_code == 200
    data = response.get_json()
    assert [item["record_id"] for item in data] == [102, 101, 103, 104]
    assert [item["risk_score"] for item in data] == [95, 80, 40, 20]


def test_api_records_filters_by_risk_level(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    conn = sqlite3.connect(db_path)
    try:
        for column, column_type in [
            ("pred_prob", "REAL"),
            ("risk_score", "REAL"),
            ("risk_level", "TEXT"),
            ("action", "TEXT"),
            ("reasons", "TEXT"),
            ("shap_explanation", "TEXT"),
            ("timestamp", "TEXT"),
        ]:
            conn.execute(
                f"ALTER TABLE decision_log ADD COLUMN {column} {column_type}"
            )

        conn.executemany(
            """
            UPDATE decision_log
            SET risk_score = ?, risk_level = ?
            WHERE record_id = ?
            """,
            [
                (80, "High", 101),
                (95, "Critical", 102),
                (40, "High", 103),
                (20, "Low", 104),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/records?level=High")

    assert response.status_code == 200
    data = response.get_json()
    assert [item["record_id"] for item in data] == [101, 103]
    assert all(item["risk_level"] == "High" for item in data)


def test_api_records_respects_limit_parameter(test_env):
    client, db_path = test_env
    authenticate(client, 1)

    conn = sqlite3.connect(db_path)
    try:
        for column, column_type in [
            ("pred_prob", "REAL"),
            ("risk_score", "REAL"),
            ("risk_level", "TEXT"),
            ("action", "TEXT"),
            ("reasons", "TEXT"),
            ("shap_explanation", "TEXT"),
            ("timestamp", "TEXT"),
        ]:
            conn.execute(
                f"ALTER TABLE decision_log ADD COLUMN {column} {column_type}"
            )

        conn.executemany(
            """
            UPDATE decision_log
            SET risk_score = ?, risk_level = ?
            WHERE record_id = ?
            """,
            [
                (80, "High", 101),
                (95, "Critical", 102),
                (40, "Medium", 103),
                (20, "Low", 104),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/records?limit=2")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 2
    assert [item["record_id"] for item in data] == [102, 101]


def test_api_records_returns_500_when_database_query_fails(
    test_env, monkeypatch
):
    client, _ = test_env
    authenticate(client, 1)

    def raise_database_error(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(app_module.pd, "read_sql", raise_database_error)

    response = client.get("/api/records")

    assert response.status_code == 500
    assert response.get_json()["error"] == "simulated database failure"


def test_unauthenticated_user_cannot_access_api_records(test_env):
    client, _ = test_env

    response = client.get("/api/records", follow_redirects=False)

    assert response.status_code == 302
    assert "login" in response.headers["Location"]
