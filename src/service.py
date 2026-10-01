from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import normalize_iso
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .recalc import RecalcProcessor
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    REASON_BASELINE, REASON_INITIAL, TITLE, TRANSITION_FINDING,
                    VIEW_ROLES, certificate_applies, completion_blockers,
                    escalation_required, medical_followup_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    rule_effects, validate_transition)

INSTRUMENT_ROLES = {'dosimetrist', 'radiation_officer'}
CERTIFICATE_ROLES = {'radiation_officer'}
RECALC_ROLES = {'dosimetrist', 'radiation_officer'}
TODO_ROLES = {'radiation_officer', 'health_physicist'}
READING_VIEW_ROLES = VIEW_ROLES


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.recalc = RecalcProcessor(repository)

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ------------------------------------------------------------ instruments
    def create_instrument(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, INSTRUMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        name = require_text(payload.get("name"), "name", 200)
        description = (payload.get("description") or "").strip()
        if len(description) > 2000:
            raise ValidationError("description不能超过2000个字符")
        instrument = self.repository.create_instrument(code, name, description, actor)
        self.repository.append_audit("create_instrument", "仪器", instrument["id"], actor, {
            "code": code, "name": name})
        return instrument

    def get_instrument(self, instrument_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_instrument(instrument_id)

    def list_instruments(self, role: str) -> list:
        self._view(role)
        return self.repository.list_instruments()

    # ----------------------------------------------------------- certificates
    def issue_certificate(self, instrument_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        """
        签发/补发校准证书。补发（同生效起点）后自动提交受影响区间的重算批次，
        请求ID固定为 cert-{证书id}，重复提交沿用首次结果。
        """
        ensure_role(role, CERTIFICATE_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_instrument(instrument_id)
        certificate_no = payload.get("certificate_no")
        if certificate_no is not None:
            certificate_no = require_text(certificate_no, "certificate_no", 100)
        valid_from = normalize_iso(require_text(payload.get("valid_from"), "valid_from", 60))
        valid_to = payload.get("valid_to")
        if valid_to is not None:
            valid_to = normalize_iso(require_text(valid_to, "valid_to", 60))
        coefficient = require_number(payload.get("coefficient"), "coefficient", 0.0000001)
        note = (payload.get("note") or "").strip()
        if len(note) > 2000:
            raise ValidationError("note不能超过2000个字符")
        before = self.repository.list_certificates(instrument_id)
        certificate = self.repository.issue_certificate(
            instrument_id, certificate_no, valid_from, valid_to, coefficient, note, actor)
        reissued = certificate["version"] > 1 or any(
            c.get("superseded_by_certificate_id") == certificate["id"] for c in before)
        batch = None
        if reissued:
            batch = self.recalc.submit_or_get(
                f"cert-{certificate['id']}", [instrument_id], valid_from, valid_to,
                f"证书补发重算：{certificate_no or certificate['id']}", actor)
        return {"certificate": certificate, "reissued": reissued,
                "recalc_batch": batch}

    def get_certificate(self, certificate_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_certificate(certificate_id)

    def list_certificates(self, role: str, instrument_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_certificates(instrument_id)

    # ----------------------------------------------------------------- items
    @staticmethod
    def _reading_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        reading = payload.get("reading")
        if reading is None:
            return None
        if not isinstance(reading, dict):
            raise ValidationError("reading必须是对象")
        instrument_id = require_number(reading.get("instrument_id"), "reading.instrument_id", 1)
        if int(instrument_id) != instrument_id:
            raise ValidationError("reading.instrument_id必须是整数")
        raw_value = require_number(reading.get("raw_value"), "reading.raw_value")
        measured_at = normalize_iso(require_text(reading.get("measured_at"), "reading.measured_at", 60))
        ref = reading.get("external_ref")
        if ref is not None:
            ref = require_text(ref, "reading.external_ref", 100)
        cert_no = reading.get("certificate_no")
        if cert_no is not None:
            cert_no = require_text(cert_no, "reading.certificate_no", 100)
        return {"instrument_id": int(instrument_id), "raw_value": raw_value,
                "measured_at": measured_at, "external_ref": ref,
                "certificate_no": cert_no}

    def _resolve_reading_certificate(self, reading: Dict[str, Any]):
        """证书号校验并覆盖测量时刻；无证书号则升级为仪器历史基线。返回(证书, 是否基线)。"""
        instrument_id = reading["instrument_id"]
        if reading["certificate_no"]:
            certificate = self.repository.get_certificate_by_no(
                instrument_id, reading["certificate_no"])
            if certificate.get("is_baseline"):
                raise ValidationError("历史基线证书无证书号，不能显式引用")
            if not certificate_applies(certificate["valid_from"],
                                       certificate["valid_to"], reading["measured_at"]):
                raise ValidationError("证书生效区间不覆盖测量时刻")
            return certificate, False
        # 旧数据没有证书号：升级为历史基线
        certificate = self.repository.get_or_create_baseline_certificate(instrument_id)
        if not certificate_applies(certificate["valid_from"],
                                   certificate["valid_to"], reading["measured_at"]):
            raise ValidationError("测量时刻已有正式证书覆盖，需提供证书号，不能升级为历史基线")
        return certificate, True

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
        reading = self._reading_payload(payload)
        if reading is not None:
            certificate, legacy_baseline = self._resolve_reading_certificate(reading)
            derived = round(reading["raw_value"] * certificate["coefficient"], 12)
            quantity = payload.get("quantity")
            if quantity is None:
                quantity = derived
            else:
                quantity = require_number(quantity, "quantity")
                if abs(quantity - derived) > 1e-6:
                    raise ValidationError(
                        f"quantity必须等于原始读数×证书系数（{derived}），修订链以读数为准")
            item = self.repository.create_item_with_reading(
                title, description, severity, threshold, external_ref,
                reading["instrument_id"], reading["external_ref"], reading["measured_at"],
                reading["raw_value"], certificate, legacy_baseline,
                REASON_BASELINE if legacy_baseline else REASON_INITIAL, actor)
        else:
            quantity = require_number(payload.get("quantity", 0), "quantity")
            item = self.repository.create_item(title, description, severity, quantity,
                                               threshold, external_ref, actor)
            self.repository.append_audit("create", ENTITY, item["id"], actor, {
                "title": title, "severity": severity, "quantity": quantity,
                "priority": priority_score(severity, quantity, threshold)})
        return self.enrich(item)

    def attach_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        """为存量剂量事件补挂原始读数；无证书号自动升级为历史基线。"""
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        reading = self._reading_payload(payload)
        if reading is None:
            raise ValidationError("必须提交reading（原始读数）")
        item = self.repository.get_item(item_id)
        certificate, legacy_baseline = self._resolve_reading_certificate(reading)
        new_quantity = round(reading["raw_value"] * certificate["coefficient"], 12)
        effects_after = rule_effects(item["severity"], new_quantity, item["threshold"])
        updated = self.repository.attach_reading(
            item_id, expected_version, reading["instrument_id"], reading["external_ref"],
            reading["measured_at"], reading["raw_value"], certificate, legacy_baseline,
            REASON_BASELINE if legacy_baseline else "reading_attached", actor, effects_after)
        return self.enrich(updated)

    def list_readings(self, role: str, instrument_id: Optional[int] = None) -> list:
        self._view(role)
        if instrument_id is not None:
            self.repository.get_instrument(int(instrument_id))
        return self.repository.list_readings(int(instrument_id) if instrument_id is not None else None)

    def list_revisions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_revisions(item_id)

    # ------------------------------------------------------------- transitions
    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
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
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        finding = TRANSITION_FINDING.get(target)
        effects = rule_effects(item["severity"], item["quantity"], item["threshold"])
        # 进入调查/随访时，结论以当时剂量的规则判定为准，不无条件成立
        if target == "investigation" and not effects["investigation_required"]:
            finding = None
        if target == "follow_up" and not effects["medical_followup_required"]:
            finding = None
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor, finding)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "finding": finding[0] if finding else None})
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

    # --------------------------------------------------------------- findings
    def list_findings(self, role: str, item_id: Optional[int] = None,
                      status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_findings(item_id, status)

    def list_todos(self, role: str, item_id: Optional[int] = None,
                   status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_todos(item_id, status)

    def close_todo(self, todo_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TODO_ROLES)
        actor = require_text(actor, "actor", 100)
        todo = self.repository.close_todo(todo_id, actor)
        self.repository.append_audit("close_todo", "待办", todo_id, actor, {
            "item_id": todo["item_id"], "kind": todo["kind"]})
        return todo

    # ----------------------------------------------------------------- recalc
    def submit_recalc(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECALC_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = require_text(payload.get("request_id"), "request_id", 100)
        instrument_ids = payload.get("instrument_ids")
        if instrument_ids is None:
            instrument_ids = [i["id"] for i in self.repository.list_instruments()]
        else:
            if not isinstance(instrument_ids, list) or not instrument_ids:
                raise ValidationError("instrument_ids必须是非空数组或省略")
            instrument_ids = [int(require_number(v, "instrument_ids", 1)) for v in instrument_ids]
            if len(set(instrument_ids)) != len(instrument_ids):
                raise ValidationError("instrument_ids不能重复")
        window_from = payload.get("window_from")
        if window_from is not None:
            window_from = normalize_iso(require_text(window_from, "window_from", 60))
        window_to = payload.get("window_to")
        if window_to is not None:
            window_to = normalize_iso(require_text(window_to, "window_to", 60))
        reason = (payload.get("reason") or "证书补发区间重算").strip()[:2000]
        return self.recalc.submit_or_get(
            request_id, instrument_ids, window_from, window_to, reason, actor)

    def get_recalc_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_id)
        batch["instruments"] = self.repository.list_batch_instruments(batch_id)
        return batch

    def list_recalc_batches(self, role: str) -> list:
        self._view(role)
        return self.repository.list_batches()

    # ------------------------------------------------------------------ audit
    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ------------------------------------------------------------- enrichment
    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["legacy_baseline"] = bool(item.get("legacy_baseline", 0))
        result["reassess_required"] = bool(item.get("reassess_required", 0))
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["medical_followup_required"] = medical_followup_required(
            item["severity"], item["quantity"], item["threshold"])
        result["reading"] = None
        if item.get("reading_id") is not None:
            result["reading"] = {
                "id": item["reading_id"], "raw_value": item["raw_value"],
                "measured_at": item["measured_at"],
                "applied_certificate_id": item["applied_certificate_id"],
                "coefficient": item["reading_coefficient"],
                "legacy_baseline": bool(item.get("legacy_baseline", 0))}
        for key in ("reading_id", "raw_value", "measured_at",
                    "applied_certificate_id", "reading_coefficient"):
            result.pop(key, None)
        return result
