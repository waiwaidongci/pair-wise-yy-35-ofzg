import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.recalc import RecalcProcessor
from src.repository import Repository
from src.rules import (REASON_BASELINE, STATES, TRANSITION_ROLES, rule_effects)
from src.service import Service

T0 = "2026-01-10T08:00:00+00:00"
V1 = "2026-01-01T00:00:00+00:00"


class RevisionChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _instrument(self, code="INST-1"):
        return self.service.create_instrument(
            {"code": code, "name": "电子剂量计"}, "officer", "radiation_officer")

    def _cert(self, instrument_id, certificate_no, coefficient, valid_from=V1,
              valid_to=None, role="radiation_officer"):
        return self.service.issue_certificate(instrument_id, {
            "certificate_no": certificate_no, "valid_from": valid_from,
            "valid_to": valid_to, "coefficient": coefficient,
            "note": "厂检"}, "officer", role)

    def _reading_item(self, instrument_id, raw, threshold, cert_no="CERT-1",
                      measured_at=T0, external_ref=None, severity="low",
                      title=None):
        payload = {"title": title or f"事件{external_ref or raw}",
                   "description": "监测读数", "severity": severity,
                   "threshold": threshold, "external_ref": external_ref,
                   "reading": {"instrument_id": instrument_id, "raw_value": raw,
                               "measured_at": measured_at,
                               "certificate_no": cert_no,
                               "external_ref": f"R-{external_ref or raw}"}}
        return self.service.create_item(payload, "dosimetrist", "dosimetrist")

    # -------------------------------------------- 补发 -> 重算 -> 旧结论失效
    def test_reissue_recalculates_window_and_supersedes_conclusions(self):
        inst = self._instrument()
        self._cert(inst["id"], "CERT-1", 1.0)
        # 读数12 × 系数1.0 = 12，阈值10：需调查；随访阈值为2倍（20），不随访
        item = self._reading_item(inst["id"], 12, 10, external_ref="E-1")
        self.assertAlmostEqual(item["quantity"], 12.0)
        self.assertTrue(item["escalation_required"])
        self.assertFalse(item["medical_followup_required"])
        deadline_before = item["deadline_hours"]
        self.assertAlmostEqual(deadline_before, rule_effects("low", 12, 10)["deadline_hours"])

        # 走到已关闭，过程中产生调查/随访/报告期限结论（未达随访则不会有follow_up，这里只走到closed）
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "u",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")

        # 补发证书：同生效起点、新证书号、系数1.5；读数变为18
        result = self._cert(inst["id"], "CERT-1-R", 1.5)
        self.assertTrue(result["reissued"])
        batch = result["recalc_batch"]
        self.assertEqual(batch["status"], "completed")
        stats = batch["summary"]["instruments"][str(inst["id"])]
        self.assertEqual(stats["revised"], 1)

        updated = self.service.get_item(item["id"], "viewer")
        self.assertAlmostEqual(updated["quantity"], 18.0)
        # 已关闭事件被标记需要重新评估
        self.assertTrue(updated["reassess_required"])

        # 修订链：首版 + 重算版，保留全部系数/证书引用
        revisions = self.service.list_revisions(item["id"], "viewer")
        self.assertEqual([r["seq"] for r in revisions], [1, 2])
        self.assertEqual(revisions[1]["reason"], "recalculation")
        self.assertEqual(revisions[1]["new_certificate_id"],
                         result["certificate"]["id"])
        self.assertEqual(revisions[1]["old_certificate_id"], 1)
        self.assertEqual(revisions[0]["new_quantity"], revisions[1]["old_quantity"])

        # 调查要求修订前后都成立 -> 调查结论保留；报告期限变化 -> 旧期限结论失效
        active = self.service.list_findings("viewer", item["id"], "active")
        superseded = self.service.list_findings("viewer", item["id"], "superseded")
        self.assertEqual({f["kind"] for f in active}, {"investigation"})
        superseded_kinds = {f["kind"] for f in superseded}
        self.assertEqual(superseded_kinds, {"report_deadline"})
        todos = self.service.list_todos("viewer", item["id"])
        todo_kinds = {t["kind"] for t in todos}
        # 没有判定翻转，只生成报告期限待办；12->18未跨过2倍随访线20
        self.assertEqual(todo_kinds, {"report_deadline_changed"})

        # 审计链完整，原始审计记录仍可查
        self.assertTrue(self.repo.verify_audit_chain())
        audit = self.service.audit("viewer", item["id"])
        self.assertTrue(any(e["action"] == "transition" for e in audit))
        rev_audit = self.repo.list_audit(entity_type="重算批次")
        self.assertTrue(any(e["detail"].get("revised", 0) == 1 for e in rev_audit))

    def test_medical_followup_flip_creates_followup_todo(self):
        inst = self._instrument("INST-2")
        self._cert(inst["id"], "C-2", 1.0)
        # 8/10 需调查，且 8/10 < 2 不随访；closed
        item = self._reading_item(inst["id"], 8, 5, cert_no="C-2",
                                  external_ref="E-2", severity="elevated")
        current = item
        self.assertTrue(item["escalation_required"])
        self.assertFalse(item["medical_followup_required"])
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "u",
                TRANSITION_ROLES[target][0])
        result = self._cert(inst["id"], "C-2-R", 3.0)  # 8*3=24 >= 20
        self.assertEqual(result["recalc_batch"]["status"], "completed")
        todos = {t["kind"] for t in self.service.list_todos("viewer", item["id"])}
        self.assertIn("reassess_follow_up", todos)
        # 调查结论前后都成立，保持active；无随访结论被失效
        kinds = {f["kind"] for f in self.service.list_findings("viewer", item["id"], "active")}
        self.assertIn("investigation", kinds)
        superseded = {f["kind"] for f in self.service.list_findings("viewer", item["id"], "superseded")}
        self.assertNotIn("medical_follow_up", superseded)
        self.assertIn("report_deadline", superseded)

    def test_unchanged_recalc_appends_no_revision(self):
        inst = self._instrument("INST-3")
        self._cert(inst["id"], "C-3", 1.0)
        item = self._reading_item(inst["id"], 5, 100, cert_no="C-3", external_ref="E-3")
        result = self._cert(inst["id"], "C-3-R", 1.0)
        stats = result["recalc_batch"]["summary"]["instruments"][str(inst["id"])]
        self.assertEqual(stats["revised"], 0)
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(len(self.service.list_revisions(item["id"], "viewer")), 1)

    # ----------------------------------------------------------- 证书版本规则
    def test_certificate_overlap_rejected_and_baseline_window(self):
        inst = self._instrument("INST-4")
        self._cert(inst["id"], "C-4", 1.0, V1, "2026-06-01T00:00:00+00:00")
        with self.assertRaises(ConflictError):
            self._cert(inst["id"], "C-4-X", 1.1,
                       "2026-03-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00")
        # 补发必须带新证书号
        with self.assertRaises(ValidationError):
            self.service.issue_certificate(inst["id"], {
                "valid_from": V1, "coefficient": 1.2}, "officer", "radiation_officer")
        with self.assertRaises(ConflictError):
            self._cert(inst["id"], "C-4", 2.0, "2026-07-01T00:00:00+00:00")
        # 补发：生效区间必须与旧证一致
        self._cert(inst["id"], "C-4-R", 1.5, V1, "2026-06-01T00:00:00+00:00")
        with self.assertRaises(ValidationError):
            self._cert(inst["id"], "C-4-R2", 1.5, V1, "2026-08-01T00:00:00+00:00")
        certs = self.service.list_certificates("viewer", inst["id"])
        live = [c for c in certs if c["superseded_at"] is None and not c["is_baseline"]]
        dead = [c for c in certs if c["superseded_at"] is not None]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["version"], 2)
        self.assertEqual([c["version"] for c in dead], [1])

    def test_only_radiation_officer_can_issue_certificate(self):
        inst = self._instrument("INST-5")
        with self.assertRaises(PermissionDenied):
            self._cert(inst["id"], "C-5", 1.0, role="dosimetrist")

    # ------------------------------------------------------------ 历史基线
    def test_legacy_reading_without_certificate_becomes_baseline(self):
        inst = self._instrument("INST-6")
        item = self.service.create_item({
            "title": "旧数据", "description": "无证书号", "severity": "low",
            "threshold": 10,
            "reading": {"instrument_id": inst["id"], "raw_value": 7,
                        "measured_at": "2025-05-01T00:00:00Z"}},
            "old", "dosimetrist")
        self.assertAlmostEqual(item["quantity"], 7.0)
        self.assertTrue(item["legacy_baseline"])
        baseline = [c for c in self.service.list_certificates("viewer", inst["id"])
                    if c["is_baseline"]]
        self.assertEqual(len(baseline), 1)
        self.assertEqual(baseline[0]["coefficient"], 1.0)
        rev = self.service.list_revisions(item["id"], "viewer")[0]
        self.assertEqual(rev["reason"], REASON_BASELINE)

        # 首张正式证书生效起点晚于旧测量时刻：历史基线区间自动收紧，旧事件不重算
        result = self._cert(inst["id"], "C-6", 2.0, "2026-01-01T00:00:00+00:00")
        self.assertFalse(result["reissued"])
        self.assertIsNone(result["recalc_batch"])
        self.assertAlmostEqual(self.service.get_item(item["id"], "viewer")["quantity"], 7.0)
        certs = {c["id"]: c for c in self.service.list_certificates("viewer", inst["id"])}
        refreshed = certs[baseline[0]["id"]]
        self.assertEqual(refreshed["valid_to"], "2026-01-01T00:00:00+00:00")

        # 正式证书覆盖的时刻不能再走基线
        with self.assertRaises(ValidationError):
            self.service.create_item({
                "title": "新数据", "description": "缺证书号", "severity": "low",
                "threshold": 10,
                "reading": {"instrument_id": inst["id"], "raw_value": 7,
                            "measured_at": "2026-02-01T00:00:00Z"}},
                "old", "dosimetrist")

    def test_attach_reading_to_legacy_item_promotes_baseline(self):
        inst = self._instrument("INST-7")
        item = self.service.create_item({"title": "存量", "description": "无读数",
                                         "severity": "low", "quantity": 9,
                                         "threshold": 100,
                                         "external_ref": "LEG-1"},
                                        "dosimetrist", "dosimetrist")
        updated = self.service.attach_reading(item["id"], {
            "expected_version": 1,
            "reading": {"instrument_id": inst["id"], "raw_value": 9,
                        "measured_at": "2025-03-01T00:00:00Z"}},
            "dosimetrist", "dosimetrist")
        self.assertTrue(updated["legacy_baseline"])
        self.assertAlmostEqual(updated["quantity"], 9.0)
        self.assertEqual(len(self.service.list_revisions(item["id"], "viewer")), 1)
        with self.assertRaises(ConflictError):
            self.service.attach_reading(item["id"], {
                "expected_version": updated["version"],
                "reading": {"instrument_id": inst["id"], "raw_value": 9,
                            "measured_at": "2025-03-01T00:00:00Z"}},
                "dosimetrist", "dosimetrist")

    # ----------------------------------------------- 失败恢复 + 幂等重试
    def test_failure_resumes_from_last_instrument_and_replays_first_result(self):
        i1 = self._instrument("INST-A")
        i2 = self._instrument("INST-B")
        self._cert(i1["id"], "CA-1", 1.0)
        self._cert(i2["id"], "CB-1", 1.0)
        self._reading_item(i1["id"], 3, 100, cert_no="CA-1", external_ref="A1")
        self._reading_item(i2["id"], 4, 100, cert_no="CB-1", external_ref="B1")

        class FailingProcessor(RecalcProcessor):
            def __init__(self, repo, fail_instrument):
                super().__init__(repo)
                self.fail_instrument = fail_instrument
                self.attempts = []

            def before_instrument(self, batch_id, instrument_id):
                self.attempts.append(instrument_id)
                if instrument_id == self.fail_instrument:
                    raise RuntimeError("注入故障：仪器重算失败")

        processor = FailingProcessor(self.repo, i2["id"])
        self.service.recalc = processor
        batch = processor.submit_or_get("REQ-1", [i1["id"], i2["id"]], None, None,
                                        "手动重算", "officer")
        self.assertEqual(batch["status"], "failed")
        # 仪器A（排在前面）已经完成
        rows = {r["instrument_id"]: r for r in self.repo.list_batch_instruments(batch["id"])}
        self.assertEqual(rows[i1["id"]]["status"], "completed")
        self.assertEqual(rows[i2["id"]]["status"], "failed")

        # 故障解除后同请求重试：从最后未完成的仪器恢复，A不重算
        processor.fail_instrument = None
        retried = processor.submit_or_get("REQ-1", [i1["id"], i2["id"]], None, None,
                                          "手动重算", "officer")
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["id"], batch["id"])
        self.assertEqual(processor.attempts.count(i2["id"]), 2)
        self.assertNotIn(i1["id"], processor.attempts[2:])
        # 同一请求再次提交：沿用首次结果（冻结），不再处理任何仪器
        again = processor.submit_or_get("REQ-1", [i1["id"], i2["id"]], None, None,
                                        "手动重算", "officer")
        self.assertEqual(again["summary"], retried["summary"])
        self.assertEqual(len(processor.attempts), 3)

    # ---------------------------------------------------- 两批并发互不覆盖
    def test_concurrent_batches_do_not_overwrite(self):
        i1 = self._instrument("INST-X")
        i2 = self._instrument("INST-Y")
        self._cert(i1["id"], "CX-1", 1.0)
        self._cert(i2["id"], "CY-1", 1.0)
        item_x = self._reading_item(i1["id"], 12, 10, cert_no="CX-1", external_ref="X1")
        item_y = self._reading_item(i2["id"], 12, 10, cert_no="CY-1", external_ref="Y1")

        # 两个补发证书都覆盖两台仪器：批次1用X-2系数（仪器X补发），批次2全量两台；
        # 仪器X额外补发X-3系数，两批提交后再发，形成真正的并发重算窗口
        import threading
        outcomes = []
        errors = []

        class BlockingProcessor(RecalcProcessor):
            gate = threading.Event()
            entered = threading.Event()

            def before_instrument(self, batch_id, instrument_id):
                if instrument_id == i1["id"]:
                    BlockingProcessor.entered.set()
                    BlockingProcessor.gate.wait(5)

        # 先补发两台仪器的新证书（产生cert批次但读数此时还在旧系数下）
        x2 = self._cert(i1["id"], "CX-2", 1.5)
        y2 = self._cert(i2["id"], "CY-2", 1.5)
        # 撤掉自动批次的修订结果（直接手动发起两个并发全量批次）
        def run(request_id):
            try:
                outcomes.append(self.service.recalc.submit_or_get(
                    request_id, [i1["id"], i2["id"]], None, None, "并发重算", "officer"))
            except Exception as exc:  # 记录线程内异常
                errors.append(exc)

        self.service.recalc = BlockingProcessor(self.repo)
        t1 = threading.Thread(target=run, args=("CONC-1",))
        t1.start()
        BlockingProcessor.entered.wait(5)
        t2 = threading.Thread(target=run, args=("CONC-2",))
        t2.start()
        t2.join(5)
        BlockingProcessor.gate.set()
        t1.join(5)

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        by_status = {b["status"]: b for b in outcomes}
        # 一批独占完成两台；另一批在仪器X上跳过（避免覆盖），先完成仪器Y
        completed = by_status["completed"]
        partial = by_status["partial"]
        skipped_rows = [r for r in self.repo.list_batch_instruments(partial["id"])
                        if r["status"] == "skipped"]
        self.assertEqual(len(skipped_rows), 1)
        self.assertEqual(skipped_rows[0]["instrument_id"], i1["id"])

        # partial批次用同一请求重试：仪器X已空闲，补齐完成；最终两个事件剂量都是18
        again = self.service.recalc.submit_or_get(
            "CONC-2", [i1["id"], i2["id"]], None, None, "并发重算", "officer")
        self.assertEqual(again["status"], "completed")
        self.assertAlmostEqual(self.service.get_item(item_x["id"], "viewer")["quantity"], 18.0)
        self.assertAlmostEqual(self.service.get_item(item_y["id"], "viewer")["quantity"], 18.0)

        # 每个读数的修订次数一致：首版 + 自动补发批次 + 两个并发批次中有效覆盖的一次
        # （自动批次已把读数迁到1.5证书，后续批次判定未变化不追加），不允许重复修订
        rows = self.repo.conn.execute(
            "SELECT item_id, COUNT(*) AS n FROM dose_revisions GROUP BY item_id").fetchall()
        counts = sorted(r["n"] for r in rows)
        self.assertEqual(counts, [2, 2])
        reasons = [r["reason"] for r in self.repo.conn.execute(
            "SELECT reason FROM dose_revisions ORDER BY id").fetchall()]
        self.assertEqual(reasons.count("recalculation"), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_todos_can_be_closed(self):
        inst = self._instrument("INST-8")
        self._cert(inst["id"], "C-8", 1.0)
        item = self._reading_item(inst["id"], 12, 10, cert_no="C-8", external_ref="E-8")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "u",
                TRANSITION_ROLES[target][0])
        self._cert(inst["id"], "C-8-R", 1.5)
        todo = self.service.list_todos("radiation_officer", item["id"])[0]
        closed = self.service.close_todo(todo["id"], "officer", "radiation_officer")
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(ConflictError):
            self.service.close_todo(todo["id"], "officer", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.close_todo(99999, "v", "viewer")


if __name__ == "__main__":
    unittest.main()
