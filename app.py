from flask import Flask, render_template, jsonify, request, g, redirect, url_for, flash
import sqlite3
import pandas as pd
import threading
from flasgger import Swagger
from flask import Response
import csv
import io
import os, secrets
import psutil
import time
import queue
import json
import math
from datetime import datetime, timezone, timedelta
from functools import wraps
from sklearn.metrics import roc_curve, roc_auc_score
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)

APP_START_TIME = datetime.now(timezone.utc).replace(tzinfo=None)
MODEL_VERSION = "Random Forest + Logistic Regression"

# ══════════════════════════════════════════
# SIMULATED ASSET MAPPING
# ══════════════════════════════════════════
NUM_MACHINES = 20

def machine_id_for_record(record_id):
    idx = (record_id % NUM_MACHINES) + 1
    return f"MCH-{idx:03d}"


def asset_id_for_machine(machine_id):
    try:
        idx = int(machine_id.split('-')[1])
        return f"AST-{1000 + idx}"
    except (IndexError, ValueError):
        return None

# ══════════════════════════════════════════
# WORK ORDER RULES & CONSTANTS
# ══════════════════════════════════════════
RISK_TO_PRIORITY = {
    'Critical': 'CRITICAL',
    'High':     'HIGH',
    'Medium':   'MEDIUM',
    'Low':      'LOW',
}

PRIORITY_DUE_HOURS = {
    'CRITICAL': 4,
    'HIGH':     24,
    'MEDIUM':   72,   # 3 days
    'LOW':      168,  # 7 days
}

def compute_due_date(priority):
    hours = PRIORITY_DUE_HOURS.get(priority, 72)
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)).isoformat(timespec='seconds')

# ══════════════════════════════════════════
# PM & ASSET STATUS HELPERS
# ══════════════════════════════════════════
PM_DUE_SOON_DAYS = 7
PM_DUE_SOON_WEAR = 20
OPEN_WO_SQL = "('OPEN','ASSIGNED','ACCEPTED','IN_PROGRESS','PENDING_VERIFICATION')"
ACTIVE_WO_SQL = "('ACCEPTED','IN_PROGRESS')"

def get_latest_tool_wear(machine_id):
    """Latest tool_wear reading for this machine, via the same
    record_id % NUM_MACHINES mapping used by /api/assets/<id>/observations."""
    try:
        machine_idx = int(machine_id.split('-')[1])
    except (IndexError, ValueError):
        return None
    target_remainder = (machine_idx - 1) % NUM_MACHINES
    conn = get_raw_conn()
    row = conn.execute("""
        SELECT tool_wear FROM decision_log
        WHERE record_id % ? = ? ORDER BY record_id DESC LIMIT 1
    """, (NUM_MACHINES, target_remainder)).fetchone()
    conn.close()
    return row[0] if row else None

def compute_pm_status(pm):
    """pm is a sqlite3.Row from pm_schedules. Returns (status, extra_info_dict)."""
    if pm['frequency_type'] == 'DAYS':
        if not pm['next_due_at']:
            return 'ON_SCHEDULE', {}
        due, now = datetime.fromisoformat(pm['next_due_at']), datetime.now(timezone.utc).replace(tzinfo=None)
        if now >= due:
            return 'OVERDUE', {"days_overdue": (now - due).days}
        elif (due - now).days <= PM_DUE_SOON_DAYS:
            return 'DUE_SOON', {"days_remaining": (due - now).days}
        return 'ON_SCHEDULE', {"days_remaining": (due - now).days}
    elif pm['frequency_type'] == 'TOOL_WEAR':
        current_wear = get_latest_tool_wear(pm['machine_id'])
        if current_wear is None or pm['last_performed_tool_wear'] is None:
            return 'ON_SCHEDULE', {}
        remaining = pm['frequency_value'] - (current_wear - pm['last_performed_tool_wear'])
        if remaining <= 0:
            return 'OVERDUE', {"wear_over": abs(remaining)}
        elif remaining <= PM_DUE_SOON_WEAR:
            return 'DUE_SOON', {"wear_remaining": remaining}
        return 'ON_SCHEDULE', {"wear_remaining": remaining}
    return 'ON_SCHEDULE', {}

def compute_next_pm_due(pm, performed_at_dt):
    """DAYS type gets a fixed next_due_at; TOOL_WEAR type is tracked purely
    by last_performed_tool_wear baseline instead, so returns None."""
    if pm['frequency_type'] == 'DAYS':
        return (performed_at_dt + timedelta(days=pm['frequency_value'])).isoformat(timespec='seconds')
    return None

def refresh_asset_status(machine_id):
    """Recompute assets.status from live signals: an open downtime event
    forces DOWN; an in-progress work order forces MAINTENANCE; an overdue
    PM (with neither of the above) forces WARNING; otherwise RUNNING.
    Call this after any downtime/PM/work-order state change."""
    conn = get_raw_conn()
    conn.row_factory = sqlite3.Row
    open_downtime = conn.execute(
        "SELECT 1 FROM downtime_events WHERE machine_id=? AND end_time IS NULL LIMIT 1",
        (machine_id,)
    ).fetchone()
    if open_downtime:
        new_status = 'DOWN'
    else:
        in_progress_wo = conn.execute(
            "SELECT 1 FROM work_orders WHERE machine_id=? AND status IN ('ACCEPTED','IN_PROGRESS') LIMIT 1",
            (machine_id,)
        ).fetchone()
        if in_progress_wo:
            new_status = 'MAINTENANCE'
        else:
            pms = conn.execute("SELECT * FROM pm_schedules WHERE machine_id=?", (machine_id,)).fetchall()
            overdue = any(compute_pm_status(pm)[0] == 'OVERDUE' for pm in pms)
            new_status = 'WARNING' if overdue else 'RUNNING'
    conn.execute("UPDATE assets SET status=? WHERE machine_id=?", (new_status, machine_id))
    conn.commit()
    conn.close()
    return new_status

app = Flask(__name__)
DB = r"C:\Users\limli\Inti Folder\FYP\machine_monitor.db"
MODEL_METRICS_CSV = os.path.join(os.path.dirname(DB), "model_metrics.csv")
SHAP_IMPORTANCE_JSON = os.path.join(os.path.dirname(DB), "shap_importance.json")

app.config['SWAGGER'] = {'title': 'Predictive Maintenance API', 'uiversion': 3}
Swagger(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

bcrypt = Bcrypt(app)

# ══════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════

_db_config_lock = threading.Lock()
_db_configured = False


def _configure_db_once():
    """Set connection-wide SQLite options once per process."""
    global _db_configured
    if _db_configured:
        return
    with _db_config_lock:
        if _db_configured:
            return
        conn = sqlite3.connect(DB, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.commit()
        finally:
            conn.close()
        _db_configured = True


def get_raw_conn():
    _configure_db_once()
    conn = sqlite3.connect(DB, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_db():
    if 'db' not in g:
        _configure_db_once()
        g.db = sqlite3.connect(DB, timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db

_notification_subscribers = {}
_notification_lock = threading.Lock()
_notification_write_lock = threading.Lock()

# Short-lived cache for read-heavy dashboard aggregates. These values are
# intentionally brief so alert/work-order updates become visible quickly while
# repeated navigation does not re-run the same full-table calculations.
_response_cache = {}
_response_cache_lock = threading.Lock()
_RESPONSE_CACHE_TTL = 5


def _cached_response(key):
    now = time.monotonic()
    with _response_cache_lock:
        item = _response_cache.get(key)
        if item and now - item[0] < _RESPONSE_CACHE_TTL:
            return item[1]
        if item:
            _response_cache.pop(key, None)
    return None


def _store_cached_response(key, value):
    with _response_cache_lock:
        _response_cache[key] = (time.monotonic(), value)
    return value


def _clear_response_cache():
    with _response_cache_lock:
        _response_cache.clear()


def _is_admin_username(username):
    conn = get_raw_conn()
    row = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row is not None and row[0] == 'admin'


def _push_to_subscribers(username, payload):
    with _notification_lock:
        if username is None:
            subscriber_groups = [
                (uname, list(qs))
                for uname, qs in _notification_subscribers.items()
            ]
        else:
            targets = list(_notification_subscribers.get(username, []))
            subscriber_groups = None
    if username is None:
        targets = []
        for uname, qs in subscriber_groups:
            if _is_admin_username(uname):
                targets.extend(qs)
    for q in targets:
        q.put(payload)


def create_notification(username, title, message, level='info', record_id=None, notif_type='system'):
    conn = get_raw_conn()
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    cur = conn.execute(
        "INSERT INTO notifications (username, title, message, level, type, record_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, title, message, level, notif_type, record_id, created_at)
    )
    conn.commit()
    notif_id = cur.lastrowid
    conn.close()

    _push_to_subscribers(username, {
        "id": notif_id, "title": title, "message": message,
        "level": level, "type": notif_type, "record_id": record_id,
        "created_at": created_at, "read_at": None,
    })
    return notif_id


def _notification_exists_for_record(record_id, notif_type):
    conn = get_raw_conn()
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE record_id=? AND type=? LIMIT 1",
        (record_id, notif_type)
    ).fetchone()
    conn.close()
    return row is not None

def df_to_records(df):
    df = df.astype(object).where(pd.notnull(df), None)
    return df.to_dict(orient='records')


def _latest_decisions_by_machine(conn):
    """Newest decision_log row per machine. decision_log has no machine_id
    column, so the machine is derived from record_id like machine_id_for_record()."""
    latest = {}
    try:
        rows = conn.execute(
            f"SELECT d.* FROM decision_log d "
            f"JOIN (SELECT MAX(record_id) AS rid FROM decision_log "
            f"      GROUP BY record_id % {NUM_MACHINES}) m ON d.record_id = m.rid"
        ).fetchall()
    except sqlite3.OperationalError:
        return latest
    for row in rows:
        latest[machine_id_for_record(row['record_id'])] = row
    return latest


def _machine_maintenance_status(conn, asset):
    """Combine operational status, open work, and PM state for the digital twin."""
    if asset['status'] == 'DOWN':
        return 'DOWN'
    if asset['status'] == 'MAINTENANCE':  # effective status, from active WO
        return 'IN_PROGRESS'

    overdue = due_soon = False
    try:
        schedules = conn.execute(
            "SELECT * FROM pm_schedules WHERE machine_id=?", (asset['machine_id'],)
        ).fetchall()
        for schedule in schedules:
            status = compute_pm_status(schedule)[0]
            overdue = overdue or status == 'OVERDUE'
            due_soon = due_soon or status == 'DUE_SOON'
    except (sqlite3.OperationalError, KeyError, TypeError, ValueError):
        pass
    if overdue:
        return 'OVERDUE'
    if due_soon:
        return 'DUE_SOON'
    return 'ON_SCHEDULE'


def _effective_asset_status(conn, asset):
    """Return the current operational status using the same live rules as
    refresh_asset_status, without relying on a potentially stale stored value."""
    machine_id = asset['machine_id']
    try:
        if conn.execute(
            "SELECT 1 FROM downtime_events WHERE machine_id=? AND end_time IS NULL LIMIT 1",
            (machine_id,)
        ).fetchone():
            return 'DOWN'
        if conn.execute(
            "SELECT 1 FROM work_orders WHERE machine_id=? "
            "AND status IN ('ACCEPTED','IN_PROGRESS') LIMIT 1",
            (machine_id,)
        ).fetchone():
            return 'MAINTENANCE'
        schedules = conn.execute(
            "SELECT * FROM pm_schedules WHERE machine_id=?", (machine_id,)
        ).fetchall()
        if any(compute_pm_status(schedule)[0] == 'OVERDUE' for schedule in schedules):
            return 'WARNING'
    except (sqlite3.OperationalError, KeyError, TypeError, ValueError):
        return asset['status'] or 'RUNNING'
    return 'RUNNING'


def _machine_snapshot(conn, asset, latest_decisions):
    decision = latest_decisions.get(asset['machine_id'])
    decision_dict = dict(decision) if decision else {}
    risk_level = decision_dict.get('risk_level') or None
    risk_score = decision_dict.get('risk_score')
    probability = decision_dict.get('pred_prob')
    try:
        risk_score = round(float(risk_score)) if risk_score is not None else None
    except (TypeError, ValueError):
        risk_score = None
    try:
        probability = round(float(probability), 3) if probability is not None else None
    except (TypeError, ValueError):
        probability = None
    try:
        open_work_orders = conn.execute(
            f"SELECT COUNT(*) c FROM work_orders WHERE machine_id=? AND status IN {OPEN_WO_SQL}",
            (asset['machine_id'],)
        ).fetchone()['c']
    except sqlite3.OperationalError:
        open_work_orders = 0
    effective_status = _effective_asset_status(conn, asset)
    effective_asset = {**dict(asset), 'status': effective_status}
    return {
        **dict(asset),
        'status': effective_status,
        'risk_level': risk_level,
        'risk_score': risk_score,
        'pred_prob': probability,
        'prediction_timestamp': decision_dict.get('timestamp'),
        'air_temp': decision_dict.get('air_temp'),
        'process_temp': decision_dict.get('process_temp'),
        'rpm': decision_dict.get('rpm'),
        'torque': decision_dict.get('torque'),
        'tool_wear': decision_dict.get('tool_wear'),
        'maintenance_status': _machine_maintenance_status(conn, effective_asset),
        'open_work_orders': open_work_orders,
        'decision_timestamp': decision_dict.get('timestamp'),
    }


def _kpis_for_machines(conn, machine_ids, days=30):
    """Calculate simple rolling reliability KPIs from recorded downtime."""
    machine_ids = list(machine_ids)
    period_minutes = days * 24 * 60 * len(machine_ids)
    if not machine_ids:
        return {
            'window_days': days, 'availability': None, 'mtbf_hours': None,
            'mttr_minutes': None, 'failure_count': 0, 'downtime_minutes': 0,
        }
    placeholders = ','.join('?' for _ in machine_ids)
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat(timespec='seconds')
    downtime_minutes = 0
    failure_downtime_minutes = 0
    failure_count = 0
    try:
        rows = conn.execute(
            f"SELECT start_time, end_time, duration_minutes, reason_category "
            f"FROM downtime_events WHERE machine_id IN ({placeholders}) AND start_time >= ?",
            (*machine_ids, cutoff)
        ).fetchall()
        for row in rows:
            duration = row['duration_minutes']
            if duration is None and row['start_time']:
                try:
                    end = datetime.fromisoformat(row['end_time']) if row['end_time'] else datetime.now(timezone.utc).replace(tzinfo=None)
                    duration = max(0, int((end - datetime.fromisoformat(row['start_time'])).total_seconds() // 60))
                except (TypeError, ValueError):
                    duration = 0
            downtime_minutes += int(duration or 0)
            if row['reason_category'] == 'UNPLANNED_FAILURE':
                failure_count += 1
                failure_downtime_minutes += int(duration or 0)
    except sqlite3.OperationalError:
        pass

    availability = max(0.0, min(100.0, (period_minutes - downtime_minutes) / period_minutes * 100))
    operating_hours = max(0.0, (period_minutes - downtime_minutes) / 60)
    return {
        'window_days': days,
        'availability': round(availability, 1),
        'mtbf_hours': round(operating_hours / failure_count, 1) if failure_count else None,
        'mttr_minutes': round(failure_downtime_minutes / failure_count, 1) if failure_count else None,
        'failure_count': failure_count,
        'downtime_minutes': downtime_minutes,
    }


def _failure_trend(conn, machine_ids, days=14):
    """Return a filled daily failure series for the dashboard chart."""
    machine_ids = list(machine_ids)
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1)
    counts = {}
    if machine_ids:
        placeholders = ','.join('?' for _ in machine_ids)
        try:
            rows = conn.execute(
                f"SELECT start_time FROM downtime_events WHERE machine_id IN ({placeholders}) "
                "AND reason_category='UNPLANNED_FAILURE' AND start_time >= ?",
                (*machine_ids, start.isoformat(timespec='seconds'))
            ).fetchall()
            for row in rows:
                try:
                    day = datetime.fromisoformat(row['start_time']).date().isoformat()
                    counts[day] = counts.get(day, 0) + 1
                except (TypeError, ValueError):
                    continue
        except sqlite3.OperationalError:
            pass
    return [
        {'date': (start + timedelta(days=i)).date().isoformat(),
         'failures': counts.get((start + timedelta(days=i)).date().isoformat(), 0)}
        for i in range(days)
    ]

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_users_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def init_notifications_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            type TEXT NOT NULL DEFAULT 'system',
            record_id INTEGER,
            created_at TEXT NOT NULL,
            read_at TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN type TEXT NOT NULL DEFAULT 'system'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def init_production_lines_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS production_lines (
            line_id TEXT PRIMARY KEY,
            line_name TEXT NOT NULL,
            location TEXT,
            status TEXT NOT NULL DEFAULT 'RUNNING'
        )
    """)
    conn.commit()
    conn.close()


def init_assets_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,
            machine_id TEXT UNIQUE NOT NULL,
            machine_name TEXT NOT NULL,
            machine_type TEXT,
            line_id TEXT NOT NULL,
            location TEXT,
            manufacturer TEXT,
            model TEXT,
            criticality TEXT NOT NULL DEFAULT 'MEDIUM',
            status TEXT NOT NULL DEFAULT 'RUNNING',
            FOREIGN KEY (line_id) REFERENCES production_lines(line_id)
        )
    """)
    conn.commit()
    conn.close()

def seed_assets_and_lines():
    conn = get_raw_conn()
    existing = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()[0]
    if existing > 0:
        conn.close()
        return

    lines = [
        ("LINE-01", "Assembly Line 1", "Building A - Floor 1"),
        ("LINE-02", "Assembly Line 2", "Building A - Floor 2"),
        ("LINE-03", "Machining Line 1", "Building B - Floor 1"),
        ("LINE-04", "Machining Line 2", "Building B - Floor 2"),
    ]
    for line_id, name, location in lines:
        conn.execute(
            "INSERT INTO production_lines (line_id, line_name, location, status) "
            "VALUES (?, ?, ?, 'RUNNING')",
            (line_id, name, location)
        )

    machine_types = ["CNC Machine", "Injection Press", "Conveyor Motor", "Hydraulic Press"]
    criticalities = ["HIGH", "MEDIUM", "MEDIUM", "LOW"]

    for i in range(1, NUM_MACHINES + 1):
        machine_id = f"MCH-{i:03d}"
        asset_id = f"AST-{1000 + i}"
        line_id = lines[(i - 1) % len(lines)][0]
        m_type = machine_types[(i - 1) % len(machine_types)]
        crit = criticalities[(i - 1) % len(criticalities)]
        conn.execute("""
            INSERT INTO assets
                (asset_id, machine_id, machine_name, machine_type, line_id,
                 location, manufacturer, model, criticality, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING')
        """, (
            asset_id, machine_id, f"{m_type} {i:02d}", m_type, line_id,
            f"{line_id} - Bay {((i - 1) % 5) + 1}", "Simulated Mfg Co.",
            f"{m_type[:3].upper()}-{2020 + (i % 5)}", crit
        ))

    conn.commit()
    conn.close()

def seed_demo_maintenance_data():
    """Deterministic (non-random) demo data across all 20 machines: every
    machine gets at least one PM schedule; a subset also get work orders,
    maintenance history, and a closed downtime event. Re-running this is
    idempotent (no-op if pm_schedules already has rows) so it's safe to
    call on every boot. Does NOT touch failure history — that stays derived
    live from decision_log via /api/assets/<id>/failure-history.
    """
    conn = get_raw_conn()
    existing = conn.execute("SELECT COUNT(*) c FROM pm_schedules").fetchone()[0]
    if existing > 0:
        conn.close()
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # machine_index -> {
    #   "pms": [(name, freq_type, freq_value, offset), ...],
    #     DAYS offset       = days until next_due_at (negative = overdue)
    #     TOOL_WEAR offset  = wear units "remaining" until due (negative = overdue)
    #   "wo": number of manual work orders to create,
    #   "maint": number of maintenance_records to create,
    #   "downtime": whether to log one closed downtime event,
    # }
    MACHINE_PROFILES = {
        1:  {"pms": [("Spindle Lubrication", "DAYS", 30, -3), ("Tool Change", "TOOL_WEAR", 200, -15)], "wo": 2, "maint": 2, "downtime": True},
        2:  {"pms": [("Bearing Inspection", "DAYS", 60, 5)], "wo": 1, "maint": 1, "downtime": False},
        3:  {"pms": [("Tool Change", "TOOL_WEAR", 200, 120)], "wo": 0, "maint": 0, "downtime": False},
        4:  {"pms": [("Belt Replacement", "DAYS", 90, 40)], "wo": 0, "maint": 0, "downtime": True},
        5:  {"pms": [("Hydraulic Fluid Check", "DAYS", 45, 4), ("Tool Change", "TOOL_WEAR", 150, 20)], "wo": 1, "maint": 1, "downtime": False},
        6:  {"pms": [("Lubrication", "DAYS", 30, 22)], "wo": 0, "maint": 0, "downtime": False},
        7:  {"pms": [("Coolant Filter", "DAYS", 30, -1)], "wo": 1, "maint": 1, "downtime": True},
        8:  {"pms": [("Tool Change", "TOOL_WEAR", 200, 60)], "wo": 0, "maint": 0, "downtime": False},
        9:  {"pms": [("Motor Inspection", "DAYS", 60, 30)], "wo": 1, "maint": 0, "downtime": False},
        10: {"pms": [("Belt Tension Check", "DAYS", 45, 6)], "wo": 0, "maint": 1, "downtime": True},
        11: {"pms": [("Tool Change", "TOOL_WEAR", 180, -5), ("General Inspection", "DAYS", 30, 15)], "wo": 2, "maint": 1, "downtime": False},
        12: {"pms": [("Hydraulic Fluid Check", "DAYS", 45, 33)], "wo": 0, "maint": 0, "downtime": False},
        13: {"pms": [("Bearing Inspection", "DAYS", 60, -8)], "wo": 1, "maint": 1, "downtime": True},
        14: {"pms": [("Tool Change", "TOOL_WEAR", 200, 90)], "wo": 0, "maint": 0, "downtime": False},
        15: {"pms": [("Lubrication", "DAYS", 30, 7)], "wo": 1, "maint": 1, "downtime": False},
        16: {"pms": [("Coolant Filter", "DAYS", 30, 18)], "wo": 0, "maint": 0, "downtime": False},
        17: {"pms": [("Tool Change", "TOOL_WEAR", 150, -25)], "wo": 0, "maint": 0, "downtime": True},
        18: {"pms": [("General Inspection", "DAYS", 45, 3)], "wo": 1, "maint": 1, "downtime": False},
        19: {"pms": [("Belt Replacement", "DAYS", 90, 50)], "wo": 0, "maint": 0, "downtime": False},
        20: {"pms": [("Bearing Inspection", "DAYS", 60, -2), ("Tool Change", "TOOL_WEAR", 200, 10)], "wo": 1, "maint": 1, "downtime": True},
    }

    for i in range(1, NUM_MACHINES + 1):
        machine_id = f"MCH-{i:03d}"
        asset_id = f"AST-{1000 + i}"
        profile = MACHINE_PROFILES[i]

        # --- PM schedules ---
        for pm_name, freq_type, freq_value, offset in profile["pms"]:
            if freq_type == "DAYS":
                next_due_at = (now + timedelta(days=offset)).isoformat(timespec='seconds')
                conn.execute("""
                    INSERT INTO pm_schedules
                        (asset_id, machine_id, pm_name, frequency_type, frequency_value,
                         next_due_at, status, created_by, created_at)
                    VALUES (?, ?, ?, 'DAYS', ?, ?, 'ON_SCHEDULE', 'system', ?)
                """, (asset_id, machine_id, pm_name, freq_value, next_due_at,
                      now.isoformat(timespec='seconds')))
            else:  # TOOL_WEAR
                current_wear = get_latest_tool_wear(machine_id) or 0
                last_performed_tool_wear = current_wear - (freq_value - offset)
                conn.execute("""
                    INSERT INTO pm_schedules
                        (asset_id, machine_id, pm_name, frequency_type, frequency_value,
                         last_performed_tool_wear, status, created_by, created_at)
                    VALUES (?, ?, ?, 'TOOL_WEAR', ?, ?, 'ON_SCHEDULE', 'system', ?)
                """, (asset_id, machine_id, pm_name, freq_value, last_performed_tool_wear,
                      now.isoformat(timespec='seconds')))

        # --- Work orders (manual — shows "Manual" source badge on the UI) ---
        for n in range(profile["wo"]):
            priority = "HIGH" if n == 0 else "MEDIUM"
            conn.execute("""
                INSERT INTO work_orders
                    (record_id, machine_id, asset_id, title, description, priority,
                     status, created_by, created_at, due_date)
                VALUES (NULL, ?, ?, ?, ?, ?, 'OPEN', 'system', ?, ?)
            """, (
                machine_id, asset_id, f"Scheduled Check #{n + 1} — {machine_id}",
                "Routine inspection flagged during planning.", priority,
                now.isoformat(timespec='seconds'), compute_due_date(priority)
            ))

        # --- Maintenance records (standalone, not tied to a WO/PM) ---
        for n in range(profile["maint"]):
            performed_at = now - timedelta(days=5 * (n + 1))
            conn.execute("""
                INSERT INTO maintenance_records
                    (asset_id, machine_id, maintenance_type, description,
                     performed_by, performed_at, created_at)
                VALUES (?, ?, 'PREVENTIVE', ?, 'system', ?, ?)
            """, (
                asset_id, machine_id, f"Routine maintenance performed on {machine_id}",
                performed_at.isoformat(timespec='seconds'), now.isoformat(timespec='seconds')
            ))

        # --- Downtime (one closed event, a few hours ago) ---
        if profile["downtime"]:
            start = now - timedelta(hours=6 + (i % 5))
            end = start + timedelta(minutes=25 + (i % 3) * 10)
            conn.execute("""
                INSERT INTO downtime_events
                    (asset_id, machine_id, start_time, end_time, duration_minutes,
                     reason_category, reason_detail, reported_by, created_at)
                VALUES (?, ?, ?, ?, ?, 'UNPLANNED_FAILURE', ?, 'system', ?)
            """, (
                asset_id, machine_id, start.isoformat(timespec='seconds'),
                end.isoformat(timespec='seconds'), int((end - start).total_seconds() // 60),
                f"Unplanned stoppage on {machine_id}", now.isoformat(timespec='seconds')
            ))

    conn.commit()
    conn.close()

def init_assignment_column():
    conn = get_raw_conn()
    try:
        conn.execute("ALTER TABLE decision_log ADD COLUMN assigned_to TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def init_resolution_columns():
    conn = get_raw_conn()
    for col_sql in [
        "ALTER TABLE decision_log ADD COLUMN resolved_by TEXT",
        "ALTER TABLE decision_log ADD COLUMN resolved_at TEXT",
        "ALTER TABLE decision_log ADD COLUMN resolution_note TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def init_work_orders_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_orders (
            wo_id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER,
            machine_id TEXT,
            asset_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            recommended_action TEXT,
            risk_level TEXT,
            risk_score INTEGER,
            priority TEXT NOT NULL DEFAULT 'MEDIUM',
            status TEXT NOT NULL DEFAULT 'OPEN',
            assigned_to TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            due_date TEXT,
            completed_at TEXT,
            completion_note TEXT,
            FOREIGN KEY (record_id) REFERENCES decision_log(record_id)
        )
    """)
    conn.commit()
    conn.close()

def init_workflow_columns():
    conn = get_raw_conn()
    for col_sql in [
        "ALTER TABLE work_orders ADD COLUMN accepted_at TEXT",
        "ALTER TABLE work_orders ADD COLUMN started_at TEXT",
        "ALTER TABLE work_orders ADD COLUMN technician_notes TEXT",
        "ALTER TABLE work_orders ADD COLUMN resolution_note TEXT",
        "ALTER TABLE work_orders ADD COLUMN verification_note TEXT",
        "ALTER TABLE work_orders ADD COLUMN verified_by TEXT",
        "ALTER TABLE work_orders ADD COLUMN verified_at TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

def init_pm_schedules_table():
    """Create the pm_schedules table if it doesn't exist yet."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pm_schedules (
            pm_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            pm_name TEXT NOT NULL,
            frequency_type TEXT NOT NULL,          -- 'DAYS' | 'TOOL_WEAR'
            frequency_value INTEGER NOT NULL,
            last_performed_at TEXT,
            last_performed_tool_wear INTEGER,
            next_due_at TEXT,
            status TEXT NOT NULL DEFAULT 'ON_SCHEDULE',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
        )
    """)
    conn.commit()
    conn.close()

def init_maintenance_records_table():
    """Create the maintenance_records table if it doesn't exist yet."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_records (
            maint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            maintenance_type TEXT NOT NULL,        -- 'CORRECTIVE' | 'PREVENTIVE' | 'INSPECTION'
            source_wo_id INTEGER,
            source_pm_id INTEGER,
            description TEXT NOT NULL,
            performed_by TEXT NOT NULL,
            performed_at TEXT NOT NULL,
            downtime_minutes INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(asset_id),
            FOREIGN KEY (source_wo_id) REFERENCES work_orders(wo_id),
            FOREIGN KEY (source_pm_id) REFERENCES pm_schedules(pm_id)
        )
    """)
    conn.commit()
    conn.close()

def init_downtime_events_table():
    """Create the downtime_events table if it doesn't exist yet."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downtime_events (
            downtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_minutes INTEGER,
            reason_category TEXT NOT NULL,         -- UNPLANNED_FAILURE | PLANNED_MAINTENANCE | CHANGEOVER | OTHER
            reason_detail TEXT,
            linked_wo_id INTEGER,
            reported_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
        )
    """)
    conn.commit()
    conn.close()

def init_indexes():
    conn = get_raw_conn()
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_decision_log_risk_score ON decision_log(risk_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_decision_log_alert_status ON decision_log(alert_status)",
        "CREATE INDEX IF NOT EXISTS idx_decision_log_risk_level ON decision_log(risk_level)",
        "CREATE INDEX IF NOT EXISTS idx_work_orders_machine_id ON work_orders(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_work_orders_status ON work_orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_username ON notifications(username)",
        "CREATE INDEX IF NOT EXISTS idx_pm_schedules_machine_id ON pm_schedules(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_records_machine_id ON maintenance_records(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_downtime_events_open ON downtime_events(machine_id, end_time)",
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

# ══════════════════════════════════════════
# SYSTEM HEALTH
# ══════════════════════════════════════════

def check_database_health():
    start = time.perf_counter()
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        status = "healthy" if latency_ms < 200 else "degraded"
        return {"status": status, "latency_ms": latency_ms, "error": None}
    except Exception as e:
        return {"status": "down", "latency_ms": None, "error": str(e)}


def check_model_health():
    result = {
        "status": "healthy",
        "metrics_file_exists": False,
        "metrics_last_updated": None,
        "decision_log_rows": None,
        "predictions_rows": None,
        "row_count_match": None,
        "error": None,
    }
    try:
        if os.path.exists(MODEL_METRICS_CSV):
            result["metrics_file_exists"] = True
            mtime = os.path.getmtime(MODEL_METRICS_CSV)
            result["metrics_last_updated"] = datetime.fromtimestamp(mtime).isoformat(timespec='seconds')
        else:
            result["status"] = "degraded"

        conn = get_db()
        dl_count = conn.execute("SELECT COUNT(*) c FROM decision_log").fetchone()['c']
        pred_count = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()['c']
        result["decision_log_rows"] = dl_count
        result["predictions_rows"] = pred_count
        result["row_count_match"] = (dl_count == pred_count)

        if not result["row_count_match"]:
            result["status"] = "degraded"
        if not result["metrics_file_exists"]:
            result["status"] = "down"

    except Exception as e:
        result["status"] = "down"
        result["error"] = str(e)

    return result


def check_system_resources():
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": cpu,
            "ram_percent": mem.percent,
            "ram_used_gb": round(mem.used / (1024 ** 3), 2),
            "ram_total_gb": round(mem.total / (1024 ** 3), 2),
            "status": "healthy" if cpu < 85 and mem.percent < 85 else "degraded",
        }
    except Exception as e:
        return {"status": "down", "error": str(e)}

def init_health_log_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            overall_status TEXT NOT NULL,
            api_latency_ms REAL,
            db_latency_ms REAL
        )
    """)
    conn.commit()
    conn.close()


def log_health_check(overall_status, api_latency_ms, db_latency_ms):
    conn = get_db()
    conn.execute(
        "INSERT INTO health_log (timestamp, overall_status, api_latency_ms, db_latency_ms) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), overall_status, api_latency_ms, db_latency_ms)
    )
    conn.execute("""
        DELETE FROM health_log WHERE id NOT IN (
            SELECT id FROM health_log ORDER BY id DESC LIMIT 500
        )
    """)
    conn.commit()

def get_last_incident():
    conn = get_db()
    incident = conn.execute("""
        SELECT id, timestamp, overall_status
        FROM health_log
        WHERE overall_status != 'healthy'
        ORDER BY id DESC LIMIT 1
    """).fetchone()

    if incident is None:
        return {"has_incident": False}

    recovered_check = conn.execute("""
        SELECT COUNT(*) c FROM health_log
        WHERE id > ? AND overall_status = 'healthy'
    """, (incident['id'],)).fetchone()

    return {
        "has_incident": True,
        "timestamp": incident['timestamp'],
        "status": incident['overall_status'],
        "recovered": recovered_check['c'] > 0,
    }

def get_uptime_string():
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - APP_START_TIME
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def compute_health_score(api_status, db_status, model_status, system_status):
    weights = {"healthy": 25, "degraded": 12.5, "down": 0}
    score = sum(weights.get(s, 0) for s in [api_status, db_status, model_status, system_status])
    return round(score)

def init_audit_log_table():
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_action(username, action, target='-', details='-'):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), username, action, target, details)
    )
    conn.commit()


# ══════════════════════════════════════════
# AUTH: User model + Flask-Login setup
# ══════════════════════════════════════════

class User(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.username = row['username']
        self.email = row['email']
        self.password_hash = row['password_hash']
        self.role = row['role']
        self.is_active_flag = bool(row['is_active'])
        self.must_change_password = bool(row['must_change_password'])

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_technician(self):
        return self.role == 'technician'

    @property
    def is_active(self):
        return self.is_active_flag

    def check_password(self, plain_password):
        return bcrypt.check_password_hash(self.password_hash, plain_password)


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return User(row) if row else None


def get_user_by_username(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return User(row) if row else None


login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(int(user_id))


@app.before_request
def enforce_password_change():
    allowed_endpoints = {'change_password', 'logout', 'static'}
    if current_user.is_authenticated and getattr(current_user, 'must_change_password', False):
        if request.endpoint not in allowed_endpoints:
            return redirect(url_for('change_password'))


def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                return jsonify({"error": "Forbidden: insufficient role"}), 403
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


# ══════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = get_user_by_username(username)

        if user is None or not user.check_password(password):
            log_action(username or 'unknown', 'FAILED_LOGIN', '-', 'Invalid credentials')
            flash('Invalid username or password.', 'error')
            return render_template('login.html'), 401

        if not user.is_active_flag:
            log_action(user.username, 'FAILED_LOGIN', '-', 'Account disabled')
            flash('This account has been disabled. Contact an admin.', 'error')
            return render_template('login.html'), 403

        login_user(user)

        conn = get_db()
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), user.id))

        conn.commit()
        log_action(user.username, 'LOGIN', '-', 'Success')

        flash(f'Welcome back, {user.username}!', 'success')
        next_page = request.args.get('next')
        return redirect(next_page or url_for('dashboard'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    log_action(current_user.username, 'LOGOUT', '-', '-')
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# ══════════════════════════════════════════
# ADMIN: Manage users
# ══════════════════════════════════════════

@app.route('/admin/users')
@login_required
@role_required('admin')
def manage_users():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_template('manage_users.html', users=users)


@app.route('/admin/users/create', methods=['POST'])
@login_required
@role_required('admin')
def create_user():
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'technician')

    if role not in ('admin', 'technician'):
        flash('Invalid role.', 'error')
        return redirect(url_for('manage_users'))

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        flash('Username already exists.', 'error')
        return redirect(url_for('manage_users'))

    pw_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    conn.execute(
        "INSERT INTO users (username, email, password_hash, role, is_active, must_change_password, created_at) "
        "VALUES (?, ?, ?, ?, 1, 1, ?)",
        (username, email, pw_hash, role, datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    )
    conn.commit()
    log_action(current_user.username, 'CREATE_USER', username, f'role={role}')
    flash(f"User '{username}' created as {role}.", 'success')
    return redirect(url_for('manage_users'))


@app.route('/admin/users/<int:user_id>/toggle-active', methods=['POST'])
@login_required
@role_required('admin')
def toggle_active(user_id):
    if user_id == current_user.id:
        log_action(current_user.username, 'SECURITY_ALERT', current_user.username, 'Attempted to toggle self active status')
        flash('You cannot disable or modify your own active status.', 'error')
        return redirect(url_for('manage_users'))

    conn = get_db()
    row = conn.execute("SELECT username, role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        flash('User not found.', 'error')
        return redirect(url_for('manage_users'))

    if row['role'] == 'admin' and row['is_active'] == 1:
        active_admins = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='admin' AND is_active=1"
        ).fetchone()['c']
        if active_admins <= 1:
            flash('Cannot disable the last active administrator.', 'error')
            return redirect(url_for('manage_users'))

    new_status = 0 if row['is_active'] else 1
    conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_status, user_id))
    conn.commit()

    log_action(
        current_user.username,
        'ENABLE_USER' if new_status else 'DISABLE_USER',
        row['username'],
        f"active→{bool(new_status)}"
    )
    flash(f"User '{row['username']}' status updated to {'Active' if new_status else 'Disabled'}.", 'info')
    return redirect(url_for('manage_users'))

@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def edit_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None:
        flash('User not found.', 'error')
        return redirect(url_for('manage_users'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        role = request.form.get('role', 'technician')

        if role not in ('admin', 'technician'):
            flash('Invalid role.', 'error')
            return redirect(url_for('edit_user', user_id=user_id))

        if user_id == current_user.id and role != 'admin':
            flash('You cannot remove your own admin role.', 'error')
            return redirect(url_for('edit_user', user_id=user_id))

        changes = []
        if email != user['email']:
            changes.append(f"email: '{user['email']}' → '{email}'")
        if role != user['role']:
            changes.append(f"role: '{user['role']}' → '{role}'")

        if not changes:
            flash('No changes were made.', 'info')
            return redirect(url_for('manage_users'))

        conn.execute("UPDATE users SET email=?, role=? WHERE id=?", (email, role, user_id))
        conn.commit()
        log_action(
            current_user.username,
            'EDIT_USER',
            user['username'],
            ", ".join(changes)
        )
        flash(f"User '{user['username']}' updated.", 'success')
        return redirect(url_for('manage_users'))

    return render_template('edit_user.html', user=user)

@app.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
@role_required('admin')
def reset_password(user_id):
    conn = get_db()
    user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None:
        flash('User not found.', 'error')
        return redirect(url_for('manage_users'))

    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if len(new_password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return redirect(url_for('edit_user', user_id=user_id))

    if new_password != confirm_password:
        flash('Passwords do not match.', 'error')
        return redirect(url_for('edit_user', user_id=user_id))

    pw_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
    conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (pw_hash, user_id))
    conn.commit()
    log_action(current_user.username, 'RESET_PASSWORD', user['username'], 'Admin-initiated reset')
    flash(f"Password reset for '{user['username']}'.", 'success')
    return redirect(url_for('manage_users'))


@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        conn = get_db()
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (current_user.id,)).fetchone()

        if not bcrypt.check_password_hash(row['password_hash'], current_password):
            flash('Current password is incorrect.', 'error')
            return render_template('change_password.html'), 401

        if len(new_password) < 8:
            flash('New password must be at least 8 characters.', 'error')
            return render_template('change_password.html'), 400

        if new_password != confirm_password:
            flash('New passwords do not match.', 'error')
            return render_template('change_password.html'), 400

        pw_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                     (pw_hash, current_user.id))
        conn.commit()
        log_action(current_user.username, 'CHANGE_PASSWORD', current_user.username, 'Self-service')
        flash('Password changed successfully.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('change_password.html')


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def delete_user(user_id):
    if user_id == current_user.id:
        log_action(current_user.username, 'SECURITY_ALERT', current_user.username, 'Attempted to delete self account')
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('manage_users'))

    conn = get_db()
    target_row = conn.execute("SELECT username, role FROM users WHERE id=?", (user_id,)).fetchone()
    if target_row is None:
        flash('User not found.', 'error')
        return redirect(url_for('manage_users'))

    if target_row['role'] == 'admin':
        total_admins = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='admin'"
        ).fetchone()['c']
        if total_admins <= 1:
            flash('Cannot delete the last remaining administrator on the system.', 'error')
            return redirect(url_for('manage_users'))

    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()

    log_action(current_user.username, 'DELETE_USER', target_row['username'], f"role={target_row['role']}")
    flash(f"User '{target_row['username']}' permanently deleted.", 'success')
    return redirect(url_for('manage_users'))


@app.route('/admin/audit-log')
@login_required
@role_required('admin')
def audit_log():
    conn = get_db()
    action_filter = request.args.get('action', '').strip()
    search = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50

    where_clauses = ["1=1"]
    params = []

    if action_filter and action_filter != 'ALL':
        where_clauses.append("action = ?")
        params.append(action_filter)
    if search:
        where_clauses.append("(username LIKE ? OR target LIKE ? OR details LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if date_from:
        where_clauses.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("timestamp <= ?")
        params.append(f"{date_to}T23:59:59")

    where_sql = " AND ".join(where_clauses)

    count_query = f"SELECT COUNT(*) c FROM audit_log WHERE {where_sql}"
    total_count = conn.execute(count_query, params).fetchone()['c']
    total_pages = max(1, math.ceil(total_count / per_page))

    if page < 1:
        page = 1
    elif page > total_pages and total_count > 0:
        page = total_pages

    offset = (page - 1) * per_page
    query = f"SELECT * FROM audit_log WHERE {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    query_params = params + [per_page, offset]
    logs = conn.execute(query, query_params).fetchall()

    actions = conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()

    return render_template(
        'audit_log.html',
        logs=logs,
        actions=actions,
        action_filter=action_filter,
        search=search,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages
    )

@app.route('/admin/audit-log/export')
@login_required
@role_required('admin')
def export_audit_log():
    action_filter = request.args.get('action', '').strip()
    search = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    conn = get_db()
    query = "SELECT timestamp, username, action, target, details FROM audit_log WHERE 1=1"
    params = []

    if action_filter and action_filter != 'ALL':
        query += " AND action=?"
        params.append(action_filter)
    if search:
        query += " AND (username LIKE ? OR target LIKE ? OR details LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    if date_from:
        query += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        query += " AND timestamp <= ?"
        params.append(f"{date_to}T23:59:59")

    query += " ORDER BY timestamp DESC"
    df = pd.read_sql(query, conn, params=params)

    output = io.StringIO()
    df.to_csv(output, index=False)

    log_action(current_user.username, 'EXPORT_AUDIT_LOG', 'Audit Log', f'{len(df)} rows exported')

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"}
    )

@app.route('/api/technicians')
@login_required
@role_required('admin')
def api_technicians():
    conn = get_db()
    rows = conn.execute(
        "SELECT username FROM users WHERE role='technician' AND is_active=1 ORDER BY username"
    ).fetchall()
    return jsonify([r['username'] for r in rows])


# ══════════════════════════════════════════
# API ENDPOINTS: WORK ORDERS
# ══════════════════════════════════════════

@app.route('/api/work-orders', methods=['GET'])
@login_required
def api_work_orders():
    try:
        conn = get_db()
        query = "SELECT * FROM work_orders WHERE 1=1"
        params = []
        status = request.args.get('status')
        priority = request.args.get('priority')
        if status and status != 'ALL':
            query += " AND status=?"
            params.append(status)
        if priority and priority != 'ALL':
            query += " AND priority=?"
            params.append(priority)
        if not current_user.is_admin:
            query += " AND (assigned_to=? OR assigned_to IS NULL)"
            params.append(current_user.username)
        query += " ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, created_at DESC"
        df = pd.read_sql(query, conn, params=params)
        return jsonify(df_to_records(df))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/work-orders/<int:wo_id>', methods=['GET'])
@login_required
def api_work_order_detail(wo_id):
    try:
        conn = get_db()
        wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        if wo is None:
            return jsonify({"error": "Work order not found"}), 404
        result = dict(wo)
        if wo['record_id']:
            alert = conn.execute(
                "SELECT record_id, risk_score, risk_level, pred_prob, action, "
                "reasons, shap_explanation, alert_status FROM decision_log WHERE record_id=?",
                (wo['record_id'],)
            ).fetchone()
            result['source_alert'] = dict(alert) if alert else None
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/work-orders/from-alert/<int:record_id>', methods=['POST'])
@login_required
def api_create_work_order_from_alert(record_id):
    try:
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        alert = conn.execute(
            "SELECT * FROM decision_log WHERE record_id=?", (record_id,)
        ).fetchone()
        if alert is None:
            conn.rollback()
            return jsonify({"error": "Alert not found"}), 404
        if alert['alert_status'] in ('RESOLVED', 'FALSE_POSITIVE'):
            conn.rollback()
            return jsonify({"error": "Reopen this alert before creating a work order"}), 400

        existing = conn.execute(
            "SELECT wo_id FROM work_orders WHERE record_id=? "
            "AND status IN ('OPEN','ASSIGNED','ACCEPTED','IN_PROGRESS','PENDING_VERIFICATION') "
            "ORDER BY created_at DESC, wo_id DESC LIMIT 1",
            (record_id,)
        ).fetchone()
        if existing:
            conn.rollback()
            return jsonify({"error": "An open work order already exists for this alert",
                             "wo_id": existing['wo_id']}), 409

        machine_id = machine_id_for_record(record_id)
        asset_id = asset_id_for_machine(machine_id)
        priority = RISK_TO_PRIORITY.get(alert['risk_level'], 'MEDIUM')
        due_date = compute_due_date(priority)

        data = request.get_json(silent=True) or {}
        title = data.get('title') or f"{alert['risk_level']} Risk — Machine {machine_id}"
        assigned_to = None
        initial_status = 'OPEN'

        cur = conn.execute("""
            INSERT INTO work_orders
                (record_id, machine_id, asset_id, title, description,
                 recommended_action, risk_level, risk_score, priority,
                 status, assigned_to, created_by, created_at, due_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id, machine_id, asset_id, title, alert['reasons'],
            alert['action'], alert['risk_level'], alert['risk_score'], priority,
            initial_status, assigned_to, current_user.username,
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), due_date
        ))
        conn.commit()
        wo_id = cur.lastrowid

        log_action(current_user.username, 'CREATE_WORK_ORDER', f'WO #{wo_id}',
                   f'from alert record #{record_id}, priority={priority}')

        return jsonify({"message": "Work order created", "wo_id": wo_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/work-orders', methods=['POST'])
@login_required
def api_create_work_order_manual():
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({"error": "title is required"}), 400

        priority = data.get('priority', 'MEDIUM')
        if priority not in PRIORITY_DUE_HOURS:
            return jsonify({"error": "Invalid priority"}), 400

        conn = get_db()
        assigned_to = data.get('assigned_to')
        if assigned_to:
            if not current_user.is_admin:
                return jsonify({"error": "Forbidden: Admin access required to assign a technician"}), 403
            active_tech = conn.execute(
                "SELECT username FROM users WHERE username=? AND role='technician' AND is_active=1",
                (assigned_to,)
            ).fetchone()
            if active_tech is None:
                return jsonify({"error": "Invalid technician"}), 400
            assigned_to = active_tech['username']
        initial_status = 'ASSIGNED' if assigned_to else 'OPEN'

        due_date = data.get('due_date') or compute_due_date(priority)
        cur = conn.execute("""
            INSERT INTO work_orders
                (record_id, machine_id, asset_id, title, description,
                 recommended_action, priority, status, assigned_to,
                 created_by, created_at, due_date)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('machine_id'), data.get('asset_id'), title,
            data.get('description'), data.get('recommended_action'),
            priority, initial_status, assigned_to,
            current_user.username, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),
            due_date
        ))
        conn.commit()
        wo_id = cur.lastrowid
        log_action(current_user.username, 'CREATE_WORK_ORDER', f'WO #{wo_id}', 'Manual creation')
        return jsonify({"message": "Work order created", "wo_id": wo_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/work-orders/<int:wo_id>/status', methods=['POST'])
@login_required
def api_update_work_order_status(wo_id):
    try:
        if not current_user.is_admin:
            return jsonify({"error": "Forbidden: Admin access required"}), 403

        data = request.get_json(silent=True) or {}
        status = data.get('status')
        allowed = ['CANCELLED']
        if status not in allowed:
            return jsonify({"error": f"Invalid status, must be one of {allowed}"}), 400

        conn = get_db()
        wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        if wo is None:
            return jsonify({"error": "Work order not found"}), 404

        conn.execute("UPDATE work_orders SET status=? WHERE wo_id=?", (status, wo_id))
        conn.commit()

        log_action(
            current_user.username,
            'UPDATE_WORK_ORDER',
            f'WO #{wo_id}',
            f"{wo['status']}→{status}"
        )
        return jsonify({"message": f"Work order {wo_id} updated to {status}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/work-orders/<int:wo_id>/accept', methods=['POST'])
@login_required
def api_accept_work_order(wo_id):
    """Technician accepts a work order that's been assigned to them."""
    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
    if wo is None:
        return jsonify({"error": "Work order not found"}), 404
    if wo['assigned_to'] != current_user.username:
        return jsonify({"error": "Forbidden: not assigned to you"}), 403
    if wo['status'] != 'ASSIGNED':
        return jsonify({"error": f"Cannot accept from status {wo['status']}"}), 400

    conn.execute(
        "UPDATE work_orders SET status='ACCEPTED', accepted_at=? WHERE wo_id=?",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), wo_id)
    )
    conn.commit()
    refresh_asset_status(wo['machine_id'])
    log_action(current_user.username, 'ACCEPT_WORK_ORDER', f'WO #{wo_id}', '-')
    return jsonify({"message": "Accepted", "status": "ACCEPTED"})


@app.route('/api/work-orders/<int:wo_id>/start', methods=['POST'])
@login_required
def api_start_work_order(wo_id):
    """Technician begins active maintenance work."""
    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
    if wo is None:
        return jsonify({"error": "Work order not found"}), 404
    if not current_user.is_admin and wo['assigned_to'] != current_user.username:
        return jsonify({"error": "Forbidden: not assigned to you"}), 403
    if wo['status'] != 'ACCEPTED':
        return jsonify({"error": f"Cannot start from status {wo['status']}"}), 400

    conn.execute(
        "UPDATE work_orders SET status='IN_PROGRESS', started_at=? WHERE wo_id=?",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), wo_id)
    )
    conn.commit()
    refresh_asset_status(wo['machine_id'])
    log_action(current_user.username, 'START_WORK_ORDER', f'WO #{wo_id}', '-')
    return jsonify({"message": "Started", "status": "IN_PROGRESS"})


@app.route('/api/work-orders/<int:wo_id>/notes', methods=['POST'])
@login_required
def api_save_technician_notes(wo_id):
    """Technician saves/updates process notes while actively working the WO."""
    data = request.get_json(silent=True) or {}
    notes = (data.get('technician_notes') or '').strip()

    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
    if wo is None:
        return jsonify({"error": "Work order not found"}), 404
    if not current_user.is_admin and wo['assigned_to'] != current_user.username:
        return jsonify({"error": "Forbidden: not assigned to you"}), 403
    if wo['status'] not in ('ACCEPTED', 'IN_PROGRESS'):
        return jsonify({"error": f"Cannot edit notes from status {wo['status']}"}), 400

    conn.execute("UPDATE work_orders SET technician_notes=? WHERE wo_id=?", (notes, wo_id))
    conn.commit()
    log_action(current_user.username, 'UPDATE_WO_NOTES', f'WO #{wo_id}', '-')
    return jsonify({"message": "Notes saved", "technician_notes": notes})


@app.route('/api/work-orders/<int:wo_id>/complete', methods=['POST'])
@login_required
def api_complete_work_order(wo_id):
    """Technician marks the maintenance done."""
    data = request.get_json(silent=True) or {}
    resolution_note = (data.get('resolution_note') or '').strip()
    if not resolution_note:
        return jsonify({"error": "resolution_note is required to complete a work order"}), 400

    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
    if wo is None:
        return jsonify({"error": "Work order not found"}), 404
    if not current_user.is_admin and wo['assigned_to'] != current_user.username:
        return jsonify({"error": "Forbidden: not assigned to you"}), 403
    if wo['status'] not in ('ACCEPTED', 'IN_PROGRESS'):
        return jsonify({"error": f"Cannot complete from status {wo['status']}"}), 400

    completed_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    conn.execute(
        "UPDATE work_orders SET status='PENDING_VERIFICATION', completed_at=?, resolution_note=? "
        "WHERE wo_id=?",
        (completed_at, resolution_note, wo_id)
    )
    conn.commit()
    log_action(current_user.username, 'COMPLETE_WORK_ORDER', f'WO #{wo_id}', 'Pending verification')

    create_notification(
        username=wo['created_by'],
        title=f"Work Order #{wo_id} awaiting verification",
        message=f"{current_user.username} completed the work — ready for review.",
        level="info", record_id=wo['record_id'], notif_type="verification_pending",
    )
    return jsonify({"message": "Marked complete, pending verification", "status": "PENDING_VERIFICATION"})


@app.route('/api/work-orders/<int:wo_id>/verify', methods=['POST'])
@login_required
def api_verify_work_order(wo_id):
    """Admin reviews the completed work and approves or rejects it."""
    data = request.get_json(silent=True) or {}
    decision = data.get('decision')
    verification_note = (data.get('verification_note') or '').strip()
    if decision not in ('approve', 'reject'):
        return jsonify({"error": "decision must be 'approve' or 'reject'"}), 400

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        if wo is None:
            conn.rollback()
            return jsonify({"error": "Work order not found"}), 404
        if not current_user.is_admin:
            conn.rollback()
            return jsonify({"error": "Forbidden: only an administrator can verify"}), 403
        if wo['status'] != 'PENDING_VERIFICATION':
            conn.rollback()
            return jsonify({"error": f"Cannot verify from status {wo['status']}"}), 400

        verified_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
        new_status = 'CLOSED' if decision == 'approve' else 'IN_PROGRESS'
        conn.execute(
            "UPDATE work_orders SET status=?, verified_by=?, verified_at=?, verification_note=? WHERE wo_id=?",
            (new_status, current_user.username, verified_at, verification_note or None, wo_id)
        )

        alert_resolved = False
        if decision == 'approve':
            conn.execute("""
                INSERT INTO maintenance_records
                    (asset_id, machine_id, maintenance_type, source_wo_id, description, performed_by, performed_at, created_at)
                VALUES (?, ?, 'CORRECTIVE', ?, ?, ?, ?, ?)
            """, (
                wo['asset_id'], wo['machine_id'], wo_id,
                wo['resolution_note'] or wo['title'],
                wo['assigned_to'] or current_user.username,
                wo['completed_at'] or verified_at, verified_at
            ))
            if wo['record_id']:
                alert = conn.execute(
                    "SELECT alert_status FROM decision_log WHERE record_id=?", (wo['record_id'],)
                ).fetchone()
                if alert and alert['alert_status'] not in ('RESOLVED', 'FALSE_POSITIVE'):
                    conn.execute(
                        "UPDATE decision_log SET alert_status='RESOLVED', resolved_by=?, resolved_at=?, resolution_note=? "
                        "WHERE record_id=? AND alert_status NOT IN ('RESOLVED', 'FALSE_POSITIVE')",
                        (current_user.username, verified_at,
                         verification_note or wo['resolution_note'] or 'Work order verified and closed.',
                         wo['record_id'])
                    )
                    alert_resolved = conn.execute("SELECT changes()").fetchone()[0] > 0

        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
            (verified_at, current_user.username, 'VERIFY_WORK_ORDER', f'WO #{wo_id}',
             f'{decision} -> {new_status}')
        )
        if alert_resolved:
            conn.execute(
                "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
                (verified_at, current_user.username, 'UPDATE_ALERT', f"Record #{wo['record_id']}",
                 f'IN_PROGRESS->RESOLVED; verified by closing WO #{wo_id}')
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500

    if decision == 'approve':
        refresh_asset_status(wo['machine_id'])
    _clear_response_cache()

    if wo['assigned_to']:
        create_notification(
            username=wo['assigned_to'],
            title=f"Work Order #{wo_id} {'closed' if decision == 'approve' else 'sent back for rework'}",
            message=verification_note or (
                "Verified and closed." if decision == 'approve' else "Please review and redo."),
            level="info" if decision == 'approve' else "warning",
            record_id=wo['record_id'],
            notif_type="wo_closed" if decision == 'approve' else "wo_rejected",
        )
    if alert_resolved:
        create_notification(
            username=None,
            title=f"Alert #{wo['record_id']} Resolved",
            message=f"Work Order #{wo_id} was verified and closed by {current_user.username}.",
            level="info", record_id=wo['record_id'], notif_type="resolved",
        )
    return jsonify({"message": f"Work order {decision}d", "status": new_status})

@app.route('/api/work-orders/<int:wo_id>/assign', methods=['POST'])
@login_required
def api_assign_work_order(wo_id):
    """Assign/reassign a work order to a technician."""
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('technician') or '').strip()
        conn = get_db()
        wo = conn.execute("SELECT status, assigned_to FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        if wo is None:
            return jsonify({"error": "Work order not found"}), 404

        if current_user.is_admin:
            if wo['status'] not in ('OPEN', 'ASSIGNED'):
                return jsonify({"error": f"Cannot assign or reassign from status {wo['status']}"}), 400
        else:
            if username != current_user.username:
                return jsonify({"error": "Forbidden: technicians may only claim work for themselves"}), 403
            if wo['status'] != 'OPEN' or wo['assigned_to'] is not None:
                return jsonify({"error": "Only an unassigned open work order can be claimed"}), 400

        tech = conn.execute(
            "SELECT username FROM users WHERE username=? AND role='technician' AND is_active=1",
            (username,)
        ).fetchone()
        if tech is None:
            return jsonify({"error": "Invalid technician"}), 400

        new_status = 'ASSIGNED'
        if wo['assigned_to'] == tech['username'] and wo['status'] == new_status:
            return jsonify({"message": "Already assigned", "assigned_to": tech['username'],
                            "status": new_status})
        conn.execute("UPDATE work_orders SET assigned_to=?, status=? WHERE wo_id=?",
                     (tech['username'], new_status, wo_id))
        conn.commit()
        create_notification(
            username=tech['username'],
            title=f"Work Order #{wo_id} assigned to you",
            message="You've been assigned a new work order.",
            level="warning", notif_type="assignment",
        )
        log_action(current_user.username, 'ASSIGN_WORK_ORDER', f'WO #{wo_id}',
                   f"{wo['assigned_to'] or 'unassigned'}->{tech['username']}")
        return jsonify({"message": "Assigned", "assigned_to": tech['username'], "status": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════
# API ENDPOINTS: PM SCHEDULES
# ══════════════════════════════════════════

@app.route('/api/assets/<asset_id>/pm-schedules')
@login_required
def api_asset_pm_schedules(asset_id):
    """
    List PM schedules for this asset with live-computed status.
    ---
    responses:
      200:
        description: PM schedules with status/status_detail
      404:
        description: Asset not found
    """
    conn = get_db()
    asset = conn.execute("SELECT asset_id FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    rows = conn.execute("SELECT * FROM pm_schedules WHERE asset_id=? ORDER BY pm_id", (asset_id,)).fetchall()
    result = []
    for pm in rows:
        status, extra = compute_pm_status(pm)
        item = dict(pm)
        item['status'] = status
        item['status_detail'] = extra
        result.append(item)
    return jsonify(result)


@app.route('/api/pm-schedules', methods=['POST'])
@login_required
@role_required('admin')
def api_create_pm_schedule():
    """
    Create a PM schedule for an asset.
    ---
    responses:
      201:
        description: PM schedule created
      400:
        description: Missing/invalid field
      404:
        description: Asset not found
    """
    data = request.get_json(silent=True) or {}
    asset_id = data.get('asset_id')
    pm_name = (data.get('pm_name') or '').strip()
    frequency_type = data.get('frequency_type')
    frequency_value = data.get('frequency_value')
    if not asset_id or not pm_name:
        return jsonify({"error": "asset_id and pm_name are required"}), 400
    if frequency_type not in ('DAYS', 'TOOL_WEAR'):
        return jsonify({"error": "frequency_type must be 'DAYS' or 'TOOL_WEAR'"}), 400
    try:
        frequency_value = int(frequency_value)
        if frequency_value <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"error": "frequency_value must be a positive integer"}), 400
    conn = get_db()
    asset = conn.execute("SELECT machine_id FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    next_due_at = (now + timedelta(days=frequency_value)).isoformat(timespec='seconds') if frequency_type == 'DAYS' else None
    current_wear = get_latest_tool_wear(asset['machine_id']) if frequency_type == 'TOOL_WEAR' else None
    cur = conn.execute("""
        INSERT INTO pm_schedules
            (asset_id, machine_id, pm_name, frequency_type, frequency_value,
             last_performed_tool_wear, next_due_at, status, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ON_SCHEDULE', ?, ?)
    """, (asset_id, asset['machine_id'], pm_name, frequency_type, frequency_value,
          current_wear, next_due_at, current_user.username, now.isoformat(timespec='seconds')))
    conn.commit()
    pm_id = cur.lastrowid
    log_action(current_user.username, 'CREATE_PM_SCHEDULE', f'PM #{pm_id}', f'asset={asset_id}')
    return jsonify({"message": "PM schedule created", "pm_id": pm_id}), 201


@app.route('/api/pm-schedules/<int:pm_id>/complete', methods=['POST'])
@login_required
def api_complete_pm_schedule(pm_id):
    """
    Mark a PM as performed today — resets the due baseline and auto-logs
    a maintenance_records entry.
    ---
    responses:
      200:
        description: PM marked complete
      404:
        description: PM schedule not found
    """
    data = request.get_json(silent=True) or {}
    note = (data.get('note') or '').strip()
    conn = get_db()
    pm = conn.execute("SELECT * FROM pm_schedules WHERE pm_id=?", (pm_id,)).fetchone()
    if pm is None:
        return jsonify({"error": "PM schedule not found"}), 404
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current_wear = get_latest_tool_wear(pm['machine_id']) if pm['frequency_type'] == 'TOOL_WEAR' else None
    next_due_at = compute_next_pm_due(pm, now)
    conn.execute("""
        UPDATE pm_schedules SET last_performed_at=?, last_performed_tool_wear=?, next_due_at=?, status='ON_SCHEDULE'
        WHERE pm_id=?
    """, (now.isoformat(timespec='seconds'), current_wear, next_due_at, pm_id))
    conn.execute("""
        INSERT INTO maintenance_records
            (asset_id, machine_id, maintenance_type, source_pm_id, description, performed_by, performed_at, created_at)
        VALUES (?, ?, 'PREVENTIVE', ?, ?, ?, ?, ?)
    """, (pm['asset_id'], pm['machine_id'], pm_id, note or f"Completed PM: {pm['pm_name']}",
          current_user.username, now.isoformat(timespec='seconds'), now.isoformat(timespec='seconds')))
    conn.commit()
    refresh_asset_status(pm['machine_id'])
    log_action(current_user.username, 'COMPLETE_PM_SCHEDULE', f'PM #{pm_id}', '-')
    return jsonify({"message": "PM marked complete", "next_due_at": next_due_at})


@app.route('/api/pm-schedules/<int:pm_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def api_delete_pm_schedule(pm_id):
    """Delete a PM schedule (admin only)."""
    conn = get_db()
    pm = conn.execute("SELECT pm_name FROM pm_schedules WHERE pm_id=?", (pm_id,)).fetchone()
    if pm is None:
        return jsonify({"error": "PM schedule not found"}), 404
    conn.execute("DELETE FROM pm_schedules WHERE pm_id=?", (pm_id,))
    conn.commit()
    log_action(current_user.username, 'DELETE_PM_SCHEDULE', f'PM #{pm_id}', pm['pm_name'])
    return jsonify({"message": "PM schedule deleted"})


# ══════════════════════════════════════════
# API ENDPOINTS: MAINTENANCE RECORDS
# ══════════════════════════════════════════

@app.route('/api/assets/<asset_id>/maintenance-records')
@login_required
def api_asset_maintenance_records(asset_id):
    """
    Get maintenance history for an asset, newest first.
    ---
    responses:
      200:
        description: List of maintenance records
      404:
        description: Asset not found
    """
    conn = get_db()
    if conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)).fetchone() is None:
        return jsonify({"error": "Asset not found"}), 404
    df = pd.read_sql(
        "SELECT * FROM maintenance_records WHERE asset_id=? ORDER BY performed_at DESC",
        conn, params=[asset_id]
    )
    return jsonify(df_to_records(df))


@app.route('/api/maintenance-records', methods=['POST'])
@login_required
def api_create_maintenance_record():
    """
    Manually log a maintenance record not tied to a Work Order or PM.
    ---
    responses:
      201:
        description: Maintenance record created
      400:
        description: Missing required field
      404:
        description: Asset not found
    """
    data = request.get_json(silent=True) or {}
    asset_id = data.get('asset_id')
    description = (data.get('description') or '').strip()
    maintenance_type = data.get('maintenance_type', 'CORRECTIVE')
    if not asset_id or not description:
        return jsonify({"error": "asset_id and description are required"}), 400
    if maintenance_type not in ('CORRECTIVE', 'PREVENTIVE', 'INSPECTION'):
        return jsonify({"error": "Invalid maintenance_type"}), 400
    conn = get_db()
    asset = conn.execute("SELECT machine_id FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    cur = conn.execute("""
        INSERT INTO maintenance_records
            (asset_id, machine_id, maintenance_type, description, performed_by, performed_at, downtime_minutes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (asset_id, asset['machine_id'], maintenance_type, description,
          current_user.username, now, data.get('downtime_minutes'), now))
    conn.commit()
    maint_id = cur.lastrowid
    log_action(current_user.username, 'CREATE_MAINTENANCE_RECORD', f'Maint #{maint_id}', f'asset={asset_id}')
    return jsonify({"message": "Maintenance record created", "maint_id": maint_id}), 201


# ══════════════════════════════════════════
# API ENDPOINTS: DOWNTIME EVENTS
# ══════════════════════════════════════════

@app.route('/api/assets/<asset_id>/downtime')
@login_required
def api_asset_downtime(asset_id):
    """
    Get downtime events for an asset, newest first.
    ---
    responses:
      200:
        description: List of downtime events
      404:
        description: Asset not found
    """
    conn = get_db()
    if conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)).fetchone() is None:
        return jsonify({"error": "Asset not found"}), 404
    df = pd.read_sql(
        "SELECT * FROM downtime_events WHERE asset_id=? ORDER BY start_time DESC", conn, params=[asset_id]
    )
    return jsonify(df_to_records(df))


@app.route('/api/downtime/start', methods=['POST'])
@login_required
def api_start_downtime():
    """
    Log the start of a downtime event; immediately forces the asset to DOWN.
    ---
    responses:
      201:
        description: Downtime started
      400:
        description: Missing field or already down
      404:
        description: Asset not found
    """
    data = request.get_json(silent=True) or {}
    asset_id = data.get('asset_id')
    reason_category = data.get('reason_category')
    allowed = ('UNPLANNED_FAILURE', 'PLANNED_MAINTENANCE', 'CHANGEOVER', 'OTHER')
    if not asset_id or reason_category not in allowed:
        return jsonify({"error": f"asset_id required, reason_category must be one of {allowed}"}), 400
    conn = get_db()
    asset = conn.execute("SELECT machine_id FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    existing_open = conn.execute(
        "SELECT downtime_id FROM downtime_events WHERE asset_id=? AND end_time IS NULL", (asset_id,)
    ).fetchone()
    if existing_open:
        return jsonify({"error": "This asset already has an open downtime event",
                         "downtime_id": existing_open['downtime_id']}), 400
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
    cur = conn.execute("""
        INSERT INTO downtime_events
            (asset_id, machine_id, start_time, reason_category, reason_detail, linked_wo_id, reported_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (asset_id, asset['machine_id'], now, reason_category,
          data.get('reason_detail'), data.get('linked_wo_id'), current_user.username, now))
    conn.commit()
    downtime_id = cur.lastrowid
    refresh_asset_status(asset['machine_id'])
    log_action(current_user.username, 'START_DOWNTIME', f'Downtime #{downtime_id}', f'asset={asset_id}')
    create_notification(
        username=None, title=f"Machine Down — {asset_id}",
        message=f"{current_user.username} logged downtime: {reason_category}",
        level="critical", notif_type="critical_alert",
    )
    return jsonify({"message": "Downtime started", "downtime_id": downtime_id}), 201


@app.route('/api/downtime/<int:downtime_id>/end', methods=['POST'])
@login_required
def api_end_downtime(downtime_id):
    """
    Close a downtime event and re-evaluate the asset's status.
    ---
    responses:
      200:
        description: Downtime ended
      400:
        description: Already closed
      404:
        description: Not found
    """
    conn = get_db()
    dt = conn.execute("SELECT * FROM downtime_events WHERE downtime_id=?", (downtime_id,)).fetchone()
    if dt is None:
        return jsonify({"error": "Downtime event not found"}), 404
    if dt['end_time'] is not None:
        return jsonify({"error": "This downtime event is already closed"}), 400
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    duration_minutes = int((now - datetime.fromisoformat(dt['start_time'])).total_seconds() // 60)
    conn.execute(
        "UPDATE downtime_events SET end_time=?, duration_minutes=? WHERE downtime_id=?",
        (now.isoformat(timespec='seconds'), duration_minutes, downtime_id)
    )
    conn.commit()
    new_status = refresh_asset_status(dt['machine_id'])
    log_action(current_user.username, 'END_DOWNTIME', f'Downtime #{downtime_id}', f'{duration_minutes} min')
    return jsonify({"message": "Downtime ended", "duration_minutes": duration_minutes, "asset_status": new_status})


# ══════════════════════════════════════════
# API ENDPOINT: FAILURE HISTORY
# ══════════════════════════════════════════

@app.route('/api/assets/<asset_id>/failure-history')
@login_required
def api_asset_failure_history(asset_id):
    """
    Get historical failures (actual_failure=1) for this asset's machine.
    ---
    responses:
      200:
        description: Failure records for this asset
      404:
        description: Asset not found
    """
    conn = get_db()
    asset = conn.execute("SELECT asset_id, machine_id FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        return jsonify({"error": "Asset not found"}), 404
    machine_id = asset['machine_id']
    try:
        machine_idx = int(machine_id.split('-')[1])
    except (IndexError, ValueError):
        return jsonify({"error": f"Malformed machine_id '{machine_id}'"}), 500
    target_remainder = (machine_idx - 1) % NUM_MACHINES
    df = pd.read_sql("""
        SELECT record_id, timestamp, air_temp, process_temp, rpm, torque, tool_wear,
               hdf_risk, pwf_risk, osf_risk, risk_score, risk_level, action, reasons
        FROM decision_log
        WHERE record_id % ? = ? AND actual_failure = 1
        ORDER BY record_id DESC
    """, conn, params=[NUM_MACHINES, target_remainder])
    return jsonify({"asset_id": asset['asset_id'], "machine_id": machine_id,
                    "count": len(df), "failures": df_to_records(df)})


# ══════════════════════════════════════════
# API ENDPOINTS: EXISTING MODULES
# ══════════════════════════════════════════

@app.route('/api/assign/<int:record_id>', methods=['POST'])
@login_required
def assign_alert(record_id):
    try:
        data = request.get_json(silent=True) or {}
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT assigned_to, alert_status FROM decision_log WHERE record_id=?",
            (record_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return jsonify({"error": "Record not found"}), 404
        if row['alert_status'] in ('RESOLVED', 'FALSE_POSITIVE'):
            conn.rollback()
            return jsonify({"error": "Cannot assign or reassign a closed alert"}), 400

        if current_user.is_admin:
            target_username = (data.get('technician') or '').strip()
            if not target_username:
                conn.rollback()
                return jsonify({"error": "technician username required"}), 400
            tech = conn.execute(
                "SELECT username FROM users WHERE username=? AND role='technician' AND is_active=1",
                (target_username,)
            ).fetchone()
            if tech is None:
                conn.rollback()
                return jsonify({"error": "Invalid technician"}), 400
            new_assignee = tech['username']
        else:
            if row['assigned_to']:
                conn.rollback()
                return jsonify({"error": "Alert is already assigned"}), 403
            new_assignee = current_user.username

        new_status = row['alert_status']
        if new_status in ('OPEN', 'OK'):
            new_status = 'ASSIGNED'

        if row['assigned_to'] == new_assignee:
            if row['alert_status'] != new_status:
                conn.execute(
                    "UPDATE decision_log SET alert_status=? WHERE record_id=?",
                    (new_status, record_id)
                )
                conn.execute(
                    "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), current_user.username,
                     'UPDATE_ALERT', f'Record #{record_id}', f"{row['alert_status']}->{new_status}")
                )
                conn.commit()
                _clear_response_cache()
            else:
                conn.rollback()
            return jsonify({"message": "Already assigned", "assigned_to": new_assignee,
                            "alert_status": new_status})

        conn.execute(
            "UPDATE decision_log SET assigned_to=?, alert_status=? WHERE record_id=?",
            (new_assignee, new_status, record_id)
        )
        previous_assignee = row['assigned_to']
        details = f'previous_assignee={previous_assignee or "(unassigned)"}; new_assignee={new_assignee}'
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), current_user.username,
             'ASSIGN_ALERT', f'Record #{record_id}', details)
        )
        conn.commit()
        _clear_response_cache()
        create_notification(
            username=new_assignee,
            title=f"Alert #{record_id} assigned to you",
            message=f"You've been assigned to investigate record #{record_id}.",
            level="warning",
            record_id=record_id,
            notif_type="assignment",
        )
        return jsonify({
            "message": f"Record {record_id} assigned to {new_assignee}.",
            "assigned_to": new_assignee,
            "alert_status": new_status
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/production-lines')
@login_required
def api_production_lines():
    try:
        conn = get_db()
        lines = conn.execute("SELECT * FROM production_lines ORDER BY line_id").fetchall()
        latest_decisions = _latest_decisions_by_machine(conn)
        result = []
        for line in lines:
            assets = conn.execute(
                "SELECT * FROM assets WHERE line_id=? ORDER BY machine_id", (line['line_id'],)
            ).fetchall()
            snapshots = [_machine_snapshot(conn, asset, latest_decisions) for asset in assets]
            status_counts = {}
            for machine in snapshots:
                status_counts[machine['status']] = status_counts.get(machine['status'], 0) + 1
            line_status = (
                'DOWN' if status_counts.get('DOWN', 0) else
                'WARNING' if status_counts.get('WARNING', 0) or status_counts.get('MAINTENANCE', 0) else
                'RUNNING'
            )
            risk_counts = {}
            for machine in snapshots:
                if machine['risk_level']:
                    risk_counts[machine['risk_level']] = risk_counts.get(machine['risk_level'], 0) + 1
            result.append({
                **dict(line),
                'status': line_status,
                "asset_count": sum(status_counts.values()),
                "status_counts": status_counts,
                "risk_counts": risk_counts,
                "kpis": _kpis_for_machines(conn, [a['machine_id'] for a in assets]),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/factory-overview')
@login_required
def api_factory_overview():
    """Return the complete factory -> line -> machine digital-twin snapshot."""
    try:
        conn = get_db()
        latest_decisions = _latest_decisions_by_machine(conn)
        assets = conn.execute("SELECT * FROM assets ORDER BY line_id, machine_id").fetchall()
        machines = [_machine_snapshot(conn, asset, latest_decisions) for asset in assets]
        lines = []
        for line in conn.execute("SELECT * FROM production_lines ORDER BY line_id").fetchall():
            line_machines = [m for m in machines if m['line_id'] == line['line_id']]
            status_counts = {}
            risk_counts = {}
            maintenance_counts = {}
            for machine in line_machines:
                status_counts[machine['status']] = status_counts.get(machine['status'], 0) + 1
                if machine['risk_level']:
                    risk_counts[machine['risk_level']] = risk_counts.get(machine['risk_level'], 0) + 1
                maintenance_counts[machine['maintenance_status']] = maintenance_counts.get(
                    machine['maintenance_status'], 0
                ) + 1
            line_status = (
                'DOWN' if status_counts.get('DOWN', 0) else
                'WARNING' if status_counts.get('WARNING', 0) or status_counts.get('MAINTENANCE', 0) else
                'RUNNING'
            )
            lines.append({
                **dict(line),
                'status': line_status,
                'asset_count': len(line_machines),
                'status_counts': status_counts,
                'risk_counts': risk_counts,
                'maintenance_counts': maintenance_counts,
                'kpis': _kpis_for_machines(conn, [m['machine_id'] for m in line_machines]),
                'machines': line_machines,
            })

        all_machine_ids = [m['machine_id'] for m in machines]
        status_summary = {}
        risk_summary = {}
        maintenance_summary = {}
        for machine in machines:
            status_summary[machine['status']] = status_summary.get(machine['status'], 0) + 1
            if machine['risk_level']:
                risk_summary[machine['risk_level']] = risk_summary.get(machine['risk_level'], 0) + 1
            maintenance_summary[machine['maintenance_status']] = maintenance_summary.get(
                machine['maintenance_status'], 0
            ) + 1
        return jsonify({
            'factory': {
                'name': 'Factory Operations Centre',
                'machine_count': len(machines),
                'line_count': len(lines),
                'status_summary': status_summary,
                'risk_summary': risk_summary,
                'maintenance_summary': maintenance_summary,
                'kpis': _kpis_for_machines(conn, all_machine_ids),
                'failure_trend': _failure_trend(conn, all_machine_ids),
                'data_source': 'Persisted SQLite decision_log and downtime_events (AI4I dataset-derived)',
                'last_updated': conn.execute(
                    "SELECT MAX(timestamp) t FROM decision_log"
                ).fetchone()['t'],
            },
            'lines': lines,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/production-lines/<line_id>')
@login_required
def api_production_line_detail(line_id):
    try:
        conn = get_db()
        line = conn.execute("SELECT * FROM production_lines WHERE line_id=?", (line_id,)).fetchone()
        if line is None:
            return jsonify({"error": "Production line not found"}), 404
        assets = conn.execute("SELECT * FROM assets WHERE line_id=? ORDER BY asset_id", (line_id,)).fetchall()
        latest_decisions = _latest_decisions_by_machine(conn)
        snapshots = [_machine_snapshot(conn, asset, latest_decisions) for asset in assets]
        status_counts = {}
        risk_counts = {}
        maintenance_counts = {}
        for machine in snapshots:
            status_counts[machine['status']] = status_counts.get(machine['status'], 0) + 1
            if machine['risk_level']:
                risk_counts[machine['risk_level']] = risk_counts.get(machine['risk_level'], 0) + 1
            maintenance_counts[machine['maintenance_status']] = maintenance_counts.get(
                machine['maintenance_status'], 0
            ) + 1
        line_status = (
            'DOWN' if status_counts.get('DOWN', 0) else
            'WARNING' if status_counts.get('WARNING', 0) or status_counts.get('MAINTENANCE', 0) else
            'RUNNING'
        )
        return jsonify({
            **dict(line),
            'status': line_status,
            'assets': snapshots,
            'machines': snapshots,
            'status_counts': status_counts,
            'risk_counts': risk_counts,
            'maintenance_counts': maintenance_counts,
            'kpis': _kpis_for_machines(conn, [a['machine_id'] for a in assets]),
            'failure_trend': _failure_trend(conn, [a['machine_id'] for a in assets]),
            'data_source': 'Persisted SQLite decision_log and downtime_events (AI4I dataset-derived)',
            'last_updated': conn.execute(
                "SELECT MAX(timestamp) t FROM decision_log"
            ).fetchone()['t'],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets')
@login_required
def api_assets():
    try:
        conn = get_db()
        line_id = request.args.get('line_id')
        if line_id:
            assets = conn.execute(
                "SELECT * FROM assets WHERE line_id=? ORDER BY asset_id", (line_id,)
            ).fetchall()
        else:
            assets = conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()
        latest_decisions = _latest_decisions_by_machine(conn)
        return jsonify([_machine_snapshot(conn, asset, latest_decisions) for asset in assets])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets/<asset_id>')
@login_required
def api_asset_detail(asset_id):
    try:
        conn = get_db()
        asset = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if asset is None:
            return jsonify({"error": "Asset not found"}), 404
        line = conn.execute(
            "SELECT * FROM production_lines WHERE line_id=?", (asset['line_id'],)
        ).fetchone()
        latest_decisions = _latest_decisions_by_machine(conn)
        snapshot = _machine_snapshot(conn, asset, latest_decisions)
        return jsonify({**snapshot, "line": dict(line) if line else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets/<asset_id>/observations')
@login_required
def api_asset_observations(asset_id):
    try:
        conn = get_db()
        asset = conn.execute(
            "SELECT asset_id, machine_id FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if asset is None:
            return jsonify({"error": "Asset not found"}), 404

        machine_id = asset['machine_id']
        try:
            machine_idx = int(machine_id.split('-')[1])
        except (IndexError, ValueError):
            return jsonify({"error": f"Malformed machine_id '{machine_id}'"}), 500

        limit = request.args.get('limit', 20, type=int)
        limit = max(1, min(limit, 200))

        target_remainder = (machine_idx - 1) % NUM_MACHINES

        df = pd.read_sql(
            """
            SELECT record_id, timestamp, air_temp, process_temp, rpm, torque,
                   tool_wear, power, pred_prob, risk_score, risk_level,
                   action, alert_status, assigned_to
            FROM decision_log
            WHERE record_id % ? = ?
            ORDER BY record_id DESC
            LIMIT ?
            """,
            conn, params=[NUM_MACHINES, target_remainder, limit]
        )

        return jsonify({
            "asset_id": asset['asset_id'],
            "machine_id": machine_id,
            "count": len(df),
            "observations": df_to_records(df)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/assets/<asset_id>/work-orders')
@login_required
def api_asset_work_orders(asset_id):
    try:
        conn = get_db()
        asset = conn.execute(
            "SELECT asset_id, machine_id FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if asset is None:
            return jsonify({"error": "Asset not found"}), 404

        df = pd.read_sql(
            "SELECT wo_id, title, priority, status, assigned_to, due_date, "
            "created_at, record_id FROM work_orders WHERE machine_id=? "
            "ORDER BY created_at DESC",
            conn, params=[asset['machine_id']]
        )
        return jsonify(df_to_records(df))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/summary')
@login_required
def api_summary():
    try:
        cached = _cached_response('summary')
        if cached is not None:
            return jsonify(cached)
        conn = get_db()
        distribution = conn.execute(
            "SELECT risk_level, COUNT(*) AS count FROM decision_log GROUP BY risk_level"
        ).fetchall()
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN alert_status='OPEN' THEN 1 ELSE 0 END) AS open_alerts,
                   SUM(CASE WHEN risk_level IN ('High','Critical') THEN 1 ELSE 0 END) AS high_risk
            FROM decision_log
        """).fetchone()
        payload = {
            "total_records":  int(row['total'] or 0),
            "open_alerts":    int(row['open_alerts'] or 0),
            "high_risk":      int(row['high_risk'] or 0),
            "distribution":   [dict(item) for item in distribution]
        }
        return jsonify(_store_cached_response('summary', payload))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/work-orders/summary')
@login_required
def api_work_orders_summary():
    try:
        conn = get_db()
        df = pd.read_sql("SELECT status, COUNT(*) c FROM work_orders GROUP BY status", conn)
        counts = dict(zip(df['status'], df['c']))

        open_count = (
            counts.get('OPEN', 0)
            + counts.get('ASSIGNED', 0)
            + counts.get('ACCEPTED', 0)
            + counts.get('IN_PROGRESS', 0)
            + counts.get('PENDING_VERIFICATION', 0)
        )

        overdue = conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE due_date < ? "
            "AND status NOT IN ('CLOSED', 'CANCELLED')",
            (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),)
        ).fetchone()['c']

        return jsonify({
            "open_work_orders": open_count,
            "overdue_work_orders": overdue,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/work-orders/my-summary')
@login_required
def api_my_work_orders_summary():
    if current_user.is_admin:
        return jsonify({"error": "Only available to technicians"}), 403

    conn = get_db()
    rows = conn.execute(
        "SELECT status FROM work_orders WHERE assigned_to=?",
        (current_user.username,)
    ).fetchall()
    statuses = [r['status'] for r in rows]
    open_statuses = ('OPEN', 'ASSIGNED', 'ACCEPTED', 'IN_PROGRESS', 'PENDING_VERIFICATION')
    pending = sum(1 for s in statuses if s in open_statuses)
    awaiting_verification = sum(1 for s in statuses if s == 'PENDING_VERIFICATION')
    overdue = conn.execute(
        "SELECT COUNT(*) c FROM work_orders WHERE assigned_to=? AND due_date < ? "
        "AND status NOT IN ('CLOSED','CANCELLED')",
        (current_user.username, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'))
    ).fetchone()['c']
    return jsonify({
        "my_pending": pending,
        "my_awaiting_verification": awaiting_verification,
        "my_overdue": overdue,
    })

@app.route('/api/metrics')
@login_required
def api_metrics():
    try:
        cached = _cached_response('metrics')
        if cached is not None:
            return jsonify(cached)
        conn = get_db()
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN alert_status='RESOLVED' THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN alert_status='FALSE_POSITIVE' THEN 1 ELSE 0 END) AS false_positives,
                   SUM(CASE WHEN alert_status='OPEN' THEN 1 ELSE 0 END) AS open_alerts,
                   SUM(CASE WHEN alert_status='IN_PROGRESS' THEN 1 ELSE 0 END) AS in_progress,
                   SUM(CASE WHEN risk_level IN ('High','Critical') THEN 1 ELSE 0 END) AS high_risk,
                   AVG(risk_score) AS avg_risk_score,
                   AVG(pred_prob) AS avg_pred_prob
            FROM decision_log
        """).fetchone()
        distribution = conn.execute(
            "SELECT risk_level, COUNT(*) AS count FROM decision_log GROUP BY risk_level"
        ).fetchall()
        trend_rows = conn.execute("""
            SELECT record_id, risk_score, pred_prob
            FROM decision_log
            WHERE record_id % 10 = 0
            ORDER BY record_id
        """).fetchall()
        total = int(row['total'] or 0)
        resolved = int(row['resolved'] or 0)
        fp = int(row['false_positives'] or 0)
        open_ = int(row['open_alerts'] or 0)
        in_progress = int(row['in_progress'] or 0)
        denom = resolved + open_ + in_progress
        payload = {
            "total_records": total,
            "open_alerts": open_,
            "in_progress": in_progress,
            "high_risk_count": int(row['high_risk'] or 0),
            "resolved_alerts": resolved,
            "false_positives": fp,
            "resolution_rate": f"{resolved / denom * 100:.1f}%" if denom > 0 else "0%",
            "avg_risk_score": round(float(row['avg_risk_score'] or 0), 1),
            "avg_pred_prob": round(float(row['avg_pred_prob'] or 0), 3),
            "distribution": [dict(item) for item in distribution],
            "trends": {
                "record_ids": [item['record_id'] for item in trend_rows],
                "risk_scores": [item['risk_score'] for item in trend_rows],
                "pred_probs": [round(float(item['pred_prob']), 3) for item in trend_rows],
            },
        }
        return jsonify(_store_cached_response('metrics', payload))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alerts')
@login_required
def api_alerts():
    try:
        conn = get_db()
        df = pd.read_sql("""
            SELECT record_id, pred_prob, risk_score, risk_level,
                   action, reasons, shap_explanation, alert_status, assigned_to, timestamp
            FROM decision_log
            WHERE risk_level IN ('Low','Medium','High','Critical')
            ORDER BY risk_score DESC
        """, conn)
        df['machine_id'] = df['record_id'].apply(machine_id_for_record)
        df['asset_id'] = df['machine_id'].apply(asset_id_for_machine)
        return jsonify(df_to_records(df))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/records')
@login_required
def api_records():
    try:
        limit = request.args.get('limit', 50, type=int)
        level = request.args.get('level', None)
        conn  = get_db()
        if level:
            df = pd.read_sql(
                "SELECT * FROM decision_log WHERE risk_level=? ORDER BY risk_score DESC LIMIT ?",
                conn, params=[level, limit]
            )
        else:
            df = pd.read_sql(
                "SELECT * FROM decision_log ORDER BY risk_score DESC LIMIT ?",
                conn, params=[limit]
            )
        return jsonify(df_to_records(df))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/record/<int:record_id>')
@login_required
def api_record(record_id):
    try:
        conn = get_db()
        df = pd.read_sql(
            "SELECT * FROM decision_log WHERE record_id=?",
            conn, params=[record_id]
        )

        if df.empty:
            return jsonify({"error": "Not found"}), 404

        shap_row = pd.read_sql(
            "SELECT * FROM shap_values WHERE record_id=?",
            conn, params=[record_id]
        )

        shap_list = df_to_records(shap_row)

        result = df_to_records(df)[0]
        result['shap'] = shap_list

        machine_id = machine_id_for_record(record_id)
        asset = conn.execute("SELECT * FROM assets WHERE machine_id=?", (machine_id,)).fetchone()
        result['machine_id'] = machine_id
        result['asset'] = dict(asset) if asset else None
        result['linked_work_orders'] = [
            dict(row) for row in conn.execute(
                "SELECT wo_id, status, assigned_to, created_at FROM work_orders "
                "WHERE record_id=? ORDER BY created_at DESC, wo_id DESC",
                (record_id,)
            ).fetchall()
        ]

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/update_status/<int:record_id>', methods=['POST'])
@login_required
def update_status(record_id):
    try:
        data = request.get_json(silent=True) or {}
        status  = data.get('status')
        note    = (data.get('note') or '').strip()
        allowed = ['OK', 'OPEN', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'FALSE_POSITIVE']
        if status not in allowed:
            return jsonify({"error": f"Invalid status. Must be one of {allowed}"}), 400

        conn = get_db()
        old_row = conn.execute(
            "SELECT alert_status, assigned_to FROM decision_log WHERE record_id=?",
            (record_id,)
        ).fetchone()
        if old_row is None:
            return jsonify({"error": "Record not found"}), 404

        if not current_user.is_admin and old_row['assigned_to'] != current_user.username:
            return jsonify({"error": "Forbidden: this alert is not assigned to you"}), 403

        old_status = old_row['alert_status']
        if old_status in ('RESOLVED', 'FALSE_POSITIVE') and status != old_status:
            return jsonify({"error": "Closed alerts must be reopened through the administrator Reopen Alert action"}), 400
        if old_status == status:
            return jsonify({"message": f"Record {record_id} is already {status}."})

        if status in ('RESOLVED', 'FALSE_POSITIVE'):
            resolved_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
            conn.execute(
                "UPDATE decision_log SET alert_status=?, resolved_by=?, resolved_at=?, resolution_note=? "
                "WHERE record_id=?",
                (status, current_user.username, resolved_at, note or None, record_id)
            )
        else:
            conn.execute(
                "UPDATE decision_log SET alert_status=?, resolved_by=NULL, resolved_at=NULL, "
                "resolution_note=NULL WHERE record_id=?",
                (status, record_id)
            )
        conn.commit()
        _clear_response_cache()
        if status == 'RESOLVED':
            create_notification(
                username=None,
                title=f"Alert #{record_id} Resolved",
                message=f"{current_user.username} marked record #{record_id} as RESOLVED.",
                level="info",
                record_id=record_id,
                notif_type="resolved",
            )
        elif status == 'FALSE_POSITIVE':
            create_notification(
                username=None,
                title=f"Alert #{record_id} False Positive",
                message=f"{current_user.username} marked record #{record_id} as FALSE_POSITIVE.",
                level="info",
                record_id=record_id,
                notif_type="false_positive",
            )
        log_action(current_user.username, 'UPDATE_ALERT', f'Record #{record_id}',
                   f'{old_status}→{status}')
        return jsonify({"message": f"Record {record_id} updated to {status}."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/reopen_alert/<int:record_id>', methods=['POST'])
@login_required
def reopen_alert(record_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden: only an administrator can reopen an alert"}), 403
    if not reason:
        return jsonify({"error": "A reason is required to reopen an alert"}), 400

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT alert_status, assigned_to FROM decision_log WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return jsonify({"error": "Record not found"}), 404
        if row['alert_status'] not in ('RESOLVED', 'FALSE_POSITIVE'):
            conn.rollback()
            return jsonify({"error": "Only a resolved or false-positive alert can be reopened"}), 400

        old_status = row['alert_status']
        new_status = 'ASSIGNED' if row['assigned_to'] else 'OPEN'
        reopened_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
        conn.execute(
            "UPDATE decision_log SET alert_status=?, resolved_by=NULL, resolved_at=NULL, resolution_note=NULL "
            "WHERE record_id=?",
            (new_status, record_id)
        )
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
            (reopened_at, current_user.username, 'REOPEN_ALERT', f'Record #{record_id}',
             f'{old_status}->{new_status}; reason={reason}')
        )
        conn.commit()
        _clear_response_cache()
        return jsonify({"message": f"Record {record_id} reopened.", "alert_status": new_status})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/record/<int:record_id>/history')
@login_required
def api_record_history(record_id):
    try:
        conn = get_db()
        target = f'Record #{record_id}'
        rows = conn.execute(
            "SELECT id, timestamp, username, action, details FROM audit_log "
            "WHERE target=? ORDER BY timestamp DESC, id DESC",
            (target,)
        ).fetchall()
        return jsonify([dict(row) for row in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/trends')
@login_required
def api_trends():
    try:
        cached = _cached_response('trends')
        if cached is not None:
            return jsonify(cached)
        conn = get_db()
        rows = conn.execute("""
            SELECT record_id, risk_score, pred_prob
            FROM decision_log
            WHERE record_id % 10 = 0
            ORDER BY record_id
        """).fetchall()
        payload = {
            "record_ids": [row['record_id'] for row in rows],
            "risk_scores": [row['risk_score'] for row in rows],
            "pred_probs": [round(float(row['pred_prob']), 3) for row in rows],
        }
        return jsonify(_store_cached_response('trends', payload))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/model-comparison')
@login_required
def api_model_comparison():
    try:
        cached = _cached_response('model-comparison')
        if cached is not None:
            return jsonify(cached)
        metrics_df = pd.read_csv(MODEL_METRICS_CSV)
        metrics = metrics_df.to_dict(orient='records')

        conn = get_db()
        df = pd.read_sql("""
            SELECT p.record_id,
                   p.pred_prob_lr,
                   p.pred_prob_rf,
                   p.model_agreement,
                   p.actual_failure,
                   d.risk_level,
                   d.risk_score,
                   d.action,
                   d.shap_explanation
            FROM predictions p
            JOIN decision_log d ON p.record_id = d.record_id
        """, conn)

        total = len(df)
        agree = int((df['model_agreement'] == 1).sum())
        disagree = total - agree

        df['lr_pred'] = (df['pred_prob_lr'] >= 0.5).astype(int)
        df['rf_pred'] = (df['pred_prob_rf'] >= 0.5).astype(int)

        rf_fail_lr_normal = int(((df['rf_pred'] == 1) & (df['lr_pred'] == 0)).sum())
        rf_normal_lr_fail = int(((df['rf_pred'] == 0) & (df['lr_pred'] == 1)).sum())

        lr_false_negatives = int(((df['actual_failure'] == 1) & (df['lr_pred'] == 0)).sum())
        rf_false_negatives = int(((df['actual_failure'] == 1) & (df['rf_pred'] == 0)).sum())
        lr_false_positives = int(((df['actual_failure'] == 0) & (df['lr_pred'] == 1)).sum())
        rf_false_positives = int(((df['actual_failure'] == 0) & (df['rf_pred'] == 1)).sum())

        def confusion_counts(actual, pred):
            return {
                "tp": int(((actual == 1) & (pred == 1)).sum()),
                "tn": int(((actual == 0) & (pred == 0)).sum()),
                "fp": int(((actual == 0) & (pred == 1)).sum()),
                "fn": int(((actual == 1) & (pred == 0)).sum()),
            }

        lr_confusion = confusion_counts(df['actual_failure'], df['lr_pred'])
        rf_confusion = confusion_counts(df['actual_failure'], df['rf_pred'])

        disagreements = (
            df[df['model_agreement'] == 0]
            .assign(prob_gap=lambda x: (x['pred_prob_rf'] - x['pred_prob_lr']).abs())
            .sort_values('prob_gap', ascending=False)
            .head(20)
        )

        payload = {
            "metrics": metrics,
            "total_records": total,
            "agreement_count": agree,
            "disagreement_count": disagree,
            "agreement_pct": f"{agree / total * 100:.1f}%" if total else "0%",
            "disagreement_pct": f"{disagree / total * 100:.1f}%" if total else "0%",
            "rf_failure_lr_normal": rf_fail_lr_normal,
            "rf_normal_lr_failure": rf_normal_lr_fail,
            "lr_false_negatives": lr_false_negatives,
            "rf_false_negatives": rf_false_negatives,
            "lr_false_positives": lr_false_positives,
            "rf_false_positives": rf_false_positives,
            "lr_confusion": lr_confusion,
            "rf_confusion": rf_confusion,
            "threshold": 0.5,
            "disagreements": disagreements.assign(
                prob_gap_pct=lambda x: (x['prob_gap'] * 100).round(1)
            )[[
                'record_id', 'pred_prob_lr', 'pred_prob_rf', 'lr_pred', 'rf_pred',
                'actual_failure', 'risk_level', 'risk_score', 'action', 'shap_explanation',
                'prob_gap_pct'
            ]].to_dict(orient='records')
        }
        return jsonify(_store_cached_response('model-comparison', payload))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/shap/global')
@login_required
def api_shap_global():
    try:
        if not os.path.exists(SHAP_IMPORTANCE_JSON):
            return jsonify({"error": "SHAP importance data not available. Train the model first."}), 404

        with open(SHAP_IMPORTANCE_JSON, 'r') as f:
            data = json.load(f)

        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/shap/highest-risk')
@login_required
def api_shap_highest_risk():
    try:
        conn = get_db()

        top_row = conn.execute("""
            SELECT record_id, risk_score, risk_level, pred_prob, action
            FROM decision_log
            ORDER BY risk_score DESC, pred_prob DESC
            LIMIT 1
        """).fetchone()

        if top_row is None:
            return jsonify({"error": "No records found"}), 404

        record_id = top_row['record_id']

        shap_row = conn.execute(
            "SELECT * FROM shap_values WHERE record_id=?", (record_id,)
        ).fetchone()

        if shap_row is None:
            return jsonify({"error": "No SHAP data for this record"}), 404

        shap_dict = dict(shap_row)
        for k in ('record_id', 'top_feature', 'top_shap_value'):
            shap_dict.pop(k, None)

        features = sorted(
            [{"feature": k, "shap_value": round(float(v), 5)} for k, v in shap_dict.items()],
            key=lambda x: abs(x["shap_value"]),
            reverse=True
        )

        return jsonify({
            "record_id": record_id,
            "risk_score": top_row['risk_score'],
            "risk_level": top_row['risk_level'],
            "pred_prob": round(top_row['pred_prob'], 4),
            "action": top_row['action'],
            "features": features
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/explainability')
@login_required
def api_explainability():
    try:
        conn = get_db()
        df = pd.read_sql(
            "SELECT actual_failure, pred_prob_lr, pred_prob_rf FROM predictions",
            conn
        )

        result = {}
        for key, col in [('lr', 'pred_prob_lr'), ('rf', 'pred_prob_rf')]:
            y_true = df['actual_failure']
            y_prob = df[col]
            fpr, tpr, thresholds = roc_curve(y_true, y_prob)
            auc = roc_auc_score(y_true, y_prob)

            clean_thresholds = [1.0 if t == float('inf') else round(float(t), 4)
                                for t in thresholds]

            result[key] = {
                "fpr": [round(float(x), 4) for x in fpr],
                "tpr": [round(float(x), 4) for x in tpr],
                "thresholds": clean_thresholds,
                "auc": round(float(auc), 4),
                "probs": [round(float(x), 4) for x in y_prob],
            }

        return jsonify({
            "actual": df['actual_failure'].astype(int).tolist(),
            "lr": result['lr'],
            "rf": result['rf'],
            "total_records": len(df),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/health')
@login_required
def api_health():
    start = time.perf_counter()
    db_health     = check_database_health()
    model_health  = check_model_health()
    system_health = check_system_resources()
    api_latency_ms = round((time.perf_counter() - start) * 1000, 1)

    statuses = [db_health["status"], model_health["status"], system_health.get("status", "down")]
    if "down" in statuses:
        overall = "down"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    health_score = compute_health_score("healthy", db_health["status"], model_health["status"], system_health.get("status", "down"))

    conn = get_db()
    last_updated_row = conn.execute("SELECT MAX(timestamp) t FROM decision_log").fetchone()
    db_last_updated = last_updated_row['t'] if last_updated_row and last_updated_row['t'] else None

    log_health_check(overall, api_latency_ms, db_health.get("latency_ms"))
    counts = conn.execute("""
        SELECT
            SUM(CASE WHEN overall_status = 'healthy' THEN 1 ELSE 0 END) AS successful,
            SUM(CASE WHEN overall_status != 'healthy' THEN 1 ELSE 0 END) AS failed,
            COUNT(*) AS total
        FROM health_log
    """).fetchone()

    return jsonify({
        "overall_status": overall,
        "health_score": health_score,
        "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'),
        "uptime": get_uptime_string(),
        "model_version": MODEL_VERSION,
        "api": {"status": "healthy", "latency_ms": api_latency_ms},
        "database": {
            **db_health,
            "engine": "SQLite",
            "record_count": model_health.get("decision_log_rows"),
            "last_updated": db_last_updated,
        },
        "model": model_health,
        "system": system_health,
        "checks": {
            "successful": counts["successful"] or 0,
            "failed": counts["failed"] or 0,
            "total": counts["total"] or 0,
        },
        "last_incident": get_last_incident(),
    })


@app.route('/api/health/history')
@login_required
def api_health_history():
    try:
        conn = get_db()
        df = pd.read_sql(
            "SELECT timestamp, overall_status, api_latency_ms FROM health_log ORDER BY id DESC LIMIT 30",
            conn
        )
        df = df.iloc[::-1]
        return jsonify(df.to_dict(orient='records'))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/stream')
@login_required
def api_stream():
    try:
        conn = get_db()
        total_row = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()
        total = int(total_row['c'] if total_row else 0)
        if total <= 0:
            return jsonify({"error": "No data"}), 404

        try:
            cursor = int(request.args.get('cursor', 0))
        except (TypeError, ValueError):
            cursor = 0
        idx = cursor % total
        df = pd.read_sql(
            "SELECT * FROM decision_log ORDER BY record_id LIMIT 1 OFFSET ?",
            conn, params=[idx]
        )
        row = df_to_records(df)[0] if not df.empty else None
        if row is None:
            return jsonify({"error": "No data"}), 404
        row['stream_index'] = idx
        row['total_records'] = int(total)
        row['next_stream_index'] = (idx + 1) % int(total)

        if row.get('risk_level') in ('High', 'Critical'):
            notif_type = 'critical_alert' if row['risk_level'] == 'Critical' else 'risk_escalated'
            with _notification_write_lock:
                if not _notification_exists_for_record(row['record_id'], notif_type):
                    create_notification(
                        username=None,
                        title=f"{row['risk_level']} Machine Alert",
                        message=f"Record #{row['record_id']} risk score reached {row['risk_score']}.",
                        level='critical' if row['risk_level'] == 'Critical' else 'warning',
                        record_id=row['record_id'],
                        notif_type=notif_type,
                    )

        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/export/csv')
@login_required
def export_csv():
    try:
        conn = get_db()
        df = pd.read_sql("""
            SELECT record_id, air_temp, process_temp, rpm, torque, tool_wear,
                   power, pred_prob, risk_score, risk_level, action,
                   reasons, shap_explanation, alert_status, timestamp
            FROM decision_log
            WHERE risk_level IN ('High','Critical')
            ORDER BY risk_score DESC
        """, conn)
        output = io.StringIO()
        df.to_csv(output, index=False)
        log_action(current_user.username, 'EXPORT_CSV', 'High/Critical records', f'{len(df)} rows')
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={"Content-Disposition": "attachment; filename=high_risk_records.csv"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/notifications')
@login_required
def api_notifications():
    try:
        conn = get_db()
        if current_user.is_admin:
            df = pd.read_sql(
                "SELECT * FROM notifications WHERE username=? OR username IS NULL "
                "ORDER BY id DESC LIMIT 100",
                conn, params=[current_user.username]
            )
        else:
            df = pd.read_sql(
                "SELECT * FROM notifications WHERE username=? ORDER BY id DESC LIMIT 100",
                conn, params=[current_user.username]
            )
        unread = int(df['read_at'].isna().sum())
        return jsonify({"notifications": df_to_records(df), "unread_count": unread})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def api_notification_read(notif_id):
    try:
        conn = get_db()
        conn.execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND (username=? OR username IS NULL)",
            (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds'), notif_id, current_user.username)
        )
        conn.commit()
        return jsonify({"message": "Marked as read"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_notifications_read_all():
    try:
        conn = get_db()
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')
        if current_user.is_admin:
            conn.execute(
                "UPDATE notifications SET read_at=? WHERE read_at IS NULL AND (username=? OR username IS NULL)",
                (now, current_user.username)
            )
        else:
            conn.execute(
                "UPDATE notifications SET read_at=? WHERE read_at IS NULL AND username=?",
                (now, current_user.username)
            )
        conn.commit()
        return jsonify({"message": "All marked as read"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/notifications/stream')
@login_required
def notifications_stream():
    username = current_user.username

    def gen():
        q = queue.Queue()
        with _notification_lock:
            _notification_subscribers.setdefault(username, []).append(q)
        try:
            yield "event: ping\ndata: connected\n\n"
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield "event: ping\ndata: keepalive\n\n"
        finally:
            with _notification_lock:
                subs = _notification_subscribers.get(username, [])
                if q in subs:
                    subs.remove(q)

    return Response(gen(), mimetype='text/event-stream')


# ══════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/factory')
@login_required
def factory_overview_page():
    return render_template('factory_overview.html')

@app.route('/alerts')
@login_required
def alerts():
    return render_template('alerts.html')

@app.route('/work-orders')
@login_required
def work_orders_page():
    return render_template('work_orders.html')

@app.route('/work-orders/<int:wo_id>')
@login_required
def work_order_detail_page(wo_id):
    return render_template('work_order_detail.html', wo_id=wo_id)

@app.route('/metrics')
@login_required
def metrics_page():
    return render_template('metrics.html')

@app.route('/model-comparison')
@login_required
def model_comparison_page():
    return render_template('model_comparison.html')

@app.route('/explainability')
@login_required
def explainability_page():
    return render_template('explainability.html')

@app.route('/health')
@login_required
def health_page():
    return render_template('health.html')

@app.route('/record/<int:record_id>')
@login_required
def record_detail(record_id):
    return render_template('record_detail.html', record_id=record_id)

@app.route('/records')
@login_required
def records_page():
    return render_template('records.html')

@app.route('/production-lines')
@login_required
def production_lines_page():
    return render_template('production_lines.html')

@app.route('/production-lines/<line_id>')
@login_required
def production_line_detail_page(line_id):
    return render_template('production_line_detail.html', line_id=line_id)

@app.route('/assets')
@login_required
def assets_page():
    return render_template('assets.html')

@app.route('/assets/<asset_id>')
@login_required
def asset_detail_page(asset_id):
    return render_template('asset_detail.html', asset_id=asset_id)

@app.route('/machine/<machine_id>')
@login_required
def machine_detail_page(machine_id):
    """Machine-facing alias used by the factory digital twin."""
    conn = get_db()
    asset = conn.execute("SELECT asset_id FROM assets WHERE machine_id=?", (machine_id,)).fetchone()
    if asset is None:
        return render_template('asset_detail.html', asset_id=machine_id), 404
    return render_template('asset_detail.html', asset_id=asset['asset_id'])

@app.route('/stream')
@login_required
def stream_page():
    return render_template('stream.html')

@app.route('/notifications')
@login_required
def notifications_page():
    return render_template('notifications.html')


if __name__ == '__main__':
    init_users_table()
    init_audit_log_table()
    init_health_log_table()
    init_assignment_column()
    init_resolution_columns()
    init_work_orders_table()
    init_workflow_columns()
    init_pm_schedules_table()
    init_maintenance_records_table()
    init_downtime_events_table()
    init_notifications_table()
    init_production_lines_table()
    init_assets_table()
    seed_assets_and_lines()
    seed_demo_maintenance_data()
    init_indexes()
    app.run(debug=False, threaded=True)
