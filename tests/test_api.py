"""HTTP API 冒烟测试：路由、身份头、错误映射与回执语义。"""

from __future__ import annotations

import http.client
import json
import threading
import unittest

from src.forecast_backend.api import create_server
from tests.forecast_case import ForecastCase


class ApiTest(ForecastCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = create_server(self.system, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def call(
        self,
        method: str,
        path: str,
        *,
        actor: str = "stat-1",
        role: str = "statistician",
        body: dict | None = None,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor-Id": actor, "X-Actor-Role": role}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def _setup_rule(self) -> None:
        status, _ = self.call(
            "POST",
            "/rules",
            body={
                "rule_id": "rule-1",
                "scope_indicator": None,
                "definition": {"weighting": "equal", "min_institutions": 3},
            },
        )
        self.assertEqual(status, 200)
        self.call("POST", "/rules/rule-1/versions/1/submit")
        status, _ = self.call(
            "POST", "/rules/rule-1/versions/1/approve", actor="appr-1", role="approver"
        )
        self.assertEqual(status, 200)

    def _submit(self, institution: str, value: float, receipt: str) -> tuple[int, dict]:
        return self.call(
            "POST",
            "/submissions",
            actor=institution,
            role="institution",
            body={
                "receipt_id": receipt,
                "institution_id": institution,
                "indicator": "IPCA",
                "target_year": 2026,
                "round_id": "R1",
                "value": value,
                "lower": value - 0.2,
                "upper": value + 0.2,
                "confidence": 0.8,
                "rationale": "基线判断",
            },
        )

    def test_full_flow_over_http(self) -> None:
        self._setup_rule()
        for index, value in enumerate((4.8, 4.9, 5.0, 5.1)):
            status, body = self._submit(self.INSTITUTIONS[index], value, f"rcpt-{index}")
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "accepted")

        # 同一回执重放返回原结果
        status, replay = self._submit(self.INSTITUTIONS[0], 4.8, "rcpt-0")
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        # 同一回执不同内容 → 409 隔离
        status, conflict = self._submit(self.INSTITUTIONS[0], 9.9, "rcpt-0")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "receipt_conflict")
        status, quarantine = self.call("GET", "/quarantine", actor="audit-1", role="auditor")
        self.assertEqual(len(quarantine), 1)

        # 共识聚合（机构角色也可查询，但只有聚合值）
        status, consensus = self.call(
            "GET",
            "/consensus?round_id=R1&indicator=IPCA&target_year=2026",
            actor="bank-a",
            role="institution",
        )
        self.assertEqual(status, 200)
        self.assertEqual(consensus["median"], 4.95)
        self.assertEqual(consensus["contributors"], 4)

        # 计算并发布
        status, publication = self.call(
            "POST",
            "/publications/compute",
            body={"round_id": "R1", "indicator": "IPCA", "target_year": 2026},
        )
        self.assertEqual(status, 200)
        status, published = self.call(
            "POST",
            f"/publications/{publication['publication_id']}/publish",
            actor="appr-1",
            role="approver",
        )
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "published")

        # 解释：修订顺序与有效样本
        status, explanation = self.call(
            "GET",
            f"/publications/{publication['publication_id']}/explain",
            actor="audit-1",
            role="auditor",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(explanation["contributors"]), 4)
        self.assertEqual(explanation["rule"]["rule_id"], "rule-1")

    def test_error_mapping(self) -> None:
        # 未认证
        status, body = self.call("GET", "/consensus?round_id=R1&indicator=IPCA&target_year=2026", actor="", role="")
        self.assertEqual(status, 401)
        # 越权：机构尝试定义规则
        status, body = self.call(
            "POST",
            "/rules",
            actor="bank-a",
            role="institution",
            body={"rule_id": "x", "definition": {}},
        )
        self.assertEqual(status, 403)
        # 不存在
        status, body = self.call("GET", "/publications/nope", actor="audit-1", role="auditor")
        self.assertEqual(status, 404)
        # 未知路由
        status, _ = self.call("GET", "/nowhere")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
