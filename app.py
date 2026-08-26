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
from datetime import datetime
from functools import wraps
from sklearn.metrics import roc_curve, roc_auc_score
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)

APP_START_TIME = datetime.utcnow()
MODEL_VERSION = "Random Forest + Logistic Regression"

app = Flask(__name__)
DB = r"C:\Users\limli\Inti Folder\FYP\machine_monitor.db"
MODEL_METRICS_CSV = os.path.join(os.path.dirname(DB), "model_metrics.csv")
SHAP_IMPORTANCE_JSON = os.path.join(os.path.dirname(DB), "shap_importance.json")

app.config['SWAGGER'] = {'title': 'Predictive Maintenance API', 'uiversion': 3}
Swagger(app)

# Required for session cookies (Flask-Login). Change to a real random value.
app.config['SECRET_KEY'] = 'change-this-to-a-long-random-secret-key'

bcrypt = Bcrypt(app)

# ══════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

_notification_subscribers = {}
_notification_lock = threading.Lock()


def _is_admin_username(username):
    """Raw-connection role check (no flask.g dependency), safe to call
    from create_notification() even outside a request context."""
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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

def init_assignment_column():
    """Add assigned_to column to decision_log if it doesn't exist yet."""
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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
    """CPU + RAM usage via psutil."""
    try:
        cpu = psutil.cpu_percent(interval=0.1)
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
    conn = sqlite3.connect(DB)
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
    conn = sqlite3.connect(DB)
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
        flash('You cannot disable your own account.', 'error')
        return redirect(url_for('manage_users'))

    conn = get_db()
    row = conn.execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        flash('User not found.', 'error')
        return redirect(url_for('manage_users'))

    new_status = 0 if row['is_active'] else 1
    conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_status, user_id))
    conn.commit()
    target_row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    log_action(current_user.username,
               'ENABLE_USER' if new_status else 'DISABLE_USER',
               target_row['username'] if target_row else str(user_id),
               f"active→{bool(new_status)}")
    flash('User status updated.', 'info')
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

        conn.execute("UPDATE users SET email=?, role=? WHERE id=?", (email, role, user_id))
        conn.commit()
        log_action(current_user.username, 'EDIT_USER', user['username'],
                   f"email={email}, role={role}")
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
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('manage_users'))

    conn = get_db()
    target_row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    log_action(current_user.username, 'DELETE_USER',
               target_row['username'] if target_row else str(user_id), '-')
    flash('User deleted.', 'success')
    return redirect(url_for('manage_users'))



@app.route('/admin/audit-log')
@login_required
@role_required('admin')
def audit_log():
    conn = get_db()
    action_filter = request.args.get('action', '').strip()
    search = request.args.get('search', '').strip()

    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if action_filter:
        query += " AND action=?"
        params.append(action_filter)
    if search:
        query += " AND username LIKE ?"
        params.append(f'%{search}%')
    query += " ORDER BY timestamp DESC LIMIT 500"

    logs = conn.execute(query, params).fetchall()
    actions = conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()

    return render_template('audit_log.html', logs=logs, actions=actions,
                            action_filter=action_filter, search=search)


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
# API ENDPOINTS  (protected: any logged-in user)
# ══════════════════════════════════════════

@app.route('/api/assign/<int:record_id>', methods=['POST'])
@login_required
def assign_alert(record_id):
    """
    Assign an alert to a technician (admin) or self-assign (technician claiming an unassigned alert).
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
            technician:
              type: string
              description: Required when admin assigns; ignored for self-assign.
    responses:
      200:
        description: Alert assigned
      400:
        description: Invalid request
      403:
        description: Forbidden
      404:
        description: Record not found
    """
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
            # Technician self-assign — only allowed if currently unassigned
            if row['assigned_to']:
                return jsonify({"error": "Alert is already assigned"}), 403
            new_assignee = current_user.username

        new_status = row['alert_status']
        if new_status == 'OPEN':
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
    try:
        conn = get_db()
        df = pd.read_sql("""
            SELECT record_id, pred_prob, risk_score, risk_level,
                   action, reasons, shap_explanation, alert_status, assigned_to, timestamp
            FROM decision_log
            WHERE risk_level IN ('High','Critical')
            ORDER BY risk_score DESC
        """, conn)
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

        # 讀取對應的 SHAP 特徵值
        shap_row = pd.read_sql(
            "SELECT * FROM shap_values WHERE record_id=?",
            conn, params=[record_id]
        )

        # 使用你定義好的 df_to_records 安全地將 DataFrame 轉換為 dict 列表
        shap_list = df_to_records(shap_row)

        # 組合資料並回傳
        result = df_to_records(df)[0]
        result['shap'] = shap_list
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

        # RBAC: technician can only update alerts assigned to them; admin can update any.
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
            # Leaving a resolved state (e.g. reopened) clears the resolution fields
            # so the card doesn't show stale "resolved by" info for an open alert.
            conn.execute(
                "UPDATE decision_log SET alert_status=?, resolved_by=NULL, resolved_at=NULL, "
                "resolution_note=NULL WHERE record_id=?",
                (status, record_id)
            )
        conn.commit()
        if status == 'RESOLVED':
            create_notification(
                username=None,  # broadcast to every connected admin
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
        # ── Part 1: performance metrics (from model_metrics.csv) ──
        metrics_df = pd.read_csv(MODEL_METRICS_CSV)
        metrics = metrics_df.to_dict(orient='records')

        # ── Part 2: prediction disagreement ──
        # predictions table already has model_agreement (computed at 0.5 threshold
        # in the notebook) — reuse it instead of recomputing agreement logic here.
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

        # LR has no stored predicted label (only probability) — derive it here.
        df['lr_pred'] = (df['pred_prob_lr'] >= 0.5).astype(int)
        df['rf_pred'] = (df['pred_prob_rf'] >= 0.5).astype(int)

        rf_fail_lr_normal = int(((df['rf_pred'] == 1) & (df['lr_pred'] == 0)).sum())
        rf_normal_lr_fail = int(((df['rf_pred'] == 0) & (df['lr_pred'] == 1)).sum())

        # False negatives = actual failure that the model predicted as normal.
        # This matters more than raw accuracy for predictive maintenance —
        # a missed failure (FN) is far costlier than a false alarm (FP).
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
    computed once at training time and cached to disk. Powers the
    dynamic Mean |SHAP| bar chart on the Dashboard — updates automatically
    whenever the model is retrained, without recomputing SHAP on every request.
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
    Replaces the static Force Plot PNG on the Dashboard with a dynamic
    chart that updates whenever a new highest-risk record appears
    (e.g. after retraining or as new records are processed).
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
        # Drop the bookkeeping columns — everything else is a feature column
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
    Get ROC curve data + raw prob/actual arrays for LR and RF,
    used by the Explainability page to draw an interactive ROC curve
    and let the user drag a threshold slider to recompute the
    confusion matrix client-side (no extra round trips per drag).
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

            # sklearn always sticks an extra threshold = inf at index 0
            # so the curve starts at (0,0) — clip it to 1.0 so it's valid JSON
            # and still reads as "predict everything as normal".
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

    # DB record info
    conn = get_db()
    last_updated_row = conn.execute("SELECT MAX(timestamp) t FROM decision_log").fetchone()
    db_last_updated = last_updated_row['t'] if last_updated_row and last_updated_row['t'] else None

    # Log this check, then pull check counters
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
        df = df.iloc[::-1]  # oldest → newest for left-to-right timeline
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
                    username=None,  # broadcast to every connected admin
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
    Server-Sent Events stream. The browser opens this ONCE and keeps it
    open; new notifications are pushed down the same connection the
    instant create_notification() fires elsewhere in the app — this is
    what replaces polling. A keepalive ping goes out every 25s so proxies
    / browsers don't time out the idle connection.
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
    init_notifications_table()
    app.run(debug=True, threaded=True)