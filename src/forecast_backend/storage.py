"""SQLite 持久化层：所有业务状态落盘，进程重启后可继续。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
  institution_id TEXT PRIMARY KEY,
  display_name   TEXT NOT NULL,
  accredited     INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
  round_id   TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  opens_at   TEXT NOT NULL,
  closes_at  TEXT NOT NULL,
  status     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id    TEXT PRIMARY KEY,
  payload_hash  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id    TEXT NOT NULL,
  existing_hash TEXT NOT NULL,
  incoming_hash TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  reason        TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  submission_id   TEXT PRIMARY KEY,
  institution_id  TEXT NOT NULL,
  indicator       TEXT NOT NULL,
  target_year     INTEGER NOT NULL,
  round_id        TEXT NOT NULL,
  value           REAL NOT NULL,
  lower           REAL,
  upper           REAL,
  confidence      REAL,
  rationale       TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  supersedes      TEXT,
  status          TEXT NOT NULL,
  submitted_at    TEXT NOT NULL,
  withdrawn_at    TEXT,
  withdraw_reason TEXT,
  receipt_id      TEXT NOT NULL,
  UNIQUE (institution_id, indicator, target_year, round_id, revision)
);
CREATE TABLE IF NOT EXISTS rules (
  rule_id         TEXT NOT NULL,
  version         INTEGER NOT NULL,
  scope_indicator TEXT,
  definition_json TEXT NOT NULL,
  status          TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  submitted_at    TEXT,
  approved_by     TEXT,
  approved_at     TEXT,
  PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS publications (
  publication_id TEXT PRIMARY KEY,
  round_id       TEXT NOT NULL,
  indicator      TEXT NOT NULL,
  target_year    INTEGER NOT NULL,
  vintage        INTEGER NOT NULL,
  rule_id        TEXT NOT NULL,
  rule_version   INTEGER NOT NULL,
  status         TEXT NOT NULL,
  cutoff_at      TEXT NOT NULL,
  computed_json  TEXT NOT NULL,
  sample_json    TEXT NOT NULL,
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  approved_by    TEXT,
  published_at   TEXT,
  UNIQUE (round_id, indicator, target_year, vintage)
);
CREATE TABLE IF NOT EXISTS actuals (
  indicator    TEXT NOT NULL,
  target_year  INTEGER NOT NULL,
  value        REAL NOT NULL,
  published_at TEXT NOT NULL,
  recorded_by  TEXT NOT NULL,
  PRIMARY KEY (indicator, target_year)
);
CREATE TABLE IF NOT EXISTS errors (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id TEXT NOT NULL,
  indicator      TEXT NOT NULL,
  target_year    INTEGER NOT NULL,
  scope          TEXT NOT NULL,
  subject        TEXT NOT NULL,
  metric         TEXT NOT NULL,
  value          REAL NOT NULL,
  computed_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtests (
  backtest_id  TEXT PRIMARY KEY,
  rule_id      TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  indicator    TEXT NOT NULL,
  target_year  INTEGER NOT NULL,
  result_json  TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_flags (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  indicator       TEXT NOT NULL,
  target_year     INTEGER NOT NULL,
  round_id        TEXT,
  reason          TEXT NOT NULL,
  detail_json     TEXT NOT NULL,
  status          TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  resolved_at     TEXT,
  resolved_by     TEXT,
  resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
  reminder_id  TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  due_at       TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  fired_at     TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  entity_id  TEXT NOT NULL,
  at         TEXT NOT NULL,
  actor      TEXT,
  data_json  TEXT NOT NULL
);
"""


class Store:
    """对单文件 SQLite 的薄封装；单连接加锁，天然串行化写入。"""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def insert(self, table: str, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )

    def update(self, table: str, changes: dict[str, Any], where: str, params: tuple[Any, ...]) -> int:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE {table} SET {assignments} WHERE {where}",
                tuple(changes.values()) + params,
            )
        return cursor.rowcount

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """显式事务；调用方不得在事务内再开事务。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
