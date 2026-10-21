"""SQLite persistence for projects, revisions, runs, messages, events, approvals, vision sessions.

Single local user, single backend process: one connection guarded by a lock,
WAL mode. Large artefacts (RTL, reports, bitstreams, waveforms, evidence) live
as files under runs/; rows store paths and compact summaries.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    current_revision_id TEXT,
    latest_run_id TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    number INTEGER NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    user_request TEXT NOT NULL DEFAULT '',
    generator TEXT NOT NULL DEFAULT '',
    interpreter TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    sim_status TEXT,
    build_status TEXT,
    dir TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    requirements_json TEXT NOT NULL DEFAULT '[]',
    observations_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    safety_json TEXT NOT NULL DEFAULT '[]',
    change_summary_json TEXT NOT NULL DEFAULT '[]',
    explanation TEXT NOT NULL DEFAULT '',
    simulation_json TEXT,
    build_json TEXT,
    verification_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_revisions_project ON revisions(project_id, number);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    trigger TEXT NOT NULL,
    request_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    revision_id TEXT,
    revision_ids_json TEXT NOT NULL DEFAULT '[]',
    stages_json TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_project ON runs(project_id, created_at);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    run_id TEXT,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_project ON messages(project_id, created_at);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    run_id TEXT,
    project_id TEXT,
    stage TEXT,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    project_id TEXT,
    created_at REAL NOT NULL,
    decided_at REAL,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS vision_sessions (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    dir TEXT NOT NULL,
    result_json TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
"""

JSON_COLUMNS = {
    "spec_json", "requirements_json", "observations_json", "files_json", "safety_json",
    "change_summary_json", "simulation_json", "build_json", "verification_json",
    "revision_ids_json", "stages_json", "options_json", "data_json", "details_json", "result_json",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_COLUMNS:
            out[key[:-5]] = json.loads(value) if value else None
        else:
            out[key] = value
    return out


def _encode(values: dict) -> dict:
    enc = {}
    for k, v in values.items():
        col = f"{k}_json" if f"{k}_json" in JSON_COLUMNS else k
        enc[col] = json.dumps(v, default=str) if col in JSON_COLUMNS and v is not None else v
    return enc


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- generic
    def insert(self, table: str, values: dict) -> None:
        enc = _encode(values)
        cols = ", ".join(enc)
        qs = ", ".join("?" for _ in enc)
        with self._lock:
            self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(enc.values()))

    def update(self, table: str, key: str, values: dict, key_col: str = "id") -> None:
        if not values:
            return
        enc = _encode(values)
        sets = ", ".join(f"{c} = ?" for c in enc)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", [*enc.values(), key])

    def get(self, table: str, key: str, key_col: str = "id") -> dict | None:
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
        return _decode(row)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode(r) for r in rows]  # type: ignore[misc]

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- events
    def append_event(self, ev: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (seq, ts, type, run_id, project_id, stage, data_json) VALUES (?,?,?,?,?,?,?)",
                (ev["seq"], ev["ts"], ev["type"], ev.get("run_id"), ev.get("project_id"), ev.get("stage"),
                 json.dumps(ev.get("data", {}), default=str)),
            )

    def events_after(self, after: int, limit: int = 2000, run_id: str | None = None) -> list[dict]:
        if run_id:
            rows = self.query(
                "SELECT * FROM (SELECT * FROM events WHERE seq > ? AND run_id = ? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (after, run_id, limit))
        else:
            rows = self.query(
                "SELECT * FROM (SELECT * FROM events WHERE seq > ? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (after, limit))
        return rows

    def max_event_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(seq), 0) FROM events") or 0)


def now() -> float:
    return time.time()
