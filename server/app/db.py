import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at INTEGER NOT NULL,
    email TEXT,
    notify_uploads INTEGER NOT NULL DEFAULT 0,
    notify_errors INTEGER NOT NULL DEFAULT 0,
    notify_devices INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    token_hash TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    accepted_at INTEGER,
    accepted_user_id INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail_outbox (
    id INTEGER PRIMARY KEY,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    config TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    last_seen INTEGER,
    last_ip TEXT,
    camera_time INTEGER,
    sd_free_kb INTEGER,
    sd_total_kb INTEGER,
    uploader_version TEXT,
    wifi_mode TEXT,
    log_tail TEXT,
    ap_ssid TEXT,
    ap_pass TEXT,
    lan_ip TEXT,
    local_web_port INTEGER
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    -- camera | uploading | received | drive_uploading | on_drive
    state TEXT NOT NULL DEFAULT 'camera',
    -- 1 = complete copy in INCOMING_DIR (waiting for Drive, or kept as cache)
    local INTEGER NOT NULL DEFAULT 0,
    upload_size INTEGER,
    requested INTEGER NOT NULL DEFAULT 0,
    drive_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at INTEGER NOT NULL,
    UNIQUE(device_id, path)
);
CREATE INDEX IF NOT EXISTS files_state ON files(state);
CREATE INDEX IF NOT EXISTS files_dev_rec ON files(device_id, recorded_at);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    -- pending | sent | done | failed
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_dev ON commands(device_id, status);

CREATE TABLE IF NOT EXISTS mail_queue (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    device_name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    recorded_at INTEGER,
    drive_path TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
"""

_init_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    with _init_lock:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        with get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _migrate(conn)


# Columns added after the first release: (table, column, definition).
_ADDED_COLUMNS = [
    ("users", "email", "TEXT"),
    ("users", "notify_uploads", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "notify_errors", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "notify_devices", "INTEGER NOT NULL DEFAULT 0"),
    ("files", "local", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "ap_ssid", "TEXT"),
    ("devices", "ap_pass", "TEXT"),
    ("devices", "lan_ip", "TEXT"),
    ("devices", "local_web_port", "INTEGER"),
]

# One-off data fixes run right after a column is added.
_AFTER_ADD = {
    ("files", "local"): "UPDATE files SET local=1 WHERE state IN ('received','drive_uploading')",
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))
            if (table, column) in _AFTER_ADD:
                conn.execute(_AFTER_ADD[(table, column)])


def now() -> int:
    return int(time.time())


def device_config(row: sqlite3.Row) -> dict:
    cfg = dict(config.DEFAULT_DEVICE_CONFIG)
    try:
        cfg.update(json.loads(row["config"] or "{}"))
    except ValueError:
        pass
    return cfg


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit(ts, actor, action, detail) VALUES (?,?,?,?)",
        (now(), actor, action, detail),
    )
