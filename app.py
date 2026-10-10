"""Network Device Monitor.

A small full-stack monitoring app: a Flask REST API checks whether network
devices/services are reachable over TCP, stores the results in SQLite, and a
dashboard (static/index.html) shows live status, latency and uptime.
"""
import ipaddress
import os
import re
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
HISTORY_KEEP = 500  # check results kept per device
MANUAL_CHECK_COOLDOWN = 10  # seconds between "Check now" runs

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    host       TEXT    NOT NULL,
    port       INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (host, port)
);
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ts         TEXT    NOT NULL,
    up         INTEGER NOT NULL,
    latency_ms REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_device ON checks (device_id, id);
"""

DEVICE_SQL = """
SELECT d.id, d.name, d.host, d.port,
  (SELECT up         FROM checks c WHERE c.device_id = d.id ORDER BY c.id DESC LIMIT 1) AS last_up,
  (SELECT latency_ms FROM checks c WHERE c.device_id = d.id ORDER BY c.id DESC LIMIT 1) AS last_latency,
  (SELECT ts         FROM checks c WHERE c.device_id = d.id ORDER BY c.id DESC LIMIT 1) AS last_ts,
  (SELECT error      FROM checks c WHERE c.device_id = d.id ORDER BY c.id DESC LIMIT 1) AS last_error,
  (SELECT ROUND(100.0 * SUM(up) / COUNT(*), 1)
     FROM (SELECT up FROM checks c WHERE c.device_id = d.id ORDER BY c.id DESC LIMIT 100)) AS uptime_pct
FROM devices d
"""

DEMO_DEVICES = [
    ("Google (HTTPS)", "google.com", 443),
    ("Cloudflare (HTTPS)", "cloudflare.com", 443),
    ("GitHub (HTTPS)", "github.com", 443),
    ("Cloudflare DNS (TCP 53)", "1.1.1.1", 53),
    ("Google DNS (TCP 53)", "8.8.8.8", 53),
    ("Demo: unresolvable host (expected DOWN)", "does-not-exist.invalid", 443),
]


def env_bool(name, default=False):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def default_config():
    return {
        "DB_PATH": os.environ.get("DB_PATH", "monitor.db"),
        "CHECK_INTERVAL": int(os.environ.get("CHECK_INTERVAL", "30")),
        "CHECK_TIMEOUT": float(os.environ.get("CHECK_TIMEOUT", "3")),
        "MAX_DEVICES": int(os.environ.get("MAX_DEVICES", "20")),
        "ALLOW_PRIVATE": env_bool("ALLOW_PRIVATE"),  # allow RFC1918/loopback targets
        "READ_ONLY": env_bool("READ_ONLY"),          # disable add/delete (public demo)
        "START_CHECKER": True,
        "SEED": True,
    }


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- database
def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def seed_demo_devices(path):
    conn = connect(path)
    try:
        if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO devices (name, host, port, created_at) VALUES (?, ?, ?, ?)",
                [(n, h, p, now_iso()) for n, h, p in DEMO_DEVICES],
            )
            conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- checking
def resolve_target(host, allow_private):
    """Resolve host to an IPv4 address; refuse private/reserved ranges.

    Blocking non-global addresses stops the monitor being used to probe
    internal networks (SSRF). The check runs again on every probe, which also
    limits DNS-rebinding tricks.
    """
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not allow_private:
        for ip in ips:
            if not ip.is_global:
                raise ValueError("private or reserved addresses are not allowed")
    return str(ips[0])


def check_target(host, port, allow_private, timeout):
    """One TCP connect check. Returns {"up", "latency_ms", "error"}."""
    try:
        ip = resolve_target(host, allow_private)
    except socket.gaierror:
        return {"up": 0, "latency_ms": None, "error": "DNS resolution failed"}
    except ValueError as exc:
        return {"up": 0, "latency_ms": None, "error": str(exc)}

    start = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except (socket.timeout, TimeoutError):
        return {"up": 0, "latency_ms": None, "error": "connection timed out"}
    except ConnectionRefusedError:
        return {"up": 0, "latency_ms": None, "error": "connection refused"}
    except OSError as exc:
        return {"up": 0, "latency_ms": None, "error": (exc.strerror or "network error")[:100]}
    return {"up": 1, "latency_ms": (time.perf_counter() - start) * 1000, "error": None}


def record_result(conn, device_id, result):
    try:
        conn.execute(
            "INSERT INTO checks (device_id, ts, up, latency_ms, error) VALUES (?, ?, ?, ?, ?)",
            (device_id, now_iso(), result["up"], result["latency_ms"], result["error"]),
        )
        conn.execute(
            "DELETE FROM checks WHERE device_id = ? AND id NOT IN "
            "(SELECT id FROM checks WHERE device_id = ? ORDER BY id DESC LIMIT ?)",
            (device_id, device_id, HISTORY_KEEP),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()  # device was deleted while being checked


def run_all_checks(db_path, allow_private, timeout):
    conn = connect(db_path)
    try:
        devices = conn.execute("SELECT id, host, port FROM devices").fetchall()
        if not devices:
            return 0
        with ThreadPoolExecutor(max_workers=min(10, len(devices))) as pool:
            results = list(pool.map(
                lambda d: check_target(d["host"], d["port"], allow_private, timeout), devices))
        for device, result in zip(devices, results):
            record_result(conn, device["id"], result)
        return len(devices)
    finally:
        conn.close()


def start_checker(app):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                run_all_checks(app.config["DB_PATH"], app.config["ALLOW_PRIVATE"],
                               app.config["CHECK_TIMEOUT"])
            except Exception:  # keep the monitor alive whatever happens
                app.logger.exception("check cycle failed")
            stop.wait(app.config["CHECK_INTERVAL"])

    threading.Thread(target=loop, name="checker", daemon=True).start()
    return stop


# ---------------------------------------------------------------------- app
def device_json(row):
    status = "unknown" if row["last_up"] is None else ("up" if row["last_up"] else "down")
    latency = row["last_latency"]
    return {
        "id": row["id"], "name": row["name"], "host": row["host"], "port": row["port"],
        "status": status,
        "latency_ms": None if latency is None else round(latency, 1),
        "last_checked": row["last_ts"], "error": row["last_error"],
        "uptime_pct": row["uptime_pct"],
    }


def create_app(config=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(default_config())
    if config:
        app.config.update(config)

    init_db(app.config["DB_PATH"])
    if app.config["SEED"]:
        seed_demo_devices(app.config["DB_PATH"])
    if app.config["START_CHECKER"]:
        start_checker(app)

    manual = {"last": float("-inf")}
    manual_lock = threading.Lock()

    def db():
        if "db" not in g:
            g.db = connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    def read_only_guard():
        if app.config["READ_ONLY"]:
            return jsonify(error="this instance is read-only"), 403
        return None

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/config")
    def get_config():
        return jsonify(read_only=app.config["READ_ONLY"],
                       check_interval=app.config["CHECK_INTERVAL"],
                       max_devices=app.config["MAX_DEVICES"])

    @app.get("/api/devices")
    def list_devices():
        rows = db().execute(DEVICE_SQL + " ORDER BY d.id").fetchall()
        return jsonify([device_json(r) for r in rows])

    @app.get("/api/summary")
    def summary():
        rows = db().execute(DEVICE_SQL).fetchall()
        statuses = [device_json(r)["status"] for r in rows]
        return jsonify(total=len(statuses), up=statuses.count("up"),
                       down=statuses.count("down"), unknown=statuses.count("unknown"))

    @app.post("/api/devices")
    def add_device():
        blocked = read_only_guard()
        if blocked:
            return blocked
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="send a JSON object with name, host and port"), 400

        name = str(data.get("name", "")).strip()
        host = str(data.get("host", "")).strip().lower()
        port = data.get("port")
        if isinstance(port, str) and port.strip().isdigit():
            port = int(port.strip())
        if not 1 <= len(name) <= 50:
            return jsonify(error="name must be 1-50 characters"), 400
        if not HOST_RE.match(host):
            return jsonify(error="host must be a valid hostname or IPv4 address"), 400
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return jsonify(error="port must be a number between 1 and 65535"), 400

        conn = db()
        if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= app.config["MAX_DEVICES"]:
            return jsonify(error=f"device limit reached ({app.config['MAX_DEVICES']})"), 400
        try:
            resolve_target(host, app.config["ALLOW_PRIVATE"])
        except socket.gaierror:
            return jsonify(error="host could not be resolved"), 400
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        try:
            cur = conn.execute(
                "INSERT INTO devices (name, host, port, created_at) VALUES (?, ?, ?, ?)",
                (name, host, port, now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify(error="this host and port are already monitored"), 409

        device_id = cur.lastrowid
        record_result(conn, device_id, check_target(
            host, port, app.config["ALLOW_PRIVATE"], app.config["CHECK_TIMEOUT"]))
        row = conn.execute(DEVICE_SQL + " WHERE d.id = ?", (device_id,)).fetchone()
        return jsonify(device_json(row)), 201

    @app.delete("/api/devices/<int:device_id>")
    def delete_device(device_id):
        blocked = read_only_guard()
        if blocked:
            return blocked
        conn = db()
        cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="device not found"), 404
        return "", 204

    @app.get("/api/devices/<int:device_id>/history")
    def history(device_id):
        conn = db()
        if conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone() is None:
            return jsonify(error="device not found"), 404
        limit = request.args.get("limit", default=50, type=int)
        limit = max(1, min(limit if limit is not None else 50, 200))
        rows = conn.execute(
            "SELECT ts, up, latency_ms, error FROM checks WHERE device_id = ? "
            "ORDER BY id DESC LIMIT ?", (device_id, limit)).fetchall()
        return jsonify([{"ts": r["ts"], "up": bool(r["up"]),
                         "latency_ms": None if r["latency_ms"] is None else round(r["latency_ms"], 1),
                         "error": r["error"]} for r in rows])

    @app.post("/api/check")
    def check_now():
        with manual_lock:
            wait = MANUAL_CHECK_COOLDOWN - (time.monotonic() - manual["last"])
            if wait > 0:
                return jsonify(error=f"please wait {int(wait) + 1}s before checking again"), 429
            manual["last"] = time.monotonic()
        checked = run_all_checks(app.config["DB_PATH"], app.config["ALLOW_PRIVATE"],
                                 app.config["CHECK_TIMEOUT"])
        return jsonify(checked=checked)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
