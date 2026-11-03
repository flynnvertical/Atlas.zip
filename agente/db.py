"""Persistencia SQLite: estado del agente, trabajos, memoria, aprobaciones y auditoría."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- preference|instruction|project|result|experience|fact
    project TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL,          -- user|agent
    confidence TEXT NOT NULL DEFAULT 'media',
    chat_id INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    superseded_by INTEGER,
    expires REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, project, kind, content='memories', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, project, kind) VALUES (new.id, new.content, new.project, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, project, kind) VALUES('delete', old.id, old.content, old.project, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, project, kind) VALUES('delete', old.id, old.content, old.project, old.kind);
  INSERT INTO memories_fts(rowid, content, project, kind) VALUES (new.id, new.content, new.project, new.kind);
END;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued|running|waiting_approval|done|failed|cancelled
    plan TEXT,
    messages TEXT,                 -- checkpoint de la conversación (JSON)
    counters TEXT,                 -- consumo de presupuesto (JSON)
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    origin TEXT NOT NULL DEFAULT 'user',
    dedupe_key TEXT UNIQUE,
    created REAL NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending|approved|rejected|executed|expired
    created REAL NOT NULL,
    decided REAL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    objective TEXT NOT NULL,
    spec TEXT NOT NULL,            -- 'every:<min>' | 'daily:HH:MM'
    next_run REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_job INTEGER,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT,
    file_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    attempts INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);

-- Buzón de entrada del webhook: se escribe ANTES de confirmar a Telegram (no se pierde si se reinicia).
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    received REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_updates (update_id INTEGER PRIMARY KEY, ts REAL NOT NULL);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        # DELETE (no WAL): el archivo único se cifra y versiona entre ejecuciones.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        if self.kv_get("schema_version") is None:
            self.kv_set("schema_version", str(SCHEMA_VERSION))

    # -- utilidades -----------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def log(self, level: str, kind: str, detail: Any = None, job_id: int | None = None) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        self.execute(
            "INSERT INTO events(ts,level,kind,job_id,detail) VALUES(?,?,?,?,?)",
            (time.time(), level, kind, job_id, (detail or "")[:4000]),
        )

    def close(self) -> None:
        self.conn.close()
