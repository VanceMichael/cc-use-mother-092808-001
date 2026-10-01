"""HTTP API 端到端测试（直调 WSGI，不占端口）。"""

import io
import json
import unittest

from src.macro_survey.api import HttpApi
from tests.world import World


def call(app: HttpApi, method: str, path: str, token: str | None = None,
         body: dict | None = None, query: str = "") -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(payload)),
        "wsgi.input": io.BytesIO(payload),
        "HTTP_AUTHORIZATION": f"Bearer {token}" if token else "",
    }
    captured: dict = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])

    raw = b"".join(app(environ, start_response))
    return captured["status"], json.loads(raw.decode())


class HttpFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
        self.app = HttpApi(self.w.svc)

    def tearDown(self) -> None:
        self.w.close()

    def test_auth_required(self) -> None:
        status, body = call(self.app, "GET", "/publications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_full_flow_over_http(self) -> None:
        # 统计员起草规则
        status, draft = call(self.app, "POST", "/rules", self.w.stat["token"], {
            "indicator_id": self.w.ipca["id"],
            "params": {"method": "median", "min_samples": 3},
        })
        self.assertEqual(status, 201)
        # 审批人批准
        status, _ = call(self.app, "POST", "/rules/decide", self.w.approver["token"],
                         {"rule_id": draft["id"], "approve": True})
        self.assertEqual(status, 200)

        # 五家机构提交
        for name, v in {"alpha": 4.92, "beta": 4.99, "gamma": 5.01,
                        "delta": 4.95, "epsilon": 5.03}.items():
            status, _ = call(self.app, "POST", "/submissions",
                             self.w.forecasters[name]["token"], {
                                 "round": "2026-W40", "indicator": "IPCA",
                                 "forecast_year": 2026, "point_value": v,
                                 "confidence": 0.9, "rationale": f"{name} 判断",
                                 "client_ref": f"http-{name}-1"})
            self.assertEqual(status, 201)

        # 幂等：同样的回执重放 -> 200 + replayed
        status, replay = call(self.app, "POST", "/submissions",
                              self.w.forecasters["alpha"]["token"], {
                                  "round": "2026-W40", "indicator": "IPCA",
                                  "forecast_year": 2026, "point_value": 4.92,
                                  "confidence": 0.9, "rationale": "alpha 判断",
                                  "client_ref": "http-alpha-1"})
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        # 同编号异内容 -> 409 隔离
        status, q = call(self.app, "POST", "/submissions",
                         self.w.forecasters["beta"]["token"], {
                             "round": "2026-W40", "indicator": "IPCA",
                             "forecast_year": 2026, "point_value": 99.0,
                             "rationale": "beta 判断", "client_ref": "http-beta-1"})
        self.assertEqual(status, 409)
        self.assertEqual(q["error"], "quarantined")

        # 发布人申请、审批人批准
        status, pub = call(self.app, "POST", "/publications/request",
                           self.w.publisher["token"], {
                               "round_id": self.w.round["id"],
                               "indicator_id": self.w.ipca["id"],
                               "forecast_year": 2026})
        self.assertEqual(status, 201)
        status, decided = call(self.app, "POST", "/publications/decide",
                               self.w.approver["token"],
                               {"publication_id": pub["id"], "approve": True})
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "published")
        self.assertAlmostEqual(decided["consensus_value"], 4.99)

        # 机构查询解释：他人理由不可见
        status, view = call(self.app, "GET", "/publications/explain",
                            self.w.forecasters["alpha"]["token"], query=f"id={pub['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(all(s["rationale"] is None for s in view["samples"]
                            if s["anon_code"] != view["your_anon_code"]))

        # 审计员看审计日志
        status, log = call(self.app, "GET", "/audit", self.w.auditor["token"])
        self.assertEqual(status, 200)
        actions = {e["action"] for e in log}
        self.assertIn("rule.approve", actions)
        self.assertIn("quarantine.hold", actions)

    def test_bootstrap_guard(self) -> None:
        status, body = call(self.app, "POST", "/admin/bootstrap", None, {"confirm": True})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

    def test_role_denial(self) -> None:
        # 贡献者不能起草规则
        status, body = call(self.app, "POST", "/rules",
                            self.w.forecasters["alpha"]["token"], {
                                "indicator_id": self.w.ipca["id"],
                                "params": {"method": "median"}})
        self.assertEqual(status, 403)

    def test_bad_json(self) -> None:
        environ = {
            "REQUEST_METHOD": "GET", "PATH_INFO": "/publications",
            "QUERY_STRING": "", "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
            "HTTP_AUTHORIZATION": f"Bearer {self.w.auditor['token']}",
        }
        captured = {}
        raw = b"".join(self.app(environ, lambda s, h: captured.setdefault("s", int(s.split()[0]))))
        self.assertEqual(captured["s"], 200)


if __name__ == "__main__":
    unittest.main()
