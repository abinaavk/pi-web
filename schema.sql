-- schema.sql
-- Run once: sqlite3 piweb.db < schema.sql

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- password_hash is set on first run via setup_password.py, not hardcoded here.

CREATE TABLE IF NOT EXISTS machines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    os_type       TEXT NOT NULL CHECK (os_type IN ('alpine', 'debian', 'ubuntu')),
    ram_mb        INTEGER NOT NULL DEFAULT 2048,
    disk_gb       INTEGER NOT NULL DEFAULT 8,
    disk_path     TEXT NOT NULL,
    vnc_port      INTEGER,
    websock_port  INTEGER,
    status        TEXT NOT NULL DEFAULT 'stopped' CHECK (status IN ('stopped', 'running', 'starting', 'stopping')),
    pid           INTEGER,
    ws_pid        INTEGER,
    guest_username      TEXT NOT NULL,
    guest_password_hash TEXT NOT NULL,   -- SHA-512 crypt hash only, never plaintext
    seed_path           TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_active   TEXT
);

-- Only one row will ever have status='running' or 'starting' at a time —
-- enforced in application logic (vm_manager.py), not by the DB, since
-- SQLite CHECK constraints can't reference other rows.
