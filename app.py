from flask import Flask, render_template, jsonify, request, g, redirect, url_for, flash
import sqlite3
import pandas as pd
import threading
from flasgger import Swagger
from flask import Response
import csv
import io
import os
import psutil
import time
import queue
import json
import math
from datetime import datetime, timedelta
from functools import wraps
from sklearn.metrics import roc_curve, roc_auc_score
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)

APP_START_TIME = datetime.utcnow()
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
    return (datetime.utcnow() + timedelta(hours=hours)).isoformat(timespec='seconds')

app = Flask(__name__)
DB = r"C:\Users\limli\Inti Folder\FYP\machine_monitor.db"
MODEL_METRICS_CSV = os.path.join(os.path.dirname(DB), "model_metrics.csv")
SHAP_IMPORTANCE_JSON = os.path.join(os.path.dirname(DB), "shap_importance.json")

app.config['SWAGGER'] = {'title': 'Predictive Maintenance API', 'uiversion': 3}
Swagger(app)

app.config['SECRET_KEY'] = 'change-this-to-a-long-random-secret-key'

bcrypt = Bcrypt(app)

# ══════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════

def get_raw_conn():
    """Shared helper for one-off connections outside the request context
    (init_* functions, create_notification, etc). WAL mode lets reads and
    writes happen concurrently instead of blocking each other, and
    busy_timeout makes a connection wait (instead of instantly erroring)
    if another connection briefly holds the write lock."""
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db

_notification_subscribers = {}
_notification_lock = threading.Lock()


def _is_admin_username(username):
    """Raw-connection role check (no flask.g dependency), safe to call
    from create_notification() even outside a request context."""
    conn = get_raw_conn()
    row = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row is not None and row[0] == 'admin'


def _push_to_subscribers(username, payload):
    with _notification_lock:
        if username is None:
            targets = []
            for uname, qs in _notification_subscribers.items():
                if _is_admin_username(uname):
                    targets.extend(qs)
        else:
            targets = list(_notification_subscribers.get(username, []))
    for q in targets:
        q.put(payload)


def create_notification(username, title, message, level='info', record_id=None, notif_type='system'):
    """
    Persist a notification (so it survives refresh/logout) AND push it
    live to any open SSE connection for that user.

    username=None  -> broadcast to every currently-connected admin
    username='bob' -> only bob sees it
    level:      'info' | 'warning' | 'critical'  (fallback color if type is unrecognized)
    notif_type: 'assignment' | 'resolved' | 'false_positive' | 'critical_alert'
                | 'risk_escalated' | 'system'  (drives icon + color in the UI)
    """
    conn = get_raw_conn()
    created_at = datetime.utcnow().isoformat(timespec='seconds')
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
    """Stream cycles through the same records repeatedly (record_id % total),
    so without this check the same alert would fire a fresh notification
    every single time it's re-streamed. Only notify once per record+type."""
    conn = get_raw_conn()
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE record_id=? AND type=? LIMIT 1",
        (record_id, notif_type)
    ).fetchone()
    conn.close()
    return row is not None

def df_to_records(df):
    """Convert a DataFrame to a list of dicts safe for jsonify —
    replaces pandas NaN (from SQL NULL) with None, since raw NaN
    is not valid JSON and breaks the browser's JSON.parse().

    IMPORTANT: must cast to object dtype FIRST. If a column is entirely
    NULL, pandas infers it as float64, and float64 columns cannot hold
    Python None — pandas silently coerces None back to NaN. Casting to
    object dtype first avoids this."""
    df = df.astype(object).where(pd.notnull(df), None)
    return df.to_dict(orient='records')

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_users_table():
    """Create the users table if it doesn't exist yet. Safe to call every startup."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'technician',   -- 'admin' or 'technician'
            is_active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    # Migration: add the column if this table already existed from before this feature.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()

def init_notifications_table():
    """Create the notifications table if it doesn't exist yet.
    username = NULL means a broadcast notification (all admins see it) —
    used for events nobody has personally claimed yet, e.g. an alert
    getting resolved, which every admin should be aware of.
    type drives the icon/color in the UI (assignment, resolved,
    false_positive, critical_alert, risk_escalated, system)."""
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
    # Migration: add the column if this table already existed from before this feature.
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN type TEXT NOT NULL DEFAULT 'system'")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()

def init_production_lines_table():
    """Create the production_lines table if it doesn't exist yet.
    Simulated application-layer data — NOT part of the AI4I dataset."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS production_lines (
            line_id TEXT PRIMARY KEY,      -- e.g. 'LINE-01'
            line_name TEXT NOT NULL,
            location TEXT,
            status TEXT NOT NULL DEFAULT 'RUNNING'   -- RUNNING | WARNING | DOWN
        )
    """)
    conn.commit()
    conn.close()


def init_assets_table():
    """Create the assets table if it doesn't exist yet.
    machine_id is the simulated stable machine identifier that groups
    many AI4I decision_log/record_id observations under one physical
    asset (see machine_id_for_record() for the mapping rule).
    Simulated application-layer data — NOT part of the AI4I dataset."""
    conn = get_raw_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,     -- e.g. 'AST-1005'
            machine_id TEXT UNIQUE NOT NULL,  -- e.g. 'MCH-005'
            machine_name TEXT NOT NULL,
            machine_type TEXT,
            line_id TEXT NOT NULL,
            location TEXT,
            manufacturer TEXT,
            model TEXT,
            criticality TEXT NOT NULL DEFAULT 'MEDIUM',  -- LOW | MEDIUM | HIGH
            status TEXT NOT NULL DEFAULT 'RUNNING',      -- RUNNING | WARNING | MAINTENANCE | DOWN
            FOREIGN KEY (line_id) REFERENCES production_lines(line_id)
        )
    """)
    conn.commit()
    conn.close()

def seed_assets_and_lines():
    """One-time seed of simulated production lines + assets, one asset
    per machine_id produced by machine_id_for_record(). Safe to call
    every startup — skips if data already exists."""
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

def init_assignment_column():
    """Add assigned_to column to decision_log if it doesn't exist yet."""
    conn = get_raw_conn()
    try:
        conn.execute("ALTER TABLE decision_log ADD COLUMN assigned_to TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


def init_resolution_columns():
    """Add resolved_by / resolved_at / resolution_note to decision_log if
    missing. Populated when an alert is marked RESOLVED or FALSE_POSITIVE,
    so record_detail can show who closed it, when, and why — without having
    to parse it back out of audit_log's free-text details column."""
    conn = get_raw_conn()
    for col_sql in [
        "ALTER TABLE decision_log ADD COLUMN resolved_by TEXT",
        "ALTER TABLE decision_log ADD COLUMN resolved_at TEXT",
        "ALTER TABLE decision_log ADD COLUMN resolution_note TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


def init_work_orders_table():
    """Create the work_orders table if it doesn't exist yet.
    A work order can originate from an AI alert (record_id set) or be
    created manually by an admin/technician (record_id NULL)."""
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
    """Week 3: technician-workflow columns for work_orders.
    accepted_at / started_at track the technician's progression;
    technician_notes is free-form process notes (editable while working);
    resolution_note is the mandatory final "what was actually fixed";
    verification_note/verified_by/verified_at record the reviewer's decision.
    Safe to call every startup."""
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
            pass  # column already exists
    conn.commit()
    conn.close()

def init_indexes():
    """Add indexes for the columns that get filtered/sorted on every
    dashboard/alerts/work-orders poll. Safe to call every startup —
    CREATE INDEX IF NOT EXISTS is a no-op once the index exists."""
    conn = get_raw_conn()
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_decision_log_risk_score ON decision_log(risk_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_decision_log_alert_status ON decision_log(alert_status)",
        "CREATE INDEX IF NOT EXISTS idx_decision_log_risk_level ON decision_log(risk_level)",
        "CREATE INDEX IF NOT EXISTS idx_work_orders_machine_id ON work_orders(machine_id)",
        "CREATE INDEX IF NOT EXISTS idx_work_orders_status ON work_orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_username ON notifications(username)",
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # table/column not ready yet, or index already exists
    conn.commit()
    conn.close()

# ══════════════════════════════════════════
# SYSTEM HEALTH
# ══════════════════════════════════════════

def check_database_health():
    """Runs a trivial query and times it. Returns dict with status + latency."""
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
    """Checks the offline-scored model artifacts: metrics CSV freshness +
    row-count consistency between decision_log and predictions."""
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
        # Metrics CSV freshness
        if os.path.exists(MODEL_METRICS_CSV):
            result["metrics_file_exists"] = True
            mtime = os.path.getmtime(MODEL_METRICS_CSV)
            result["metrics_last_updated"] = datetime.fromtimestamp(mtime).isoformat(timespec='seconds')
        else:
            result["status"] = "degraded"

        # Row count consistency
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
    """CPU + RAM usage via psutil. interval=None uses the delta since the
    last call instead of blocking the request thread for 100ms."""
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
    """Create the health_log table if it doesn't exist yet."""
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
    """Write one row to health_log, then trim table to the most recent 500 rows
    so it doesn't grow unbounded during long-running demos."""
    conn = get_db()
    conn.execute(
        "INSERT INTO health_log (timestamp, overall_status, api_latency_ms, db_latency_ms) VALUES (?, ?, ?, ?)",
        (datetime.utcnow().isoformat(timespec='seconds'), overall_status, api_latency_ms, db_latency_ms)
    )
    conn.execute("""
        DELETE FROM health_log WHERE id NOT IN (
            SELECT id FROM health_log ORDER BY id DESC LIMIT 500
        )
    """)
    conn.commit()

def get_last_incident():
    """Find the most recent non-healthy health_log entry, and whether the
    system has recovered since (a healthy check occurred after it)."""
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
    delta = datetime.utcnow() - APP_START_TIME
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def compute_health_score(api_status, db_status, model_status, system_status):
    """Each component contributes 25 points. 'healthy' = full, 'degraded' = half, 'down' = 0."""
    weights = {"healthy": 25, "degraded": 12.5, "down": 0}
    score = sum(weights.get(s, 0) for s in [api_status, db_status, model_status, system_status])
    return round(score)

def init_audit_log_table():
    """Create the audit_log table if it doesn't exist yet. Safe to call every startup."""
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
    """Write one row to audit_log. username is the actor performing the action
    (not necessarily the current_user — e.g. a failed login logs the attempted username)."""
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (timestamp, username, action, target, details) VALUES (?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(timespec='seconds'), username, action, target, details)
    )
    conn.commit()


# ══════════════════════════════════════════
# AUTH: User model + Flask-Login setup
# ══════════════════════════════════════════

class User(UserMixin):
    """Thin wrapper around a users-table row for Flask-Login."""
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
    """If the logged-in user still has must_change_password=1, force them to
    the change-password page before they can reach anything else."""
    allowed_endpoints = {'change_password', 'logout', 'static'}
    if current_user.is_authenticated and getattr(current_user, 'must_change_password', False):
        if request.endpoint not in allowed_endpoints:
            return redirect(url_for('change_password'))


def role_required(*allowed_roles):
    """RBAC decorator. Use AFTER @login_required, e.g.:
    @app.route('/admin/users')
    @login_required
    @role_required('admin')
    def manage_users(): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                return jsonify({"error": "Forbidden: insufficient role"}), 403
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


# ══════════════════════════════════════════
# AUTH ROUTES (UC1: Login)
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
                      (datetime.utcnow().isoformat(), user.id))
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
# ADMIN: Manage users (UC10, admin-only)
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
        (username, email, pw_hash, role, datetime.utcnow().isoformat())
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
    """Export filtered audit log entries as CSV."""
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
    """
    Get list of active technicians for assignment dropdown.
    ---
    responses:
      200:
        description: List of technician usernames
    """
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
    """
    List work orders, optionally filtered by status/priority/assignee.
    ---
    responses:
      200:
        description: List of work orders
    """
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
    """
    Get a single work order, plus its source alert (if any).
    ---
    responses:
      200:
        description: Work order detail
      404:
        description: Not found
    """
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
    """
    Create a Work Order from an existing High/Critical AI alert.
    Blocks duplicate creation if an open WO already exists for this record.
    ---
    responses:
      201:
        description: Work order created
      409:
        description: Work order already exists for this alert
      404:
        description: Alert not found
    """
    try:
        conn = get_db()
        alert = conn.execute(
            "SELECT * FROM decision_log WHERE record_id=?", (record_id,)
        ).fetchone()
        if alert is None:
            return jsonify({"error": "Alert not found"}), 404

        existing = conn.execute(
            "SELECT wo_id FROM work_orders WHERE record_id=? AND status NOT IN ('COMPLETED','CANCELLED')",
            (record_id,)
        ).fetchone()
        if existing:
            return jsonify({"error": "An open work order already exists for this alert",
                             "wo_id": existing['wo_id']}), 409

        machine_id = machine_id_for_record(record_id)
        asset_id = asset_id_for_machine(machine_id)
        priority = RISK_TO_PRIORITY.get(alert['risk_level'], 'MEDIUM')
        due_date = compute_due_date(priority)

        data = request.get_json(silent=True) or {}
        title = data.get('title') or f"{alert['risk_level']} Risk — Machine {machine_id}"
        assigned_to = data.get('assigned_to') or alert['assigned_to']
        initial_status = 'ASSIGNED' if assigned_to else 'OPEN'  # ← 新增這行

        cur = conn.execute("""
            INSERT INTO work_orders
                (record_id, machine_id, asset_id, title, description,
                 recommended_action, risk_level, risk_score, priority,
                 status, assigned_to, created_by, created_at, due_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id, machine_id, asset_id, title, alert['reasons'],
            alert['action'], alert['risk_level'], alert['risk_score'], priority,
            initial_status, assigned_to, current_user.username,  # ← status 改用變數
            datetime.utcnow().isoformat(timespec='seconds'), due_date
        ))
        conn.commit()
        wo_id = cur.lastrowid

        log_action(current_user.username, 'CREATE_WORK_ORDER', f'WO #{wo_id}',
                   f'from alert record #{record_id}, priority={priority}')

        if assigned_to:
            create_notification(
                username=assigned_to,
                title=f"New Work Order #{wo_id} assigned",
                message=f"{priority} priority work order created from alert #{record_id}.",
                level="warning",
                record_id=record_id,
                notif_type="assignment",
            )

        return jsonify({"message": "Work order created", "wo_id": wo_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/work-orders', methods=['POST'])
@login_required
def api_create_work_order_manual():
    """
    Manually create a work order (not tied to an AI alert).
    ---
    responses:
      201:
        description: Work order created
      400:
        description: Missing required field
    """
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({"error": "title is required"}), 400

        priority = data.get('priority', 'MEDIUM')
        if priority not in PRIORITY_DUE_HOURS:
            return jsonify({"error": "Invalid priority"}), 400

        assigned_to = data.get('assigned_to')
        initial_status = 'ASSIGNED' if assigned_to else 'OPEN'  # ← 新增

        due_date = data.get('due_date') or compute_due_date(priority)
        conn = get_db()
        cur = conn.execute("""
            INSERT INTO work_orders
                (record_id, machine_id, asset_id, title, description,
                 recommended_action, priority, status, assigned_to,
                 created_by, created_at, due_date)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('machine_id'), data.get('asset_id'), title,
            data.get('description'), data.get('recommended_action'),
            priority, initial_status, assigned_to,  # ← status 改用變數
            current_user.username, datetime.utcnow().isoformat(timespec='seconds'),
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
    """
    Cancel work order (Admin only).
    ---
    responses:
      200:
        description: Cancelled
      400:
        description: Invalid status
      403:
        description: Forbidden
      404:
        description: Not found
    """
    try:
        # 僅限管理員執行手動取消
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
        (datetime.utcnow().isoformat(timespec='seconds'), wo_id)
    )
    conn.commit()
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
        (datetime.utcnow().isoformat(timespec='seconds'), wo_id)
    )
    conn.commit()
    log_action(current_user.username, 'START_WORK_ORDER', f'WO #{wo_id}', '-')
    return jsonify({"message": "Started", "status": "IN_PROGRESS"})


@app.route('/api/work-orders/<int:wo_id>/notes', methods=['POST'])
@login_required
def api_save_technician_notes(wo_id):
    """Technician saves/updates process notes while actively working the WO
    (diagnosis, observations) — separate from the final resolution_note."""
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
    """Technician marks the maintenance done. Requires resolution_note —
    a mandatory record of what was actually fixed — and moves the WO to
    PENDING_VERIFICATION rather than closing it outright."""
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

    completed_at = datetime.utcnow().isoformat(timespec='seconds')
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
        level="info", record_id=wo['record_id'], notif_type="verification_pending",  # ← 改這裡
    )
    return jsonify({"message": "Marked complete, pending verification", "status": "PENDING_VERIFICATION"})


@app.route('/api/work-orders/<int:wo_id>/verify', methods=['POST'])
@login_required
def api_verify_work_order(wo_id):
    """Admin or the WO's original creator reviews the completed work and
    either closes the loop (approve) or sends it back for rework (reject)."""
    data = request.get_json(silent=True) or {}
    decision = data.get('decision')  # 'approve' | 'reject'
    verification_note = (data.get('verification_note') or '').strip()
    if decision not in ('approve', 'reject'):
        return jsonify({"error": "decision must be 'approve' or 'reject'"}), 400

    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
    if wo is None:
        return jsonify({"error": "Work order not found"}), 404
    if not (current_user.is_admin or wo['created_by'] == current_user.username):
        return jsonify({"error": "Forbidden: only an admin or the work order's creator can verify"}), 403
    if wo['status'] != 'PENDING_VERIFICATION':
        return jsonify({"error": f"Cannot verify from status {wo['status']}"}), 400

    verified_at = datetime.utcnow().isoformat(timespec='seconds')
    new_status = 'CLOSED' if decision == 'approve' else 'IN_PROGRESS'

    conn.execute(
        "UPDATE work_orders SET status=?, verified_by=?, verified_at=?, verification_note=? WHERE wo_id=?",
        (new_status, current_user.username, verified_at, verification_note or None, wo_id)
    )
    conn.commit()
    log_action(current_user.username, 'VERIFY_WORK_ORDER', f'WO #{wo_id}', f'{decision} → {new_status}')

    if wo['assigned_to']:
        create_notification(
            username=wo['assigned_to'],
            title=f"Work Order #{wo_id} {'closed' if decision == 'approve' else 'sent back for rework'}",
            message=verification_note or (
                "Verified and closed." if decision == 'approve' else "Please review and redo."),
            level="info" if decision == 'approve' else "warning",
            record_id=wo['record_id'],
            notif_type="wo_closed" if decision == 'approve' else "wo_rejected",  # ← 改這裡
        )
    return jsonify({"message": f"Work order {decision}d", "status": new_status})

@app.route('/api/work-orders/<int:wo_id>/assign', methods=['POST'])
@login_required
@role_required('admin')
def api_assign_work_order(wo_id):
    """
    Assign/reassign a work order to a technician.
    ---
    responses:
      200:
        description: Assigned
    """
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('technician') or '').strip()
        tech = get_user_by_username(username)
        if tech is None or tech.role != 'technician':
            return jsonify({"error": "Invalid technician"}), 400

        conn = get_db()
        wo = conn.execute("SELECT status FROM work_orders WHERE wo_id=?", (wo_id,)).fetchone()
        if wo is None:
            return jsonify({"error": "Work order not found"}), 404

        new_status = 'ASSIGNED' if wo['status'] == 'OPEN' else wo['status']
        conn.execute("UPDATE work_orders SET assigned_to=?, status=? WHERE wo_id=?",
                     (tech.username, new_status, wo_id))
        conn.commit()
        create_notification(
            username=tech.username,
            title=f"Work Order #{wo_id} assigned to you",
            message="You've been assigned a new work order.",
            level="warning", notif_type="assignment",
        )
        log_action(current_user.username, 'ASSIGN_WORK_ORDER', f'WO #{wo_id}', f'assigned_to={tech.username}')
        return jsonify({"message": "Assigned", "assigned_to": tech.username, "status": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════
# API ENDPOINTS: EXISTING MODULES
# ══════════════════════════════════════════

@app.route('/api/assign/<int:record_id>', methods=['POST'])
@login_required
def assign_alert(record_id):
    try:
        data = request.get_json(silent=True) or {}
        conn = get_db()
        row = conn.execute(
            "SELECT assigned_to, alert_status FROM decision_log WHERE record_id=?",
            (record_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "Record not found"}), 404

        if current_user.is_admin:
            target_username = data.get('technician', '').strip()
            if not target_username:
                return jsonify({"error": "technician username required"}), 400
            tech = get_user_by_username(target_username)
            if tech is None or tech.role != 'technician':
                return jsonify({"error": "Invalid technician"}), 400
            new_assignee = tech.username
        else:
            if row['assigned_to']:
                return jsonify({"error": "Alert is already assigned"}), 403
            new_assignee = current_user.username

        new_status = row['alert_status']
        if new_status in ('OPEN', 'OK'):
            new_status = 'ASSIGNED'

        conn.execute(
            "UPDATE decision_log SET assigned_to=?, alert_status=? WHERE record_id=?",
            (new_assignee, new_status, record_id)
        )
        conn.commit()
        create_notification(
            username=new_assignee,
            title = f"Alert #{record_id} assigned to you",
            message=f"You've been assigned to investigate record #{record_id}.",
            level="warning",
            record_id=record_id,
            notif_type="assignment",
            )
        log_action(current_user.username, 'ASSIGN_ALERT', f'Record #{record_id}',
                   f'assigned_to={new_assignee}')
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
    """
    Get all production lines with live machine-status aggregation.
    ---
    responses:
      200:
        description: List of production lines with asset counts
    """
    try:
        conn = get_db()
        lines = conn.execute("SELECT * FROM production_lines ORDER BY line_id").fetchall()
        result = []
        for line in lines:
            counts = conn.execute("""
                SELECT status, COUNT(*) c FROM assets WHERE line_id=? GROUP BY status
            """, (line['line_id'],)).fetchall()
            status_counts = {row['status']: row['c'] for row in counts}
            result.append({
                **dict(line),
                "asset_count": sum(status_counts.values()),
                "status_counts": status_counts
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/production-lines/<line_id>')
@login_required
def api_production_line_detail(line_id):
    """
    Get a single production line plus its assets.
    ---
    parameters:
      - name: line_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Production line detail with asset list
      404:
        description: Line not found
    """
    try:
        conn = get_db()
        line = conn.execute("SELECT * FROM production_lines WHERE line_id=?", (line_id,)).fetchone()
        if line is None:
            return jsonify({"error": "Production line not found"}), 404
        assets = conn.execute("SELECT * FROM assets WHERE line_id=? ORDER BY asset_id", (line_id,)).fetchall()
        return jsonify({**dict(line), "assets": df_to_records(pd.DataFrame([dict(a) for a in assets]))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets')
@login_required
def api_assets():
    """
    Get all assets, optionally filtered by production line.
    ---
    parameters:
      - name: line_id
        in: query
        type: string
        required: false
    responses:
      200:
        description: List of assets
    """
    try:
        conn = get_db()
        line_id = request.args.get('line_id')
        if line_id:
            assets = conn.execute(
                "SELECT * FROM assets WHERE line_id=? ORDER BY asset_id", (line_id,)
            ).fetchall()
        else:
            assets = conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()
        return jsonify(df_to_records(pd.DataFrame([dict(a) for a in assets])))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets/<asset_id>')
@login_required
def api_asset_detail(asset_id):
    """
    Get a single asset plus the production line it belongs to.
    ---
    parameters:
      - name: asset_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Asset detail
      404:
        description: Asset not found
    """
    try:
        conn = get_db()
        asset = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if asset is None:
            return jsonify({"error": "Asset not found"}), 404
        line = conn.execute(
            "SELECT * FROM production_lines WHERE line_id=?", (asset['line_id'],)
        ).fetchone()
        return jsonify({**dict(asset), "line": dict(line) if line else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/assets/<asset_id>/observations')
@login_required
def api_asset_observations(asset_id):
    """
    Get recent AI4I sensor observations (decision_log rows) belonging to
    this asset. Since AI4I records have no native machine reference, rows
    are matched back to the asset via the same deterministic mapping used
    by machine_id_for_record(): record_id -> ((record_id % NUM_MACHINES) + 1)
    -> MCH-XXX. This is the inverse of that mapping, filtered by SQL modulo
    instead of iterating every record_id in Python.
    ---
    parameters:
      - name: asset_id
        in: path
        type: string
        required: true
      - name: limit
        in: query
        type: integer
        required: false
        description: Max rows to return (default 20, max 200).
    responses:
      200:
        description: Recent observations for this asset's machine
      404:
        description: Asset not found
    """
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
            return jsonify({"error": f"Malformed machine_id '{machine_id}' for this asset"}), 500

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
    """
    Get work orders linked to this asset's machine (by machine_id),
    newest first.
    ---
    parameters:
      - name: asset_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Work orders for this asset
      404:
        description: Asset not found
    """
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
    """
    Get system summary.
    ---
    responses:
      200:
        description: Summary statistics
    """
    try:
        conn = get_db()
        df       = pd.read_sql("SELECT risk_level, COUNT(*) as count FROM decision_log GROUP BY risk_level", conn)
        total    = pd.read_sql("SELECT COUNT(*) as total FROM decision_log", conn)['total'][0]
        alerts   = pd.read_sql("SELECT COUNT(*) as c FROM decision_log WHERE alert_status='OPEN'", conn)['c'][0]
        high     = pd.read_sql("SELECT COUNT(*) as c FROM decision_log WHERE risk_level IN ('High','Critical')", conn)['c'][0]
        return jsonify({
            "total_records":  int(total),
            "open_alerts":    int(alerts),
            "high_risk":      int(high),
            "distribution":   df.to_dict(orient='records')
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/work-orders/summary')
@login_required
def api_work_orders_summary():
    """
    Get Work Order KPI counts for the dashboard.
    ---
    responses:
      200:
        description: Work order counts by status
    """
    try:
        conn = get_db()
        df = pd.read_sql("SELECT status, COUNT(*) c FROM work_orders GROUP BY status", conn)
        counts = dict(zip(df['status'], df['c']))

        # 將 ACCEPTED 與 PENDING_VERIFICATION 一併計入進行中 / 未結案的 open_count
        open_count = (
            counts.get('OPEN', 0)
            + counts.get('ASSIGNED', 0)
            + counts.get('ACCEPTED', 0)
            + counts.get('IN_PROGRESS', 0)
            + counts.get('PENDING_VERIFICATION', 0)
        )

        # 排除已結案 (CLOSED) 與已取消 (CANCELLED) 的逾期統計
        overdue = conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE due_date < ? "
            "AND status NOT IN ('CLOSED', 'CANCELLED')",
            (datetime.utcnow().isoformat(timespec='seconds'),)
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
    """KPI counts scoped to the current technician's own assigned work
    orders, for the technician-specific dashboard card."""
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
        (current_user.username, datetime.utcnow().isoformat(timespec='seconds'))
    ).fetchone()['c']
    return jsonify({
        "my_pending": pending,
        "my_awaiting_verification": awaiting_verification,
        "my_overdue": overdue,
    })

@app.route('/api/metrics')
@login_required
def api_metrics():
    """
    Get KPI metrics.
    ---
    responses:
      200:
        description: KPI metrics including resolution rate and risk stats
    """
    try:
        conn = get_db()
        df   = pd.read_sql("SELECT * FROM decision_log", conn)
        total = len(df)
        resolved = int((df['alert_status'] == 'RESOLVED').sum())
        fp = int((df['alert_status'] == 'FALSE_POSITIVE').sum())
        open_ = int((df['alert_status'] == 'OPEN').sum())
        in_progress = int((df['alert_status'] == 'IN_PROGRESS').sum())
        high = int(df['risk_level'].isin(['High', 'Critical']).sum())
        denom = resolved + open_ + in_progress
        return jsonify({
            "total_records": total,
            "open_alerts": open_,
            "in_progress": in_progress,
            "high_risk_count": high,
            "resolved_alerts": resolved,
            "false_positives": fp,
            "resolution_rate": f"{resolved / denom * 100:.1f}%" if denom > 0 else "0%",
            "avg_risk_score": round(float(df['risk_score'].mean()), 1),
            "avg_pred_prob": round(float(df['pred_prob'].mean()), 3)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/alerts')
@login_required
def api_alerts():
    """
    Get High/Critical risk records for the High Risk Alerts preview.
    Includes the simulated machine_id + asset_id for each record so the
    dashboard can show and link the responsible machine directly.
    ---
    responses:
      200:
        description: High/Critical risk records, highest risk score first
    """
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

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/update_status/<int:record_id>', methods=['POST'])
@login_required
def update_status(record_id):
    """
    Update an alert's status. When status is RESOLVED or FALSE_POSITIVE,
    an optional 'note' in the JSON body is stored as the resolution reason
    (e.g. "False positive after inspection.") alongside who closed it and
    when — shown as the Resolution Information card on record_detail.
    ---
    parameters:
      - name: record_id
        in: path
        type: integer
        required: true
      - name: body
        in: body
        schema:
          properties:
            status:
              type: string
            note:
              type: string
              description: Optional resolution note, only used for RESOLVED/FALSE_POSITIVE.
    responses:
      200:
        description: Status updated
    """
    try:
        status  = request.json.get('status')
        note    = (request.json.get('note') or '').strip()
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

        if status in ('RESOLVED', 'FALSE_POSITIVE'):
            resolved_at = datetime.utcnow().isoformat(timespec='seconds')
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


@app.route('/api/record/<int:record_id>/history')
@login_required
def api_record_history(record_id):
    """
    Get assignment/status history for a specific record, sourced from audit_log.
    ---
    parameters:
      - name: record_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: List of audit log entries for this record
    """
    try:
        conn = get_db()
        target = f'Record #{record_id}'
        df = pd.read_sql(
            "SELECT timestamp, username, action, details FROM audit_log "
            "WHERE target=? ORDER BY timestamp ASC",
            conn, params=[target]
        )
        return jsonify(df_to_records(df))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/trends')
@login_required
def api_trends():
    """
    Get risk score trend data for charting.
    ---
    responses:
      200:
        description: Trend data grouped by risk level
    """
    try:
        conn = get_db()
        df   = pd.read_sql("SELECT record_id, risk_score, pred_prob, risk_level FROM decision_log ORDER BY record_id", conn)
        sampled = df[df['record_id'] % 10 == 0]
        return jsonify({
            "record_ids":   sampled['record_id'].tolist(),
            "risk_scores":  sampled['risk_score'].tolist(),
            "pred_probs":   sampled['pred_prob'].round(3).tolist(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

_stream_index = 0
_stream_lock  = threading.Lock()


@app.route('/api/model-comparison')
@login_required
def api_model_comparison():
    """
    Get LR vs RF performance metrics and prediction disagreement analysis.
    ---
    responses:
      200:
        description: Model comparison metrics and disagreement records
    """
    try:
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

        return jsonify({
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
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/shap/global')
@login_required
def api_shap_global():
    """
    Get global SHAP feature importance (mean |SHAP value| per feature),
    computed once at training time and cached to disk.
    ---
    responses:
      200:
        description: Feature importance ranking + model metadata
      404:
        description: SHAP importance file not found (model not yet trained)
    """
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
    """
    Get the full SHAP feature breakdown for the current highest-risk record.
    ---
    responses:
      200:
        description: Record info + all feature SHAP values, sorted by |impact|
      404:
        description: No records found
    """
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
    """
    Get ROC curve data + raw prob/actual arrays for LR and RF.
    ---
    responses:
      200:
        description: ROC points, AUC, and per-record probabilities for both models
    """
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
    """
    Get system health status (API, DB, Model, System resources).
    ---
    responses:
      200:
        description: Health status of all subsystems
    """
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
        "checked_at": datetime.utcnow().isoformat(timespec='seconds'),
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
    """
    Get recent health check history for timeline visualization.
    ---
    responses:
      200:
        description: Last 30 health check results
    """
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
    """
    Get next record for streaming simulation.
    ---
    responses:
      200:
        description: Single record cycled row-by-row
    """
    global _stream_index
    try:
        conn = get_db()
        total = pd.read_sql("SELECT COUNT(*) as c FROM decision_log", conn)['c'][0]
        with _stream_lock:
            idx = _stream_index % int(total)
            _stream_index += 1
        df = pd.read_sql(
            "SELECT * FROM decision_log ORDER BY record_id LIMIT 1 OFFSET ?",
            conn, params=[idx]
        )
        row = df_to_records(df)[0] if not df.empty else None
        if row is None:
            return jsonify({"error": "No data"}), 404
        row['stream_index'] = idx
        row['total_records'] = int(total)

        if row.get('risk_level') in ('High', 'Critical'):
            notif_type = 'critical_alert' if row['risk_level'] == 'Critical' else 'risk_escalated'
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
    """
    Export High/Critical risk records as CSV.
    ---
    responses:
      200:
        description: CSV file download
    """
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
    """
    Get the persistent notification list for the current user.
    Admins also see broadcast notifications (username IS NULL).
    ---
    responses:
      200:
        description: Notifications newest-first, plus unread count
    """
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
    """
    Mark a single notification as read.
    ---
    responses:
      200:
        description: Marked as read
    """
    try:
        conn = get_db()
        conn.execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND (username=? OR username IS NULL)",
            (datetime.utcnow().isoformat(timespec='seconds'), notif_id, current_user.username)
        )
        conn.commit()
        return jsonify({"message": "Marked as read"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_notifications_read_all():
    """
    Mark every notification visible to the current user as read.
    ---
    responses:
      200:
        description: All marked as read
    """
    try:
        conn = get_db()
        now = datetime.utcnow().isoformat(timespec='seconds')
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
    """
    Server-Sent Events stream.
    """
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
# PAGE ROUTES (protected: any logged-in user)
# ══════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

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
    init_notifications_table()
    init_production_lines_table()
    init_assets_table()
    seed_assets_and_lines()
    init_indexes()
    app.run(debug=False, threaded=True)