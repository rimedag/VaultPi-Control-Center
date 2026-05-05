PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    category TEXT,
    type TEXT,
    environment TEXT,
    stack TEXT,
    repo_url TEXT,
    local_url TEXT,
    remote_url TEXT,
    healthcheck_url TEXT,
    host_machine TEXT,
    port INTEGER,
    run_command TEXT,
    stop_command TEXT,
    restart_command TEXT,
    log_path TEXT,
    working_directory TEXT,
    notes TEXT,
    tags TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 100,
    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
    action_enabled INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    response_ms INTEGER,
    error_text TEXT,
    checker TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_service_checks_project_checked ON service_checks(project_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id INTEGER,
    actor TEXT,
    message TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    command TEXT NOT NULL,
    working_directory TEXT,
    timeout_sec INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ssid TEXT NOT NULL UNIQUE,
    psk TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL
);
