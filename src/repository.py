from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    is_baseline INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    invalidated_by_batch INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recalc_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('running','completed','failed')),
                    total_instruments INTEGER NOT NULL DEFAULT 0,
                    completed_instruments INTEGER NOT NULL DEFAULT 0,
                    failed_instruments INTEGER NOT NULL DEFAULT 0,
                    skipped_instruments INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '{{}}',
                    error TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instruments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    active_batch_id INTEGER REFERENCES recalc_batches(id) ON DELETE SET NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS certificates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
                    certificate_no TEXT NOT NULL,
                    coefficient REAL NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_certificates_instrument ON certificates(instrument_id);
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
                    certificate_id INTEGER REFERENCES certificates(id) ON DELETE SET NULL,
                    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                    raw_value REAL NOT NULL,
                    measured_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_readings_instrument ON readings(instrument_id);
                CREATE INDEX IF NOT EXISTS idx_readings_item ON readings(item_id);
                CREATE TABLE IF NOT EXISTS event_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    severity TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    certificate_id INTEGER REFERENCES certificates(id) ON DELETE SET NULL,
                    batch_id INTEGER REFERENCES recalc_batches(id) ON DELETE SET NULL,
                    reason TEXT NOT NULL
                        CHECK(reason IN ('initial','certificate_reissue','historical_baseline','reading_added')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, revision_no)
                );
                CREATE TABLE IF NOT EXISTS recalc_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES recalc_batches(id) ON DELETE CASCADE,
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
                    certificate_id INTEGER NOT NULL REFERENCES certificates(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','completed','failed','skipped')),
                    processed_events INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    completed_at TEXT,
                    UNIQUE(batch_id, instrument_id)
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    batch_id INTEGER REFERENCES recalc_batches(id) ON DELETE SET NULL,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('investigation','follow_up','deadline','conclusion_invalid')),
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','done')),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_todos_item ON todos(item_id);
            """)
        self._migrate()

    def _migrate(self) -> None:
        """为旧库补充新列，保证历史数据可升级。"""
        def columns(table: str) -> set:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {r[1] for r in rows}
        with self.conn:
            item_cols = columns("items")
            if "is_baseline" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0")
            record_cols = columns("records")
            if "invalidated" not in record_cols:
                self.conn.execute("ALTER TABLE records ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0")
            if "invalidated_by_batch" not in record_cols:
                self.conn.execute("ALTER TABLE records ADD COLUMN invalidated_by_batch INTEGER")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------- 仪器 ----------
    def create_instrument(self, code: str, name: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO instruments(code, name, created_by, created_at) VALUES(?,?,?,?)",
                    (code, name, actor, now),
                )
                instrument_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("仪器编号已存在") from exc
        return self.get_instrument(instrument_id)

    def get_instrument(self, instrument_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        if row is None:
            raise NotFoundError("仪器不存在")
        return dict(row)

    def get_instrument_by_code(self, code: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM instruments WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("仪器不存在")
        return dict(row)

    def list_instruments(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT i.*, (c.certificate_no) AS active_certificate_no
                   FROM instruments i
                   LEFT JOIN certificates c ON c.id = (
                       SELECT id FROM certificates WHERE instrument_id=i.id AND status='active'
                       ORDER BY version DESC LIMIT 1)
                   ORDER BY i.id""").fetchall()
        return [dict(row) for row in rows]

    def claim_instrument(self, instrument_id: int, batch_id: int) -> bool:
        """原子认领仪器：已被其他批次认领则返回False，两批不互相覆盖。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE instruments SET active_batch_id=? WHERE id=? AND active_batch_id IS NULL",
                (batch_id, instrument_id),
            )
            return cur.rowcount == 1

    def release_instrument(self, instrument_id: int, batch_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE instruments SET active_batch_id=NULL WHERE id=? AND active_batch_id=?",
                (instrument_id, batch_id),
            )

    # ---------- 校准证书 ----------
    def issue_certificate(self, instrument_id: int, certificate_no: str, coefficient: float,
                          effective_from: str, effective_to: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM certificates WHERE instrument_id=?",
                (instrument_id,)).fetchone()
            version = int(row["v"]) + 1
            cur = self.conn.execute(
                """INSERT INTO certificates(instrument_id, certificate_no, coefficient,
                   effective_from, effective_to, version, status, created_by, created_at)
                   VALUES(?,?,?,?,?,?,'active',?,?)""",
                (instrument_id, certificate_no, coefficient, effective_from, effective_to,
                 version, actor, now),
            )
            certificate_id = int(cur.lastrowid)
            # 新证书自生效日起替代旧的有效证书，旧证书保留为历史版本
            self.conn.execute(
                """UPDATE certificates SET status='superseded', effective_to=?
                   WHERE instrument_id=? AND id!=? AND status='active'
                     AND (effective_to IS NULL OR effective_to > ?)""",
                (effective_from, instrument_id, certificate_id, effective_from),
            )
        return self.get_certificate(certificate_id)

    def get_certificate(self, certificate_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM certificates WHERE id=?", (certificate_id,)).fetchone()
        if row is None:
            raise NotFoundError("校准证书不存在")
        return dict(row)

    def list_certificates(self, instrument_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM certificates WHERE instrument_id=? ORDER BY version DESC",
                (instrument_id,)).fetchall()
        return [dict(row) for row in rows]

    def active_certificate_at(self, instrument_id: int, at: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM certificates WHERE instrument_id=? AND status='active'
                   AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)
                   ORDER BY version DESC LIMIT 1""",
                (instrument_id, at, at),
            ).fetchone()
        return dict(row) if row else None

    # ---------- 原始读数 ----------
    def add_reading(self, instrument_id: int, item_id: Optional[int], raw_value: float,
                    measured_at: str, certificate_id: Optional[int], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO readings(instrument_id, certificate_id, item_id, raw_value,
                   measured_at, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (instrument_id, certificate_id, item_id, raw_value, measured_at, actor, now),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("原始读数不存在")
        return dict(row)

    def list_readings(self, instrument_id: Optional[int] = None,
                      item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM readings WHERE 1=1"
        params: tuple = ()
        if instrument_id is not None:
            sql += " AND instrument_id=?"; params += (instrument_id,)
        if item_id is not None:
            sql += " AND item_id=?"; params += (item_id,)
        sql += " ORDER BY measured_at, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def readings_in_interval(self, instrument_id: int, effective_from: str,
                             effective_to: Optional[str]) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM readings WHERE instrument_id=?
                   AND measured_at >= ? AND (? IS NULL OR measured_at <= ?)
                   ORDER BY measured_at, id""",
                (instrument_id, effective_from, effective_to, effective_to),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_reading_certificate(self, reading_id: int, certificate_id: Optional[int]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE readings SET certificate_id=? WHERE id=?",
                (certificate_id, reading_id),
            )

    # ---------- 剂量事件修订 ----------
    def create_event_revision(self, item_id: int, revision_no: int, quantity: float,
                              severity: str, threshold: float, certificate_id: Optional[int],
                              batch_id: Optional[int], reason: str, actor: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO event_revisions(item_id, revision_no, quantity, severity, threshold,
                   certificate_id, batch_id, reason, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, revision_no, quantity, severity, threshold, certificate_id,
                 batch_id, reason, actor, now),
            )

    def list_event_revisions(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT er.*, c.certificate_no AS certificate_no, c.coefficient AS coefficient
                   FROM event_revisions er
                   LEFT JOIN certificates c ON c.id = er.certificate_id
                   WHERE er.item_id=? ORDER BY er.revision_no""",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def latest_revision_no(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(revision_no) AS m FROM event_revisions WHERE item_id=?",
                (item_id,)).fetchone()
        return int(row["m"] or 0)

    def has_baseline_revision(self, item_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM event_revisions WHERE item_id=? AND reason='historical_baseline' LIMIT 1",
                (item_id,)).fetchone()
            return row is not None

    def mark_item_baseline(self, item_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET is_baseline=1, updated_at=? WHERE id=?",
                (utc_now(), item_id),
            )

    def update_item_quantity(self, item_id: int, quantity: float) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET quantity=?, version=version+1, updated_at=? WHERE id=?",
                (quantity, utc_now(), item_id),
            )
        return self.get_item(item_id)

    # ---------- 重算批次 ----------
    def create_batch(self, request_key: str, total_instruments: int,
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO recalc_batches(request_key, status, total_instruments, result,
                   created_by, created_at, updated_at)
                   VALUES(?, 'running', ?, '{}', ?, ?, ?)""",
                (request_key, total_instruments, actor, now, now),
            )
            batch_id = int(cur.lastrowid)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM recalc_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("重算批次不存在")
        batch = dict(row)
        batch["result"] = json.loads(batch["result"])
        return batch

    def get_batch_by_key(self, request_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recalc_batches WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM recalc_batches ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            batch = dict(row)
            batch["result"] = json.loads(batch["result"])
            result.append(batch)
        return result

    def add_batch_item(self, batch_id: int, instrument_id: int,
                       certificate_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO recalc_batch_items(batch_id, instrument_id, certificate_id, status)
                   VALUES(?,?,?, 'pending')""",
                (batch_id, instrument_id, certificate_id),
            )
            row_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recalc_batch_items WHERE id=?", (row_id,)).fetchone()
        return dict(row)

    def list_batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT rbi.*, i.code AS instrument_code, c.certificate_no AS certificate_no
                   FROM recalc_batch_items rbi
                   JOIN instruments i ON i.id = rbi.instrument_id
                   JOIN certificates c ON c.id = rbi.certificate_id
                   WHERE rbi.batch_id=? ORDER BY rbi.id""",
                (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_batch_item_status(self, batch_id: int, instrument_id: int, status: str,
                                 processed_events: Optional[int] = None,
                                 message: Optional[str] = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recalc_batch_items SET status=?,
                   processed_events=COALESCE(?, processed_events),
                   message=COALESCE(?, message),
                   completed_at=CASE WHEN ? IN ('completed','failed','skipped') THEN ? ELSE completed_at END
                   WHERE batch_id=? AND instrument_id=?""",
                (status, processed_events, message, status, utc_now(),
                 batch_id, instrument_id),
            )

    def update_batch_status(self, batch_id: int, status: str, completed: int, failed: int,
                            skipped: int, result: Dict[str, Any],
                            error: Optional[str] = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recalc_batches SET status=?, completed_instruments=?,
                   failed_instruments=?, skipped_instruments=?, result=?, error=?, updated_at=?
                   WHERE id=?""",
                (status, completed, failed, skipped,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), error,
                 utc_now(), batch_id),
            )

    # ---------- 待办 ----------
    def create_todo(self, item_id: int, batch_id: Optional[int], kind: str,
                    reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO todos(item_id, batch_id, kind, status, reason, created_by, created_at)
                   VALUES(?,?,?, 'open', ?, ?, ?)""",
                (item_id, batch_id, kind, reason, actor, now),
            )
            todo_id = int(cur.lastrowid)
        return self.get_todo(todo_id)

    def get_todo(self, todo_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        if row is None:
            raise NotFoundError("待办不存在")
        return dict(row)

    def list_todos(self, status: Optional[str] = None,
                   item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM todos WHERE 1=1"
        params: tuple = ()
        if status is not None:
            sql += " AND status=?"; params += (status,)
        if item_id is not None:
            sql += " AND item_id=?"; params += (item_id,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close_todo(self, todo_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE todos SET status='done', closed_at=? WHERE id=?",
                (utc_now(), todo_id),
            )
        return self.get_todo(todo_id)

    def invalidate_concluded_records(self, item_id: int, batch_id: int) -> int:
        """将已关闭的结论性记录标记失效，审计记录保持可查。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET invalidated=1, invalidated_by_batch=?
                   WHERE item_id=? AND status='closed' AND invalidated=0""",
                (batch_id, item_id),
            )
            return cur.rowcount
