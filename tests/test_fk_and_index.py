"""Regression tests for DEF-05 / LIM-02: unique index on decision_log.record_id
and enforced foreign keys. Runs on a throw-away database, so it never touches
machine_monitor.db and does not affect the other tests."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite3
import pytest

import app as A


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(A, "DB", db_path)              # restored automatically after the test
    monkeypatch.setattr(A, "_db_configured", False)

    # decision_log is normally created by the training notebook (no PK / unique index)
    c = sqlite3.connect(db_path)
    c.execute("CREATE TABLE decision_log (record_id INTEGER, risk_level TEXT, alert_status TEXT)")
    c.executemany("INSERT INTO decision_log VALUES (?, 'Low', 'OK')", [(i,) for i in range(5)])
    c.commit()
    c.close()

    A.init_unique_record_index()
    A.init_work_orders_table()
    A.init_indexes()

    conn = A.get_raw_conn()
    yield conn
    conn.close()


def test_fresh_database_has_unique_record_id_index(fresh_db):
    idx = fresh_db.execute("PRAGMA index_list(decision_log)").fetchall()
    assert any(row[2] == 1 for row in idx)                      # a UNIQUE index exists
    assert fresh_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_key_is_enforced(fresh_db):
    with pytest.raises(sqlite3.IntegrityError):                 # record_id 999 does not exist
        fresh_db.execute(
            "INSERT INTO work_orders (record_id, title, created_by, created_at) "
            "VALUES (999, 'x', 'a', 'now')")
    fresh_db.execute(                                           # record_id 3 exists -> allowed
        "INSERT INTO work_orders (record_id, title, created_by, created_at) "
        "VALUES (3, 'x', 'a', 'now')")