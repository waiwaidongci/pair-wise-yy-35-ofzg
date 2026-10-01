import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import TRANSITION_ROLES
from src.service import Service


class RecalcChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.instrument = self.service.create_instrument(
            {"code": "INST-1", "name": "剂量仪A"}, "creator", "radiation_officer")
        self.cert1 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-001", "coefficient": 1.0,
             "effective_from": "2025-01-01"},
            "creator", "radiation_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _close_item(self, item):
        self.service.add_record(item["id"], {
            "kind": "conclusion", "detail": "调查完成",
            "status": "closed", "external_ref": "R-1"}, "recorder", "radiation_officer")
        current = item
        for target in ["reviewing", "investigation", "follow_up", "closed"]:
            role = "radiation_officer" if target in ("reviewing", "investigation") else "health_physicist"
            current = self.service.transition(
                current["id"], target, current["version"], "recorder", role)
        return current

    def test_certificate_versions_and_supersede(self):
        self.assertEqual(self.cert1["version"], 1)
        self.assertEqual(self.cert1["status"], "active")
        cert2 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-002", "coefficient": 1.5,
             "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        self.assertEqual(cert2["version"], 2)
        self.assertEqual(cert2["status"], "active")
        certs = self.service.list_certificates(self.instrument["id"], "viewer")
        self.assertEqual([c["version"] for c in certs], [2, 1])
        self.assertEqual(certs[1]["status"], "superseded")
        self.assertEqual(certs[1]["effective_to"], "2025-06-01")

    def test_recalc_recomputes_and_creates_revision_chain(self):
        item = self.service.create_item({
            "title": "事件", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        self.assertEqual(item["quantity"], 6.0)
        cert2 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-002", "coefficient": 1.5,
             "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        batch = self.service.create_recalc_batch(
            {"request_key": "req-1", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        self.assertEqual(batch["status"], "completed")
        updated = self.service.get_item(item["id"], "viewer")
        self.assertEqual(updated["quantity"], 9.0)
        revisions = self.service.revisions(item["id"], "viewer")
        self.assertEqual([r["revision_no"] for r in revisions], [1, 2])
        self.assertEqual(revisions[0]["reason"], "initial")
        self.assertEqual(revisions[0]["certificate_no"], "CERT-001")
        self.assertEqual(revisions[1]["reason"], "certificate_reissue")
        self.assertEqual(revisions[1]["certificate_no"], "CERT-002")
        self.assertEqual(revisions[1]["quantity"], 9.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_recalc_generates_todos_and_invalidates_closed_conclusions(self):
        item = self.service.create_item({
            "title": "事件", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        closed = self._close_item(item)
        self.assertEqual(closed["status"], "closed")
        cert2 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-002", "coefficient": 1.5,
             "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        self.service.create_recalc_batch(
            {"request_key": "req-2", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        todos = self.service.list_todos("viewer")
        kinds = {t["kind"] for t in todos}
        self.assertIn("conclusion_invalid", kinds)
        self.assertIn("follow_up", kinds)
        self.assertIn("deadline", kinds)
        records = self.service.list_records(item["id"], "viewer")
        self.assertTrue(all(r["invalidated"] == 1 for r in records))
        # 审计记录仍然可查
        events = self.service.audit("viewer", item["id"])
        self.assertTrue(any(e["action"] == "recalc" for e in events))
        self.assertTrue(any(e["action"] == "conclusion_invalid" for e in events))
        self.assertTrue(self.repo.verify_audit_chain())

    def test_same_request_key_returns_first_result(self):
        item = self.service.create_item({
            "title": "事件", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        cert2 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-002", "coefficient": 1.5,
             "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        first = self.service.create_recalc_batch(
            {"request_key": "req-idem", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        second = self.service.create_recalc_batch(
            {"request_key": "req-idem", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "completed")
        # 没有重复修订
        self.assertEqual(len(self.service.revisions(item["id"], "viewer")), 2)

    def test_concurrent_batch_does_not_overwrite(self):
        item = self.service.create_item({
            "title": "事件", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 5.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        cert2 = self.service.issue_certificate(
            self.instrument["id"],
            {"certificate_no": "CERT-002", "coefficient": 2.0,
             "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        # 模拟批次A正在占用仪器
        batch_a = self.repo.create_batch("req-A", 1, "creator")
        self.repo.add_batch_item(batch_a["id"], self.instrument["id"], cert2["id"])
        self.assertTrue(self.repo.claim_instrument(self.instrument["id"], batch_a["id"]))
        # 批次B必须跳过，不能覆盖
        batch_b = self.service.create_recalc_batch(
            {"request_key": "req-B", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        item_b = self.repo.list_batch_items(batch_b["id"])[0]
        self.assertEqual(item_b["status"], "skipped")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["quantity"], 5.0)
        self.assertEqual(len(self.service.revisions(item["id"], "viewer")), 1)
        # 释放后重试同一请求，沿用首次结果（仍为skipped）
        self.repo.release_instrument(self.instrument["id"], batch_a["id"])
        batch_b2 = self.service.create_recalc_batch(
            {"request_key": "req-B", "certificate_id": cert2["id"]},
            "creator", "radiation_officer")
        self.assertEqual(batch_b2["id"], batch_b["id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_failure_resumes_from_last_completed_instrument(self):
        inst2 = self.service.create_instrument(
            {"code": "INST-2", "name": "剂量仪B"}, "creator", "radiation_officer")
        c1 = self.service.issue_certificate(
            inst2["id"], {"certificate_no": "C1", "coefficient": 1.0,
                          "effective_from": "2025-01-01"},
            "creator", "radiation_officer")
        it1 = self.service.create_item({
            "title": "E1", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        it2 = self.service.create_item({
            "title": "E2", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": inst2["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": c1["id"]}],
        }, "creator", "dosimetrist")
        c1n = self.service.issue_certificate(
            self.instrument["id"], {"certificate_no": "C1N", "coefficient": 2.0,
                                    "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        c2n = self.service.issue_certificate(
            inst2["id"], {"certificate_no": "C2N", "coefficient": 2.0,
                          "effective_from": "2025-06-01"},
            "creator", "radiation_officer")
        original = self.service._recalc_instrument

        def failing(instrument_id, certificate_id, batch_id, actor):
            if instrument_id == inst2["id"]:
                raise RuntimeError("模拟仪器2处理失败")
            return original(instrument_id, certificate_id, batch_id, actor)

        self.service._recalc_instrument = failing
        with self.assertRaises(RuntimeError):
            self.service.create_recalc_batch(
                {"request_key": "req-fail", "certificate_ids": [c1n["id"], c2n["id"]]},
                "creator", "radiation_officer")
        self.service._recalc_instrument = original
        failed = self.repo.get_batch_by_key("req-fail")
        self.assertEqual(failed["status"], "failed")
        checkpoints = {i["instrument_code"]: i["status"]
                       for i in self.repo.list_batch_items(failed["id"])}
        self.assertEqual(checkpoints["INST-1"], "completed")
        self.assertEqual(checkpoints["INST-2"], "failed")
        # 恢复重试：已完成的仪器跳过，失败的继续
        resumed = self.service.retry_recalc_batch(failed["id"], "creator", "radiation_officer")
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(all(i["status"] == "completed"
                            for i in self.repo.list_batch_items(resumed["id"])))
        # 已完成的仪器没有重复修订
        self.assertEqual(len(self.service.revisions(it1["id"], "viewer")), 2)
        self.assertEqual(len(self.service.revisions(it2["id"], "viewer")), 2)
        self.assertEqual(self.service.get_item(it2["id"], "viewer")["quantity"], 12.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_baseline_upgrade_for_legacy_events(self):
        # 旧事件：无读数、无证书号
        legacy = self.service.create_item({
            "title": "legacy", "description": "old", "severity": "low",
            "quantity": 3, "threshold": 10},
            "creator", "dosimetrist")
        # 新事件：读数带证书
        with_cert = self.service.create_item({
            "title": "new", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01", "certificate_id": self.cert1["id"]}],
        }, "creator", "dosimetrist")
        result = self.service.upgrade_baselines("creator", "radiation_officer")
        self.assertIn(legacy["id"], result["upgraded"])
        self.assertNotIn(with_cert["id"], result["upgraded"])
        legacy_revs = self.service.revisions(legacy["id"], "viewer")
        self.assertEqual(legacy_revs[0]["reason"], "historical_baseline")
        self.assertIsNone(legacy_revs[0]["certificate_id"])
        self.assertEqual(self.service.get_item(legacy["id"], "viewer")["is_baseline"], 1)
        # 重复升级不产生重复修订
        result2 = self.service.upgrade_baselines("creator", "radiation_officer")
        self.assertEqual(result2["upgraded"], [])
        self.assertEqual(len(self.service.revisions(legacy["id"], "viewer")), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reading_without_certificate_uses_baseline_coefficient(self):
        item = self.service.create_item({
            "title": "事件", "description": "d", "severity": "high", "threshold": 6,
            "readings": [{"instrument_id": self.instrument["id"], "raw_value": 6.0,
                          "measured_at": "2025-06-01"}],
        }, "creator", "dosimetrist")
        self.assertEqual(item["quantity"], 6.0)
        self.assertEqual(item["is_baseline"], 1)
        revisions = self.service.revisions(item["id"], "viewer")
        self.assertIsNone(revisions[0]["certificate_id"])

    def test_permission_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_instrument({"code": "X", "name": "X"}, "attacker", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.issue_certificate(
                self.instrument["id"],
                {"certificate_no": "X", "coefficient": 1.0,
                 "effective_from": "2025-01-01"},
                "attacker", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_recalc_batch(
                {"request_key": "x", "certificate_id": self.cert1["id"]},
                "attacker", "viewer")
        with self.assertRaises(ValidationError):
            self.service.issue_certificate(
                self.instrument["id"],
                {"certificate_no": "BAD", "coefficient": -1,
                 "effective_from": "2025-01-01"},
                "creator", "radiation_officer")


if __name__ == "__main__":
    unittest.main()
