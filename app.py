import os
import sqlite3
import threading
import time
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, g, jsonify, abort
)
from werkzeug.security import check_password_hash

import vm_manager  # Phase 2 fills in the real QEMU calls

DB_PATH = os.path.join(os.path.dirname(__file__), "piweb.db")
INACTIVITY_LIMIT_SECONDS = 15 * 60

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("PIWEB_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# When you're behind Cloudflare Tunnel the connection reaching Flask is
# already TLS-terminated at Cloudflare's edge, but set this True once
# you confirm the tunnel is forcing https end to end.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("PIWEB_FORCE_SECURE_COOKIE", "1") == "1"

OS_LABELS = {
    "alpine": "Alpine Linux",
    "debian": "Debian 12 (Pi OS-style)",
    "ubuntu": "Ubuntu Server (minimal)",
}

# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_config(key, default=None):
    row = get_db().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ------------------------------------------------------------------- auth

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    password = request.form.get("password", "")
    stored_hash = get_config("password_hash")

    if not stored_hash:
        return render_template(
            "login.html",
            error="No admin password is set. Run setup_password.py on the Pi first."
        )

    # Constant-time-safe comparison is handled inside check_password_hash.
    if check_password_hash(stored_hash, password):
        session.clear()
        session["authed"] = True
        session.permanent = False
        return redirect(url_for("dashboard"))

    time.sleep(0.6)  # slow down brute-force attempts
    return render_template("login.html", error="Incorrect password.")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------------------- dashboard

@app.route("/")
@login_required
def dashboard():
    db = get_db()
    machines = db.execute(
        "SELECT * FROM machines ORDER BY created_at DESC"
    ).fetchall()
    any_running = any(m["status"] in ("running", "starting") for m in machines)
    return render_template(
        "dashboard.html",
        machines=machines,
        os_labels=OS_LABELS,
        any_running=any_running,
    )


@app.route("/machines/new", methods=["GET", "POST"])
@login_required
def new_machine():
    if request.method == "GET":
        return render_template("new_machine.html", os_labels=OS_LABELS)

    name = request.form.get("name", "").strip()
    os_type = request.form.get("os_type", "")
    ram_mb = int(request.form.get("ram_mb", 2048))
    disk_gb = int(request.form.get("disk_gb", 8))
    guest_username = request.form.get("guest_username", "").strip().lower()
    guest_password = request.form.get("guest_password", "")
    guest_password_confirm = request.form.get("guest_password_confirm", "")

    if not name or os_type not in OS_LABELS:
        return render_template(
            "new_machine.html", os_labels=OS_LABELS,
            error="Give it a name and pick a base OS."
        )
    if not (512 <= ram_mb <= 3072):
        return render_template(
            "new_machine.html", os_labels=OS_LABELS,
            error="RAM must be between 512MB and 3072MB (leave headroom for the host)."
        )
    if guest_password != guest_password_confirm:
        return render_template(
            "new_machine.html", os_labels=OS_LABELS,
            error="Guest passwords don't match."
        )

    try:
        vm_manager.create_machine(get_db(), name=name, os_type=os_type,
                                   ram_mb=ram_mb, disk_gb=disk_gb,
                                   guest_username=guest_username,
                                   guest_password=guest_password)
    except vm_manager.VMError as e:
        return render_template("new_machine.html", os_labels=OS_LABELS, error=str(e))

    return redirect(url_for("dashboard"))


@app.route("/machines/<int:machine_id>/settings", methods=["GET", "POST"])
@login_required
def machine_settings(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        abort(404)

    if machine["status"] != "stopped":
        return render_template(
            "settings.html", machine=machine, os_labels=OS_LABELS,
            error="Stop the machine before changing its settings."
        )

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ram_mb = int(request.form.get("ram_mb", machine["ram_mb"]))
        if not name:
            return render_template("settings.html", machine=machine, os_labels=OS_LABELS,
                                    error="Name can't be empty.")
        if not (512 <= ram_mb <= 3072):
            return render_template("settings.html", machine=machine, os_labels=OS_LABELS,
                                    error="RAM must be between 512MB and 3072MB.")
        db.execute("UPDATE machines SET name = ?, ram_mb = ? WHERE id = ?",
                   (name, ram_mb, machine_id))
        db.commit()
        return redirect(url_for("dashboard"))

    return render_template("settings.html", machine=machine, os_labels=OS_LABELS)


@app.route("/machines/<int:machine_id>/delete", methods=["POST"])
@login_required
def delete_machine(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        abort(404)
    if machine["status"] != "stopped":
        abort(400, "Stop the machine before deleting it.")
    vm_manager.delete_machine(db, machine)
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------ machine run

@app.route("/machines/<int:machine_id>/start", methods=["POST"])
@login_required
def start_machine(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        abort(404)

    running = db.execute(
        "SELECT id FROM machines WHERE status IN ('running','starting') AND id != ?",
        (machine_id,)
    ).fetchone()
    if running:
        return jsonify({"ok": False, "error": "Another machine is already running. "
                                                 "Only one can be on at a time."}), 409

    try:
        vm_manager.start_machine(db, machine)
    except vm_manager.VMError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "redirect": url_for("console", machine_id=machine_id)})


@app.route("/machines/<int:machine_id>/stop", methods=["POST"])
@login_required
def stop_machine(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        abort(404)
    vm_manager.stop_machine(db, machine)
    return jsonify({"ok": True})


@app.route("/machines/<int:machine_id>/console")
@login_required
def console(machine_id):
    db = get_db()
    machine = db.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        abort(404)
    if machine["status"] not in ("running", "starting"):
        return redirect(url_for("dashboard"))
    return render_template("console.html", machine=machine)


@app.route("/machines/<int:machine_id>/heartbeat", methods=["POST"])
@login_required
def heartbeat(machine_id):
    db = get_db()
    db.execute("UPDATE machines SET last_active = datetime('now') WHERE id = ?", (machine_id,))
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------- inactivity job

def inactivity_watcher():
    """
    Background thread: any machine whose last_active is older than
    INACTIVITY_LIMIT_SECONDS gets force-stopped. This is what covers the
    'tab closed without shutting down' case, since a closed tab stops
    sending heartbeats.
    """
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.utcnow() - timedelta(seconds=INACTIVITY_LIMIT_SECONDS)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            stale = conn.execute(
                "SELECT * FROM machines WHERE status IN ('running','starting') "
                "AND (last_active IS NULL OR last_active < ?)",
                (cutoff,)
            ).fetchall()
            for machine in stale:
                vm_manager.stop_machine(conn, machine)
            conn.close()
        except Exception as e:
            app.logger.error("inactivity watcher error: %s", e)
        time.sleep(30)


def start_background_thread():
    t = threading.Thread(target=inactivity_watcher, daemon=True)
    t.start()


# Runs at import time — this is what makes it work under gunicorn too,
# not just `python3 app.py` directly. Gunicorn imports this module once
# per worker process rather than executing the `__main__` block, so the
# watcher has to start here to run at all under that deployment.
#
# IMPORTANT: run gunicorn with exactly one worker (--workers 1) for
# this app. It's a single-admin panel with no need for concurrency, and
# more than one worker means more than one of these threads independently
# polling/stopping machines, plus a separate SECRET_KEY per worker unless
# PIWEB_SECRET_KEY is set in the environment (it must be, either way —
# see README).
if not os.path.exists(DB_PATH):
    raise SystemExit(
        "No database found. Run:\n"
        "  sqlite3 piweb.db < schema.sql\n"
        "  python3 setup_password.py\n"
    )
start_background_thread()


if __name__ == "__main__":
    # Bind to localhost only — Cloudflare Tunnel talks to this locally
    # and handles the public-facing TLS side. Don't expose 0.0.0.0
    # directly to the internet.
    app.run(host="127.0.0.1", port=5000)
