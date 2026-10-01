from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (EFFECT_FINDING, EFFECT_TODO, REASON_RECALC, STATES)

EPS = 1e-9
BASELINE_VALID_FROM = "1970-01-01T00:00:00+00:00"
BATCH_PENDING, BATCH_RUNNING, BATCH_COMPLETED, BATCH_PARTIAL, BATCH_FAILED = (
    "pending", "running", "completed", "partial", "failed")
INSTRUMENT_RESUMABLE = ("pending", "skipped", "failed")


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._batch_guard = threading.Lock()
        self._batch_locks: Dict[int, threading.Lock] = {}
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    # ---------------------------------------------------------------- schema
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
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS instruments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calibration_certificates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                    certificate_no TEXT UNIQUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT,
                    coefficient REAL NOT NULL,
                    is_baseline INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    superseded_at TEXT,
                    superseded_by_certificate_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_certificates_instrument
                    ON calibration_certificates(instrument_id, valid_from);
                CREATE TABLE IF NOT EXISTS raw_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                    external_ref TEXT,
                    measured_at TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    applied_certificate_id INTEGER REFERENCES calibration_certificates(id),
                    coefficient REAL NOT NULL,
                    legacy_baseline INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_readings_instrument_time
                    ON raw_readings(instrument_id, measured_at);
                CREATE TABLE IF NOT EXISTS dose_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    seq INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    old_quantity REAL,
                    new_quantity REAL NOT NULL,
                    old_coefficient REAL,
                    new_coefficient REAL NOT NULL,
                    old_certificate_id INTEGER,
                    new_certificate_id INTEGER,
                    reading_id INTEGER NOT NULL REFERENCES raw_readings(id),
                    batch_id INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, seq)
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    kind TEXT NOT NULL CHECK(kind IN ('investigation','medical_follow_up','report_deadline')),
                    conclusion TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded')),
                    source_revision_id INTEGER REFERENCES dose_revisions(id),
                    superseded_by_revision_id INTEGER REFERENCES dose_revisions(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_findings_item ON findings(item_id);
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    kind TEXT NOT NULL CHECK(kind IN ('reassess_investigation','reassess_follow_up','report_deadline_changed')),
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    due_at TEXT,
                    source_revision_id INTEGER NOT NULL REFERENCES dose_revisions(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    UNIQUE(source_revision_id, kind)
                );
                CREATE TABLE IF NOT EXISTS recalc_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
                    window_from TEXT,
                    window_to TEXT,
                    reason TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '{{}}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recalc_batch_instruments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES recalc_batches(id),
                    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','completed','skipped','failed')),
                    detail TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, instrument_id)
                );
                CREATE INDEX IF NOT EXISTS ix_batch_instruments_instrument
                    ON recalc_batch_instruments(instrument_id, status);
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
            """)
            # 旧库补齐新增列，历史数据默认非基线、无需重评
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
            if "legacy_baseline" not in cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN legacy_baseline INTEGER NOT NULL DEFAULT 0")
            if "reassess_required" not in cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN reassess_required INTEGER NOT NULL DEFAULT 0")

    def _audit_row(self, action: str, entity_type: str, entity_id: int,
                   actor: str, detail: dict) -> None:
        """在当前事务内追加审计事件（不自行提交），保证审计链与业务改动同成同败。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]))

    # ----------------------------------------------------------------- items
    _ITEM_SELECT = """SELECT i.*, r.id AS reading_id, r.raw_value AS raw_value,
        r.measured_at AS measured_at, r.applied_certificate_id AS applied_certificate_id,
        r.coefficient AS reading_coefficient, r.legacy_baseline AS legacy_baseline
        FROM items i LEFT JOIN raw_readings r ON r.item_id=i.id"""

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, legacy_baseline: bool = False) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       legacy_baseline)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, 1 if legacy_baseline else 0),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def create_item_with_reading(self, title: str, description: str, severity: str,
                                 threshold: float, external_ref: Optional[str],
                                 instrument_id: int, reading_ref: Optional[str],
                                 measured_at: str, raw_value: float,
                                 certificate: Dict[str, Any], legacy_baseline: bool,
                                 reason: str, actor: str) -> Dict[str, Any]:
        """事件与原始读数同事务建立，并落地第1版剂量修订（读数→剂量事件链起点）。"""
        now = utc_now()
        quantity = round(raw_value * float(certificate["coefficient"]), 12)
        with self._lock, self.conn:
            self._require_instrument(instrument_id)
            try:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       legacy_baseline)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, 1 if legacy_baseline else 0))
                item_id = int(cur.lastrowid)
                rc = self.conn.execute(
                    """INSERT INTO raw_readings(item_id, instrument_id, external_ref,
                       measured_at, raw_value, applied_certificate_id, coefficient,
                       legacy_baseline, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, instrument_id, reading_ref, measured_at, raw_value,
                     certificate["id"], certificate["coefficient"],
                     1 if legacy_baseline else 0, actor, now))
                reading_id = int(rc.lastrowid)
                self.conn.execute(
                    """INSERT INTO dose_revisions(item_id, seq, reason, old_quantity,
                       new_quantity, old_coefficient, new_coefficient, old_certificate_id,
                       new_certificate_id, reading_id, batch_id, detail, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, 1, reason, None, quantity, None, certificate["coefficient"],
                     None, certificate["id"], reading_id, None,
                     ("历史基线证书，系数1.0" if legacy_baseline else "原始读数按证书系数换算"),
                     actor, now))
                self._audit_row("create", "剂量事件", item_id, actor, {
                    "title": title, "severity": severity, "quantity": quantity,
                    "instrument_id": instrument_id, "reading_id": reading_id,
                    "certificate_id": certificate["id"], "legacy_baseline": legacy_baseline})
            except sqlite3.IntegrityError as exc:
                raise ConflictError("唯一标识冲突：external_ref/reading_ref重复") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                self._ITEM_SELECT + " WHERE i.id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("剂量事件不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = self._ITEM_SELECT
        params: tuple = ()
        if status:
            sql += " WHERE i.status=?"
            params = (status,)
        sql += " ORDER BY i.id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, finding: Optional[tuple] = None) -> Dict[str, Any]:
        """状态流转；进入调查/随访/关闭时在同一事务落地对应结论。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version))
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("剂量事件不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            if finding is not None:
                self.conn.execute(
                    """INSERT INTO findings(item_id, kind, conclusion, status, created_by, created_at)
                       VALUES(?, ?, ?, 'active', ?, ?)""",
                    (item_id, finding[0], finding[1], actor, now))
        return self.get_item(item_id)

    def attach_reading(self, item_id: int, expected_version: int, instrument_id: int,
                       reading_ref: Optional[str], measured_at: str, raw_value: float,
                       certificate: Dict[str, Any], legacy_baseline: bool,
                       reason: str, actor: str,
                       effects_after) -> Dict[str, Any]:
        """为存量事件补挂原始读数；剂量变化沿修订链落地并使旧结论失效。"""
        from .rules import rule_effects
        now = utc_now()
        coefficient = float(certificate["coefficient"])
        new_quantity = round(raw_value * coefficient, 12)
        with self._lock, self.conn:
            item = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("剂量事件不存在")
            if self.conn.execute("SELECT 1 FROM raw_readings WHERE item_id=?", (item_id,)).fetchone():
                raise ConflictError("该剂量事件已存在原始读数")
            self._require_instrument(instrument_id)
            old_quantity = float(item["quantity"])
            cur = self.conn.execute(
                """UPDATE items SET quantity=?, version=version+1, updated_at=?,
                   legacy_baseline=? WHERE id=? AND version=?""",
                (new_quantity, now, 1 if legacy_baseline else 0, item_id, expected_version))
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            try:
                rc = self.conn.execute(
                    """INSERT INTO raw_readings(item_id, instrument_id, external_ref,
                       measured_at, raw_value, applied_certificate_id, coefficient,
                       legacy_baseline, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, instrument_id, reading_ref, measured_at, raw_value,
                     certificate["id"], coefficient, 1 if legacy_baseline else 0, actor, now))
                reading_id = int(rc.lastrowid)
                revision_id = self._insert_revision(
                    item_id, reason, None, old_quantity, None, coefficient, None,
                    certificate["id"], reading_id, None, actor, now,
                    "补挂原始读数：" + ("历史基线，系数1.0" if legacy_baseline else "按证书系数换算"))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("读数唯一标识已存在") from exc
            effects = []
            if abs(new_quantity - old_quantity) > EPS:
                effects = self._apply_effect_changes(
                    item, item_id, old_quantity, new_quantity, revision_id,
                    effects_after, actor, now)
            self._audit_row("attach_reading", "剂量事件", item_id, actor, {
                "reading_id": reading_id, "instrument_id": instrument_id,
                "certificate_id": certificate["id"], "legacy_baseline": legacy_baseline,
                "old_quantity": old_quantity, "new_quantity": new_quantity,
                "effects": effects})
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

    # ------------------------------------------------------------- instruments
    def _require_instrument(self, instrument_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        if row is None:
            raise NotFoundError("仪器不存在")
        return dict(row)

    def create_instrument(self, code: str, name: str, description: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO instruments(code, name, description, created_by, created_at)
                       VALUES(?,?,?,?,?)""", (code, name, description, actor, now))
                instrument_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("仪器编号已存在") from exc
        return self.get_instrument(instrument_id)

    def get_instrument(self, instrument_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        if row is None:
            raise NotFoundError("仪器不存在")
        return dict(row)

    def list_instruments(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM instruments ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ certificates
    def get_or_create_baseline_certificate(self, instrument_id: int,
                                           actor: str = "system") -> Dict[str, Any]:
        """旧数据升级：每台仪器一张系数1.0的历史基线证书。"""
        with self._lock, self.conn:
            self._require_instrument(instrument_id)
            row = self.conn.execute(
                "SELECT * FROM calibration_certificates WHERE instrument_id=? AND is_baseline=1",
                (instrument_id,)).fetchone()
            if row is not None:
                return dict(row)
            first = self.conn.execute(
                """SELECT valid_from FROM calibration_certificates
                   WHERE instrument_id=? AND is_baseline=0 AND superseded_at IS NULL
                   ORDER BY valid_from LIMIT 1""", (instrument_id,)).fetchone()
            valid_to = first["valid_from"] if first else None
            cur = self.conn.execute(
                """INSERT INTO calibration_certificates(instrument_id, certificate_no, version,
                   valid_from, valid_to, coefficient, is_baseline, note, created_by, created_at)
                   VALUES(?,NULL,1,?,?,1.0,1,'历史基线：旧数据无证书号，系数1.0',?,?)""",
                (instrument_id, BASELINE_VALID_FROM, valid_to, actor, utc_now()))
            return self.get_certificate(int(cur.lastrowid))

    def issue_certificate(self, instrument_id: int, certificate_no: Optional[str],
                          valid_from: str, valid_to: Optional[str], coefficient: float,
                          note: str, actor: str) -> Dict[str, Any]:
        """
        证书按仪器+生效区间形成版本：
        - 与现有有效证书区间重叠且生效起点不同 -> 拒绝；
        - 生效起点相同视为补发：旧证 superseded，新证 version+1（必须带新证书号）；
        - 首张正式证书收紧历史基线区间。
        """
        now = utc_now()
        with self._lock, self.conn:
            self._require_instrument(instrument_id)
            if valid_to is not None and valid_to <= valid_from:
                raise ValidationError("生效截止必须晚于生效起点")
            overlap = self.conn.execute(
                """SELECT id, version FROM calibration_certificates
                   WHERE instrument_id=? AND is_baseline=0 AND superseded_at IS NULL
                     AND valid_from<? AND (valid_to IS NULL OR valid_to>?)
                   ORDER BY valid_from LIMIT 1""",
                (instrument_id, valid_to or "9999-12-31T23:59:59+00:00", valid_from)).fetchone()
            same_start = self.conn.execute(
                """SELECT id, version FROM calibration_certificates
                   WHERE instrument_id=? AND is_baseline=0 AND superseded_at IS NULL
                     AND valid_from=? ORDER BY version DESC LIMIT 1""",
                (instrument_id, valid_from)).fetchone()
            if overlap is not None and same_start is None:
                raise ConflictError("证书生效区间与现有证书重叠，不能签发")
            if same_start is not None and certificate_no is None:
                raise ValidationError("补发证书必须提供新的证书号")
            if certificate_no is not None and self.conn.execute(
                    "SELECT 1 FROM calibration_certificates WHERE certificate_no=?",
                    (certificate_no,)).fetchone():
                raise ConflictError("证书号已存在")
            if same_start is not None:
                version = int(same_start["version"]) + 1
                old_row = self.conn.execute(
                    "SELECT valid_to FROM calibration_certificates WHERE id=?",
                    (same_start["id"],)).fetchone()
                old_valid_to = old_row["valid_to"]
                # 补发只换系数与证书号，生效区间必须与旧证一致
                if valid_to != old_valid_to:
                    raise ValidationError("补发证书不能改变生效区间，valid_to必须与旧证一致")
            else:
                version = 1
                old_valid_to = None
            cur = self.conn.execute(
                """INSERT INTO calibration_certificates(instrument_id, certificate_no, version,
                   valid_from, valid_to, coefficient, is_baseline, note, created_by, created_at)
                   VALUES(?,?,?,?,?,?,'0',?,?,?)""",
                (instrument_id, certificate_no, version, valid_from, valid_to,
                 coefficient, note, actor, now))
            certificate_id = int(cur.lastrowid)
            if same_start is not None:
                self.conn.execute(
                    """UPDATE calibration_certificates SET superseded_at=?,
                       superseded_by_certificate_id=? WHERE id=?""",
                    (now, certificate_id, same_start["id"]))
            # 新正式证书把历史基线收紧到其生效起点之前
            self.conn.execute(
                """UPDATE calibration_certificates SET valid_to=?
                   WHERE instrument_id=? AND is_baseline=1
                     AND (valid_to IS NULL OR valid_to>?)""",
                (valid_from, instrument_id, valid_from))
            self._audit_row("issue_certificate", "校准证书", certificate_id, actor, {
                "instrument_id": instrument_id, "certificate_no": certificate_no,
                "version": version, "valid_from": valid_from, "valid_to": valid_to,
                "coefficient": coefficient,
                "reissued": same_start["id"] if same_start is not None else None})
        return self.get_certificate(certificate_id)

    def get_certificate(self, certificate_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM calibration_certificates WHERE id=?",
                (certificate_id,)).fetchone()
        if row is None:
            raise NotFoundError("校准证书不存在")
        return dict(row)

    def get_certificate_by_no(self, instrument_id: int, certificate_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM calibration_certificates WHERE instrument_id=? AND certificate_no=?",
                (instrument_id, certificate_no)).fetchone()
        if row is None:
            raise NotFoundError("证书号不存在或不属于该仪器")
        return dict(row)

    def list_certificates(self, instrument_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM calibration_certificates"
        params: tuple = ()
        if instrument_id is not None:
            sql += " WHERE instrument_id=?"
            params = (instrument_id,)
        sql += " ORDER BY instrument_id, valid_from, version DESC, id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def applicable_certificate(self, instrument_id: int, measured_at: str) -> Optional[Dict[str, Any]]:
        """覆盖测量时刻的最新版本证书（正式证书优先于历史基线，被补发替换的旧版本排除）。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM calibration_certificates
                   WHERE instrument_id=? AND superseded_at IS NULL
                     AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
                   ORDER BY is_baseline ASC, version DESC, id DESC LIMIT 1""",
                (instrument_id, measured_at, measured_at)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- readings
    def list_readings(self, instrument_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM raw_readings"
        params: tuple = ()
        if instrument_id is not None:
            sql += " WHERE instrument_id=?"
            params = (instrument_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_reading_by_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM raw_readings WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- revisions
    def _insert_revision(self, item_id: int, reason: str, old_quantity: Optional[float],
                         new_quantity: float, old_coefficient: Optional[float],
                         new_coefficient: float, old_certificate_id: Optional[int],
                         new_certificate_id: Optional[int], reading_id: int,
                         batch_id: Optional[int], actor: str, now: str,
                         detail: str = "") -> int:
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM dose_revisions WHERE item_id=?",
            (item_id,)).fetchone()
        seq = int(seq_row["next_seq"])
        cur = self.conn.execute(
            """INSERT INTO dose_revisions(item_id, seq, reason, old_quantity, new_quantity,
               old_coefficient, new_coefficient, old_certificate_id, new_certificate_id,
               reading_id, batch_id, detail, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, seq, reason, old_quantity, new_quantity, old_coefficient,
             new_coefficient, old_certificate_id, new_certificate_id, reading_id,
             batch_id, detail, actor, now))
        return int(cur.lastrowid)

    def list_revisions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_revisions WHERE item_id=? ORDER BY seq",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def _apply_effect_changes(self, item: sqlite3.Row, item_id: int,
                              old_quantity: float, new_quantity: float,
                              revision_id: int, effects_after: dict, actor: str,
                              now: str) -> List[Dict[str, Any]]:
        """
        新结果改变调查/医学随访/报告期限时：旧结论置superseded（原审计记录仍可查），
        生成待办；已关闭事件叠加reassess_required标记。
        """
        from .rules import changed_effects, rule_effects
        before = rule_effects(item["severity"], old_quantity, item["threshold"])
        changes = changed_effects(before, effects_after)
        recorded = []
        for key, change in changes.items():
            finding_kind = EFFECT_FINDING[key]
            todo_kind = EFFECT_TODO[key]
            self.conn.execute(
                """UPDATE findings SET status='superseded', superseded_at=?,
                   superseded_by_revision_id=?
                   WHERE item_id=? AND kind=? AND status='active'""",
                (now, revision_id, item_id, finding_kind))
            due_at = effects_after["deadline_hours"] if key == "deadline_hours" else None
            detail = {"effect": key, "before": change["before"], "after": change["after"]}
            if key == "deadline_hours":
                detail["message"] = "报告期限因剂量修订变化，需重新评估并按期报告"
                due_at = None
            self.conn.execute(
                """INSERT OR IGNORE INTO todos(item_id, kind, detail, due_at,
                   source_revision_id, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (item_id, todo_kind, json.dumps(detail, ensure_ascii=False),
                 due_at, revision_id, actor, now))
            recorded.append({"effect": key, "before": change["before"], "after": change["after"]})
        if recorded and item["status"] == "closed":
            self.conn.execute(
                "UPDATE items SET reassess_required=1 WHERE id=?", (item_id,))
        return recorded

    # -------------------------------------------------------------- findings
    def list_findings(self, item_id: Optional[int] = None,
                      status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM findings WHERE 1=1"
        params: List[Any] = []
        if item_id is not None:
            sql += " AND item_id=?"; params.append(item_id)
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------------- todos
    def list_todos(self, item_id: Optional[int] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM todos WHERE 1=1"
        params: List[Any] = []
        if item_id is not None:
            sql += " AND item_id=?"; params.append(item_id)
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            todo = dict(row); todo["detail"] = json.loads(todo["detail"])
            result.append(todo)
        return result

    def close_todo(self, todo_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE todos SET status='closed', closed_at=? WHERE id=? AND status='open'",
                (now, todo_id))
            if cur.rowcount == 0:
                if self.conn.execute("SELECT 1 FROM todos WHERE id=?", (todo_id,)).fetchone() is None:
                    raise NotFoundError("待办不存在")
                raise ConflictError("待办已关闭")
        with self._lock:
            row = self.conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        todo = dict(row); todo["detail"] = json.loads(todo["detail"])
        return todo

    # --------------------------------------------------------- recalc batches
    def batch_lock(self, batch_id: int) -> threading.Lock:
        """同一批次在进程内串行执行（跨批次的互斥由认领槽位在数据库层保证）。"""
        with self._batch_guard:
            lock = self._batch_locks.get(batch_id)
            if lock is None:
                lock = threading.Lock(); self._batch_locks[batch_id] = lock
            return lock

    def create_batch(self, request_id: str, instrument_ids: List[int],
                     window_from: Optional[str], window_to: Optional[str],
                     reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            for instrument_id in instrument_ids:
                self._require_instrument(instrument_id)
            status = BATCH_RUNNING if instrument_ids else BATCH_COMPLETED
            cur = self.conn.execute(
                """INSERT INTO recalc_batches(request_id, status, window_from, window_to,
                   reason, summary, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?, '{}', ?,?,?)""",
                (request_id, status, window_from, window_to, reason, actor, now, now))
            batch_id = int(cur.lastrowid)
            for instrument_id in instrument_ids:
                self.conn.execute(
                    """INSERT INTO recalc_batch_instruments(batch_id, instrument_id,
                       status, updated_at) VALUES(?,?,'pending',?)""",
                    (batch_id, instrument_id, now))
            self._audit_row("recalc_batch_submit", "重算批次", batch_id, actor, {
                "request_id": request_id, "instrument_ids": instrument_ids,
                "window_from": window_from, "window_to": window_to, "reason": reason})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recalc_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("重算批次不存在")
        batch = dict(row); batch["summary"] = json.loads(batch["summary"] or "{}")
        return batch

    def get_batch_by_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM recalc_batches WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        batch = dict(row); batch["summary"] = json.loads(batch["summary"] or "{}")
        return batch

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM recalc_batches ORDER BY id").fetchall()
        result = []
        for row in rows:
            batch = dict(row); batch["summary"] = json.loads(batch["summary"] or "{}")
            result.append(batch)
        return result

    def list_batch_instruments(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM recalc_batch_instruments WHERE batch_id=? ORDER BY id",
                (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def claim_instrument(self, batch_id: int, instrument_id: int) -> bool:
        """
        认领仪器槽位：本批次未完成的槽位可认领；其它批次正在处理同一仪器时不可认领
        （返回False -> skipped），从而两批并发重算互不覆盖。
        """
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recalc_batch_instruments SET status='pending', updated_at=?
                   WHERE batch_id=? AND instrument_id=? AND status='running'""",
                (now, batch_id, instrument_id))
            cur = self.conn.execute(
                """UPDATE recalc_batch_instruments SET status='running', started_at=?,
                   updated_at=?, detail='认领成功，开始重算'
                   WHERE batch_id=? AND instrument_id=?
                     AND status IN ('pending','skipped','failed')
                     AND NOT EXISTS (
                       SELECT 1 FROM recalc_batch_instruments other
                       WHERE other.instrument_id=recalc_batch_instruments.instrument_id
                         AND other.status='running' AND other.batch_id<>?)""",
                (now, now, batch_id, instrument_id, batch_id))
            return cur.rowcount == 1

    def mark_instrument(self, batch_id: int, instrument_id: int, status: str,
                        detail: str) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE recalc_batch_instruments SET status=?, detail=?, finished_at=?,
                   updated_at=? WHERE batch_id=? AND instrument_id=?""",
                (status, detail, now, now, batch_id, instrument_id))

    def save_batch_result(self, batch_id: int, status: str, summary: dict) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE recalc_batches SET status=?, summary=?, updated_at=? WHERE id=?",
                (status, json.dumps(summary, ensure_ascii=False, sort_keys=True), now, batch_id))

    def recalc_instrument(self, batch_id: int, instrument_id: int,
                          window_from: Optional[str], window_to: Optional[str],
                          actor: str) -> Dict[str, Any]:
        """
        单台仪器的重算事务：按当前有效证书版本重新换算区间内全部读数，沿修订链追加
        dose_revisions；剂量变化同步使旧结论失效、生成待办。历史基线读数不随补发重算。
        """
        now = utc_now()
        with self._lock, self.conn:
            sql = """SELECT r.*, i.severity AS severity, i.threshold AS threshold,
                       i.quantity AS old_quantity, i.status AS item_status
                     FROM raw_readings r JOIN items i ON i.id=r.item_id
                     WHERE r.instrument_id=? AND r.legacy_baseline=0"""
            params: List[Any] = [instrument_id]
            if window_from is not None:
                sql += " AND r.measured_at>=?"; params.append(window_from)
            if window_to is not None:
                sql += " AND r.measured_at<=?"; params.append(window_to)
            sql += " ORDER BY r.measured_at, r.id"
            readings = self.conn.execute(sql, tuple(params)).fetchall()

            stats = {"instrument_id": instrument_id, "scanned": len(readings),
                     "revised": 0, "unchanged": 0, "skipped": 0, "items": []}
            for reading in readings:
                item_id = int(reading["item_id"])
                cert_row = self.conn.execute(
                    """SELECT * FROM calibration_certificates
                       WHERE instrument_id=? AND superseded_at IS NULL
                         AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
                       ORDER BY is_baseline ASC, version DESC, id DESC LIMIT 1""",
                    (instrument_id, reading["measured_at"], reading["measured_at"])).fetchone()
                if cert_row is None:
                    stats["skipped"] += 1
                    stats["items"].append({"item_id": item_id, "result": "skipped",
                                           "reason": "测量时刻无有效证书"})
                    continue
                cert = dict(cert_row)
                old_quantity = float(reading["old_quantity"])
                old_cert_id = reading["applied_certificate_id"]
                old_coefficient = float(reading["coefficient"])
                new_coefficient = float(cert["coefficient"])
                new_quantity = round(float(reading["raw_value"]) * new_coefficient, 12)
                cert_changed = int(cert["id"]) != int(old_cert_id or -1)
                if abs(new_quantity - old_quantity) <= EPS:
                    # 剂量未变：仅同步读数上的证书引用，不追加剂量修订
                    if cert_changed:
                        self.conn.execute(
                            """UPDATE raw_readings SET applied_certificate_id=?, coefficient=?
                               WHERE id=?""", (cert["id"], new_coefficient, reading["id"]))
                    stats["unchanged"] += 1
                    stats["items"].append({"item_id": item_id, "result": "unchanged"})
                    continue
                # 乐观锁更新剂量；同仪器认领槽位保证不会有并发批次同行竞争
                cur = self.conn.execute(
                    """UPDATE items SET quantity=?, version=version+1, updated_at=?
                       WHERE id=? AND quantity=?""",
                    (new_quantity, now, item_id, old_quantity))
                if cur.rowcount == 0:
                    stats["skipped"] += 1
                    stats["items"].append({"item_id": item_id, "result": "skipped",
                                           "reason": "剂量已被其他修订修改"})
                    continue
                self.conn.execute(
                    """UPDATE raw_readings SET applied_certificate_id=?, coefficient=?
                       WHERE id=?""", (cert["id"], new_coefficient, reading["id"]))
                revision_id = self._insert_revision(
                    item_id, REASON_RECALC, old_quantity, new_quantity,
                    old_coefficient, new_coefficient, old_cert_id, cert["id"],
                    int(reading["id"]), batch_id, actor, now,
                    f"证书补发重算：证书{cert.get('certificate_no') or cert['id']}，版本v{cert['version']}")
                from .rules import rule_effects
                effects_after = rule_effects(
                    reading["severity"], new_quantity, reading["threshold"])
                item_stub = {"severity": reading["severity"],
                             "threshold": reading["threshold"],
                             "status": reading["item_status"]}
                effects = self._apply_effect_changes(
                    item_stub, item_id, old_quantity, new_quantity, revision_id,
                    effects_after, actor, now)
                stats["revised"] += 1
                stats["items"].append({"item_id": item_id, "result": "revised",
                                       "old_quantity": old_quantity,
                                       "new_quantity": new_quantity,
                                       "old_certificate_id": old_cert_id,
                                       "new_certificate_id": cert["id"],
                                       "revision_id": revision_id, "effects": effects})
            self._audit_row("recalc_instrument", "重算批次", batch_id, actor, stats)
        return stats

    # ----------------------------------------------------------------- audit
    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            self._audit_row(action, entity_type, entity_id, actor, detail)
            row = self.conn.execute(
                "SELECT * FROM audit_events WHERE entity_id=? ORDER BY id DESC LIMIT 1",
                (entity_id,)).fetchone()
            event = dict(row)
        event["detail"] = json.loads(event["detail"])
        return event

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: List[Any] = []
        if entity_id is not None:
            sql += " AND entity_id=?"; params.append(entity_id)
        if entity_type is not None:
            sql += " AND entity_type=?"; params.append(entity_type)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
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
