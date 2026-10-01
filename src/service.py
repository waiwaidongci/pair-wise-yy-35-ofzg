from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BASELINE_UPGRADE_ROLES, CERTIFICATE_WRITE_ROLES,
                    CONCLUDED_STATES, CREATE_ROLES, ENTITY, ENTITY_BATCH,
                    ENTITY_CERTIFICATE, ENTITY_INSTRUMENT, ENTITY_READING, ENTITY_TODO,
                    INSTRUMENT_WRITE_ROLES, READING_WRITE_ROLES, RECALC_ROLES,
                    RECORD_ROLES, TODO_CLOSE_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, todo_kind_label, validate_certificate_interval,
                    validate_coefficient, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        readings = self._validate_readings_payload(payload.get("readings"))
        if readings:
            # 剂量由原始读数按证书系数计算，无证书的读数按历史基线（系数1.0）处理
            quantity = self._compute_readings_quantity(readings)
        else:
            quantity = require_number(payload.get("quantity", 0), "quantity")
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        if readings:
            first_cert = None
            for r in readings:
                self.repository.add_reading(r["instrument_id"], item["id"], r["raw_value"],
                                            r["measured_at"], r["certificate_id"], actor)
                if first_cert is None and r["certificate_id"] is not None:
                    first_cert = r["certificate_id"]
            self.repository.create_event_revision(
                item["id"], 1, quantity, severity, threshold, first_cert, None, "initial", actor)
            if all(r["certificate_id"] is None for r in readings):
                self.repository.mark_item_baseline(item["id"])
                item = self.repository.get_item(item["id"])
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "readings": len(readings),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 仪器 ----------
    def create_instrument(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, INSTRUMENT_WRITE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        name = require_text(payload.get("name"), "name", 200)
        instrument = self.repository.create_instrument(code, name, actor)
        self.repository.append_audit("create", ENTITY_INSTRUMENT, instrument["id"], actor,
                                     {"code": code, "name": name})
        return instrument

    def list_instruments(self, role: str) -> list:
        self._view(role)
        return self.repository.list_instruments()

    # ---------- 校准证书 ----------
    def issue_certificate(self, instrument_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CERTIFICATE_WRITE_ROLES)
        actor = require_text(actor, "actor", 100)
        instrument = self.repository.get_instrument(instrument_id)
        certificate_no = require_text(payload.get("certificate_no"), "certificate_no", 100)
        coefficient = validate_coefficient(payload.get("coefficient"))
        effective_from, effective_to = validate_certificate_interval(
            payload.get("effective_from"), payload.get("effective_to"))
        certificate = self.repository.issue_certificate(
            instrument_id, certificate_no, coefficient, effective_from, effective_to, actor)
        self.repository.append_audit("issue", ENTITY_CERTIFICATE, certificate["id"], actor, {
            "instrument_id": instrument["id"], "certificate_no": certificate_no,
            "coefficient": coefficient, "effective_from": effective_from,
            "effective_to": effective_to, "version": certificate["version"],
        })
        return certificate

    def list_certificates(self, instrument_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_instrument(instrument_id)
        return self.repository.list_certificates(instrument_id)

    # ---------- 原始读数 ----------
    def _validate_readings_payload(self, raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        if not isinstance(raw, list) or not raw:
            raise ValidationError("readings必须是非空数组")
        readings = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValidationError("readings元素必须是对象")
            instrument_id = entry.get("instrument_id")
            if not isinstance(instrument_id, int) or instrument_id < 1:
                raise ValidationError("instrument_id必须是正整数")
            raw_value = require_number(entry.get("raw_value"), "raw_value")
            measured_at = require_text(entry.get("measured_at"), "measured_at", 100)
            certificate_id = entry.get("certificate_id")
            if certificate_id is not None:
                if not isinstance(certificate_id, int) or certificate_id < 1:
                    raise ValidationError("certificate_id必须是正整数")
            readings.append({
                "instrument_id": instrument_id, "raw_value": raw_value,
                "measured_at": measured_at, "certificate_id": certificate_id,
            })
        return readings

    def _compute_readings_quantity(self, readings: List[Dict[str, Any]]) -> float:
        total = 0.0
        for r in readings:
            certificate = None
            if r["certificate_id"] is not None:
                certificate = self.repository.get_certificate(r["certificate_id"])
                if certificate["instrument_id"] != r["instrument_id"]:
                    raise ValidationError("证书与仪器不匹配")
            coefficient = certificate["coefficient"] if certificate else 1.0
            total += r["raw_value"] * coefficient
        return total

    def add_reading(self, instrument_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_WRITE_ROLES)
        actor = require_text(actor, "actor", 100)
        instrument = self.repository.get_instrument(instrument_id)
        raw_value = require_number(payload.get("raw_value"), "raw_value")
        measured_at = require_text(payload.get("measured_at"), "measured_at", 100)
        item_id = payload.get("item_id")
        if item_id is not None:
            if not isinstance(item_id, int) or item_id < 1:
                raise ValidationError("item_id必须是正整数")
            self.repository.get_item(item_id)
        certificate_id = payload.get("certificate_id")
        if certificate_id is not None:
            if not isinstance(certificate_id, int) or certificate_id < 1:
                raise ValidationError("certificate_id必须是正整数")
            certificate = self.repository.get_certificate(certificate_id)
            if certificate["instrument_id"] != instrument["id"]:
                raise ValidationError("证书与仪器不匹配")
        reading = self.repository.add_reading(
            instrument["id"], item_id, raw_value, measured_at, certificate_id, actor)
        if item_id is not None:
            self._recompute_item_from_readings(item_id, None, actor, "reading_added")
        self.repository.append_audit("reading", ENTITY_READING, reading["id"], actor, {
            "instrument_id": instrument["id"], "item_id": item_id,
            "raw_value": raw_value, "certificate_id": certificate_id,
        })
        return reading

    def list_readings(self, instrument_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_instrument(instrument_id)
        return self.repository.list_readings(instrument_id=instrument_id)

    def _recompute_item_from_readings(self, item_id: int, certificate_id: Optional[int],
                                      actor: str, reason: str) -> Dict[str, Any]:
        """按当前读数与证书系数重算事件剂量，生成新修订。"""
        item = self.repository.get_item(item_id)
        readings = self.repository.list_readings(item_id=item_id)
        total = 0.0
        for reading in readings:
            coefficient = 1.0
            if reading["certificate_id"] is not None:
                certificate = self.repository.get_certificate(reading["certificate_id"])
                coefficient = certificate["coefficient"]
            total += reading["raw_value"] * coefficient
        if abs(total - item["quantity"]) < 1e-9:
            return item
        revision_no = self.repository.latest_revision_no(item_id) + 1
        self.repository.create_event_revision(
            item_id, revision_no, total, item["severity"], item["threshold"],
            certificate_id, None, reason, actor)
        return self.repository.update_item_quantity(item_id, total)

    # ---------- 重算批次 ----------
    def create_recalc_batch(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, RECALC_ROLES)
        actor = require_text(actor, "actor", 100)
        request_key = require_text(payload.get("request_key"), "request_key", 100)
        existing = self.repository.get_batch_by_key(request_key)
        if existing is not None:
            # 同一请求重试沿用首次结果；未完成批次从最后完成的仪器恢复
            if existing["status"] != "completed":
                return self._run_batch(existing, actor)
            return existing
        certificate_ids = payload.get("certificate_ids")
        if certificate_ids is None:
            single = payload.get("certificate_id")
            if single is None:
                raise ValidationError("certificate_id或certificate_ids不能为空")
            certificate_ids = [single]
        if not isinstance(certificate_ids, list) or not certificate_ids:
            raise ValidationError("certificate_ids必须是非空数组")
        certificates = []
        for cid in certificate_ids:
            if not isinstance(cid, int) or cid < 1:
                raise ValidationError("certificate_id必须是正整数")
            certificates.append(self.repository.get_certificate(cid))
        batch = self.repository.create_batch(request_key, len(certificates), actor)
        for certificate in certificates:
            self.repository.add_batch_item(
                batch["id"], certificate["instrument_id"], certificate["id"])
        self.repository.append_audit("recalc_request", ENTITY_BATCH, batch["id"], actor, {
            "request_key": request_key, "certificate_ids": [c["id"] for c in certificates],
        })
        return self._run_batch(batch, actor)

    def retry_recalc_batch(self, batch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECALC_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        if batch["status"] == "completed":
            return batch
        return self._run_batch(batch, actor)

    def list_recalc_batches(self, role: str) -> list:
        self._view(role)
        return self.repository.list_batches()

    def get_recalc_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_id)
        batch["items"] = self.repository.list_batch_items(batch_id)
        return batch

    def _run_batch(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        items = self.repository.list_batch_items(batch["id"])
        for batch_item in items:
            if batch_item["status"] == "completed":
                # 检查点：失败恢复时跳过已完成的仪器
                continue
            claimed = False
            try:
                claimed = self.repository.claim_instrument(
                    batch_item["instrument_id"], batch["id"])
                if not claimed:
                    # 两批并发：该仪器已被其他批次认领，本批不覆盖
                    self.repository.update_batch_item_status(
                        batch["id"], batch_item["instrument_id"], "skipped",
                        message="仪器正被其他重算批次占用，已跳过以避免覆盖")
                    continue
                self.repository.update_batch_item_status(
                    batch["id"], batch_item["instrument_id"], "running")
                result = self._recalc_instrument(
                    batch_item["instrument_id"], batch_item["certificate_id"],
                    batch["id"], actor)
                self.repository.update_batch_item_status(
                    batch["id"], batch_item["instrument_id"], "completed",
                    processed_events=result["events"],
                    message=f"重算{result['readings']}条读数，{result['events']}个事件生成新修订")
            except Exception as exc:
                self.repository.update_batch_item_status(
                    batch["id"], batch_item["instrument_id"], "failed",
                    message=str(exc))
                self.repository.update_batch_status(
                    batch["id"], "failed",
                    completed=sum(1 for i in self.repository.list_batch_items(batch["id"])
                                  if i["status"] == "completed"),
                    failed=sum(1 for i in self.repository.list_batch_items(batch["id"])
                               if i["status"] == "failed"),
                    skipped=sum(1 for i in self.repository.list_batch_items(batch["id"])
                                if i["status"] == "skipped"),
                    result={"error": str(exc)}, error=str(exc))
                raise
            finally:
                if claimed:
                    self.repository.release_instrument(
                        batch_item["instrument_id"], batch["id"])
        final_items = self.repository.list_batch_items(batch["id"])
        completed = sum(1 for i in final_items if i["status"] == "completed")
        failed = sum(1 for i in final_items if i["status"] == "failed")
        skipped = sum(1 for i in final_items if i["status"] == "skipped")
        status = "completed" if failed == 0 else "failed"
        result = {
            "completed": completed, "failed": failed, "skipped": skipped,
            "items": [
                {"instrument_id": i["instrument_id"], "instrument_code": i["instrument_code"],
                 "status": i["status"], "processed_events": i["processed_events"],
                 "message": i["message"]}
                for i in final_items
            ],
        }
        self.repository.update_batch_status(
            batch["id"], status, completed, failed, skipped, result)
        return self.repository.get_batch(batch["id"])

    def _recalc_instrument(self, instrument_id: int, certificate_id: int,
                           batch_id: int, actor: str) -> Dict[str, Any]:
        certificate = self.repository.get_certificate(certificate_id)
        readings = self.repository.readings_in_interval(
            instrument_id, certificate["effective_from"], certificate["effective_to"])
        affected_items = set()
        for reading in readings:
            if reading["certificate_id"] != certificate_id:
                self.repository.update_reading_certificate(reading["id"], certificate_id)
            if reading["item_id"] is not None:
                affected_items.add(reading["item_id"])
        changed = 0
        for item_id in affected_items:
            item = self.repository.get_item(item_id)
            new_quantity = self._compute_item_quantity(item_id)
            if abs(new_quantity - item["quantity"]) < 1e-9:
                continue
            self._apply_recalc_revision(item, new_quantity, certificate, batch_id, actor)
            changed += 1
        return {"readings": len(readings), "events": changed}

    def _compute_item_quantity(self, item_id: int) -> float:
        total = 0.0
        for reading in self.repository.list_readings(item_id=item_id):
            coefficient = 1.0
            if reading["certificate_id"] is not None:
                certificate = self.repository.get_certificate(reading["certificate_id"])
                coefficient = certificate["coefficient"]
            total += reading["raw_value"] * coefficient
        return total

    def _apply_recalc_revision(self, item: Dict[str, Any], new_quantity: float,
                               certificate: Dict[str, Any], batch_id: int,
                               actor: str) -> None:
        old_quantity = item["quantity"]
        severity = item["severity"]
        threshold = item["threshold"]
        revision_no = self.repository.latest_revision_no(item["id"]) + 1
        self.repository.create_event_revision(
            item["id"], revision_no, new_quantity, severity, threshold,
            certificate["id"], batch_id, "certificate_reissue", actor)
        self.repository.update_item_quantity(item["id"], new_quantity)
        old_escalation = escalation_required(severity, old_quantity, threshold)
        new_escalation = escalation_required(severity, new_quantity, threshold)
        old_deadline = response_deadline_hours(severity, old_quantity, threshold)
        new_deadline = response_deadline_hours(severity, new_quantity, threshold)
        concluded = item["status"] in CONCLUDED_STATES
        todos = []
        if concluded:
            invalidated = self.repository.invalidate_concluded_records(item["id"], batch_id)
            todos.append(("conclusion_invalid",
                          f"证书{certificate['certificate_no']}系数重算后，"
                          f"原{', '.join(sorted(CONCLUDED_STATES))}结论失效，需重新评估"))
            self.repository.append_audit("conclusion_invalid", ENTITY, item["id"], actor, {
                "batch_id": batch_id, "revision_no": revision_no,
                "invalidated_records": invalidated,
                "old_quantity": old_quantity, "new_quantity": new_quantity,
            })
        if new_escalation and not old_escalation:
            todos.append(("investigation",
                          f"重算后剂量{new_quantity:.3g}≥阈值{threshold:g}，需启动调查"))
        if severity in ("high", "critical") or new_escalation:
            if concluded or new_escalation:
                todos.append(("follow_up",
                              f"重算后剂量{new_quantity:.3g}，需安排医学随访重新评估"))
        if old_deadline != new_deadline:
            todos.append(("deadline",
                          f"报告期限由{old_deadline}小时变更为{new_deadline}小时"))
        for kind, reason_text in todos:
            self.repository.create_todo(item["id"], batch_id, kind, reason_text, actor)
        self.repository.append_audit("recalc", ENTITY, item["id"], actor, {
            "batch_id": batch_id, "certificate_id": certificate["id"],
            "certificate_no": certificate["certificate_no"], "revision_no": revision_no,
            "old_quantity": old_quantity, "new_quantity": new_quantity,
            "old_deadline_hours": old_deadline, "new_deadline_hours": new_deadline,
            "escalation_required": new_escalation,
            "todos": [todo_kind_label(kind) for kind, _ in todos],
        })

    # ---------- 待办 ----------
    def list_todos(self, role: str, status: Optional[str] = None,
                    item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_todos(status=status, item_id=item_id)

    def close_todo(self, todo_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TODO_CLOSE_ROLES)
        actor = require_text(actor, "actor", 100)
        todo = self.repository.get_todo(todo_id)
        if todo["status"] == "done":
            return todo
        updated = self.repository.close_todo(todo_id)
        self.repository.append_audit("todo_done", ENTITY_TODO, todo["id"], actor, {
            "item_id": todo["item_id"], "kind": todo["kind"],
        })
        return updated

    # ---------- 历史基线升级 ----------
    def upgrade_baselines(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BASELINE_UPGRADE_ROLES)
        actor = require_text(actor, "actor", 100)
        upgraded = []
        for item in self.repository.list_items():
            if self.repository.has_baseline_revision(item["id"]):
                continue
            readings = self.repository.list_readings(item_id=item["id"])
            has_unlinked = any(r["certificate_id"] is None for r in readings)
            if readings and not has_unlinked:
                continue
            revision_no = self.repository.latest_revision_no(item["id"]) + 1
            self.repository.create_event_revision(
                item["id"], revision_no, item["quantity"], item["severity"],
                item["threshold"], None, None, "historical_baseline", actor)
            self.repository.mark_item_baseline(item["id"])
            self.repository.append_audit("baseline_upgrade", ENTITY, item["id"], actor, {
                "revision_no": revision_no, "baseline_quantity": item["quantity"],
            })
            upgraded.append(item["id"])
        return {"upgraded": upgraded}

    def revisions(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_event_revisions(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
