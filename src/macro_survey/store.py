"""SQLite 持久化层：schema、连接与事务原语。

所有时间以带时区的 ISO-8601 UTC 字符串保存，字典序即可比较先后。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS institutions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  institution_id TEXT REFERENCES institutions(id),
  username TEXT NOT NULL UNIQUE,
  roles TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_tokens (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  issued_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS indicators (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  unit TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  opens_at TEXT NOT NULL,
  submission_deadline TEXT NOT NULL,
  publish_at TEXT NOT NULL,
  years_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS institution_weights (
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  institution_id TEXT NOT NULL REFERENCES institutions(id),
  weight REAL NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (indicator_id, institution_id)
);

CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY,
  institution_id TEXT NOT NULL REFERENCES institutions(id),
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  forecast_year INTEGER NOT NULL,
  round_id TEXT NOT NULL REFERENCES rounds(id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('upsert','withdraw')),
  point_value REAL,
  low REAL,
  high REAL,
  confidence REAL,
  rationale TEXT,
  client_ref TEXT NOT NULL,
  idempotency_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','superseded','withdrawn','late')),
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES users(id),
  supersedes_id TEXT REFERENCES submissions(id),
  prev_hash TEXT,
  row_hash TEXT NOT NULL,
  UNIQUE(round_id, indicator_id, forecast_year, institution_id, seq),
  UNIQUE(institution_id, client_ref)
);
CREATE INDEX IF NOT EXISTS idx_sub_target
  ON submissions(round_id, indicator_id, forecast_year, institution_id);

CREATE TABLE IF NOT EXISTS idempotent_receipts (
  institution_id TEXT NOT NULL REFERENCES institutions(id),
  client_ref TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  response_json TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (institution_id, client_ref)
);

CREATE TABLE IF NOT EXISTS quarantined_messages (
  id TEXT PRIMARY KEY,
  institution_id TEXT NOT NULL REFERENCES institutions(id),
  round_id TEXT,
  indicator_id TEXT,
  client_ref TEXT,
  reason TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','discarded','accepted')),
  resolution_note TEXT,
  resolved_by TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS rule_versions (
  id TEXT PRIMARY KEY,
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  version INTEGER NOT NULL,
  params_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','approved','rejected')),
  created_by TEXT NOT NULL,
  created_by_name TEXT NOT NULL,
  approved_by TEXT,
  approved_by_name TEXT,
  created_at TEXT NOT NULL,
  approved_at TEXT,
  reject_reason TEXT,
  UNIQUE(indicator_id, version)
);

CREATE TABLE IF NOT EXISTS publications (
  id TEXT PRIMARY KEY,
  round_id TEXT NOT NULL REFERENCES rounds(id),
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  forecast_year INTEGER NOT NULL,
  rule_version_id TEXT NOT NULL REFERENCES rule_versions(id),
  consensus_value REAL,
  status TEXT NOT NULL
    CHECK (status IN ('pending_approval','published','rejected')),
  requested_by TEXT NOT NULL,
  watermark_at TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  published_at TEXT,
  reject_reason TEXT,
  n_samples INTEGER NOT NULL DEFAULT 0,
  n_included INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pub_target ON publications(round_id, indicator_id, forecast_year);
-- 同一目标至多一份待批或已发布的发布单；驳回后允许重新申请（驳回记录留档）
CREATE UNIQUE INDEX IF NOT EXISTS uq_pub_active
  ON publications(round_id, indicator_id, forecast_year)
  WHERE status IN ('pending_approval','published');

CREATE TABLE IF NOT EXISTS publication_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id TEXT NOT NULL REFERENCES publications(id),
  institution_id TEXT NOT NULL,
  anon_code TEXT NOT NULL,
  revision_seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  point_value REAL,
  low REAL,
  high REAL,
  confidence REAL,
  rationale TEXT,
  effective_value REAL,
  included INTEGER NOT NULL,
  excluded_reason TEXT,
  weight REAL NOT NULL,
  submitted_at TEXT NOT NULL,
  row_hash TEXT,
  UNIQUE(publication_id, institution_id, revision_seq)
);
CREATE INDEX IF NOT EXISTS idx_ps_pub ON publication_samples(publication_id);

CREATE TABLE IF NOT EXISTS actuals (
  id TEXT PRIMARY KEY,
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  forecast_year INTEGER NOT NULL,
  actual_value REAL NOT NULL,
  review_threshold REAL,
  released_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(indicator_id, forecast_year)
);

CREATE TABLE IF NOT EXISTS forecast_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id TEXT NOT NULL UNIQUE REFERENCES publications(id),
  actual_value REAL NOT NULL,
  error REAL NOT NULL,
  abs_error REAL NOT NULL,
  pct_error REAL,
  rule_version_id TEXT NOT NULL,
  rule_params_json TEXT NOT NULL,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contributor_deviations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publication_id TEXT NOT NULL REFERENCES publications(id),
  institution_id TEXT NOT NULL,
  anon_code TEXT NOT NULL,
  forecast_value REAL,
  actual_value REAL NOT NULL,
  abs_error REAL,
  threshold REAL NOT NULL,
  needs_review INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(publication_id, institution_id)
);
CREATE INDEX IF NOT EXISTS idx_dev_review ON contributor_deviations(needs_review);

CREATE TABLE IF NOT EXISTS rule_backtests (
  id TEXT PRIMARY KEY,
  rule_version_id TEXT NOT NULL REFERENCES rule_versions(id),
  indicator_id TEXT NOT NULL REFERENCES indicators(id),
  forecast_year INTEGER NOT NULL,
  n_publications INTEGER NOT NULL,
  mae REAL NOT NULL,
  rmse REAL NOT NULL,
  bias REAL NOT NULL,
  interval_coverage REAL,
  computed_at TEXT NOT NULL,
  UNIQUE(rule_version_id, indicator_id, forecast_year)
);

CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY,
  due_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref_id TEXT,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due','delivered','cancelled')),
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_name TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT,
  detail_json TEXT
);
"""


class Store:
    """薄数据访问层。业务逻辑在 service 层。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # isolation_level=None：语句自动提交，复合操作自行 BEGIN/COMMIT，
        # 避免写操作停留在隐式事务里，进程重启后丢失。
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._tx_depth = 0
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # -- 基础工具 -----------------------------------------------------------

    def begin(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    @contextmanager
    def transaction(self):
        """复合写操作的原子边界；嵌套时退化为 SAVEPOINT。"""
        if not getattr(self, "_tx_depth", 0):
            self._tx_depth = 0
        if self._tx_depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield
                self.conn.execute("COMMIT")
                self._tx_depth = 0
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                self._tx_depth = 0
                raise
        else:
            sp = f"sp_{self._tx_depth}"
            self.conn.execute(f"SAVEPOINT {sp}")
            self._tx_depth += 1
            try:
                yield
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                self._tx_depth -= 1
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                self._tx_depth -= 1
                raise

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        # 容忍"已提交后再回滚"的路径，避免掩盖真正的业务异常
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
