import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        handler = make_handler(Service(self.repo), str(Path(__file__).resolve().parent.parent / "static"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.repo.close()
        self.tmp.cleanup()

    def _request(self, method, path, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_certificate_revision_chain_over_http(self):
        officer = {"X-Actor": "officer", "X-Role": "radiation_officer"}
        dosimetrist = {"X-Actor": "dosi", "X-Role": "dosimetrist"}

        status, body = self._request("POST", "/api/instruments",
                                     {"code": "HTTP-1", "name": "剂量计"}, officer)
        self.assertEqual(status, 201)
        instrument_id = body["id"]

        status, body = self._request("POST", f"/api/instruments/{instrument_id}/certificates", {
            "certificate_no": "H-CERT-1", "valid_from": "2026-01-01T00:00:00Z",
            "coefficient": 1.0}, officer)
        self.assertEqual(status, 201)
        self.assertFalse(body["reissued"])

        status, body = self._request("POST", "/api/items", {
            "title": "HTTP事件", "description": "d", "severity": "low", "threshold": 10,
            "reading": {"instrument_id": instrument_id, "raw_value": 12,
                        "measured_at": "2026-01-10T00:00:00Z",
                        "certificate_no": "H-CERT-1"}}, dosimetrist)
        self.assertEqual(status, 201)
        item_id = body["id"]
        self.assertAlmostEqual(body["quantity"], 12.0)
        self.assertIsNotNone(body["reading"])

        # 无证书号旧数据 -> 历史基线
        status, body = self._request("POST", "/api/items", {
            "title": "HTTP旧数据", "description": "d", "severity": "low", "threshold": 10,
            "reading": {"instrument_id": instrument_id, "raw_value": 3,
                        "measured_at": "2025-01-10T00:00:00Z"}}, dosimetrist)
        self.assertEqual(status, 201)
        self.assertTrue(body["legacy_baseline"])

        # 幂等批次：Idempotency-Key 复用首次结果
        batch_payload = {"instrument_ids": [instrument_id]}
        status, first = self._request("POST", "/api/recalc-batches", batch_payload,
                                      {**officer, "Idempotency-Key": "HTTP-REQ-1"})
        self.assertEqual(status, 202)
        status, second = self._request("POST", "/api/recalc-batches", batch_payload,
                                       {**officer, "Idempotency-Key": "HTTP-REQ-1"})
        self.assertEqual(status, 202)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["summary"], first["summary"])

        # 修订链可查
        status, body = self._request("GET", f"/api/items/{item_id}/revisions",
                                     headers={"X-Actor": "v", "X-Role": "viewer"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["revisions"]), 1)

        # 越权：viewer不能签发证书
        status, body = self._request("POST", f"/api/instruments/{instrument_id}/certificates",
                                     {"valid_from": "2026-01-01T00:00:00Z",
                                      "coefficient": 2.0},
                                     {"X-Actor": "v", "X-Role": "viewer"})
        self.assertEqual(status, 403)

        # 健康检查与审计
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/api/audit",
                                     headers={"X-Actor": "v", "X-Role": "health_physicist"})
        self.assertEqual(status, 200)
        actions = {e["action"] for e in body["events"]}
        self.assertIn("issue_certificate", actions)
        self.assertIn("recalc_batch_submit", actions)


if __name__ == "__main__":
    unittest.main()
