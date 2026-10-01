"""征集、水位、撤回、幂等与隔离测试。"""

import unittest

from src.macro_survey.errors import (
    ClosedRoundError,
    DomainError,
    ForbiddenError,
    QuarantineError,
)
from tests.world import World


class SubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()

    def tearDown(self) -> None:
        self.w.close()

    def test_first_submission_and_seq(self) -> None:
        r = self.w.submit("alpha", 4.92)
        self.assertEqual(r["seq"], 1)
        self.assertEqual(r["status"], "active")
        self.assertTrue(r["eligible"])

    def test_revision_supersedes_but_history_kept(self) -> None:
        self.w.submit("alpha", 4.92, ref="a1", rationale="基线")
        r2 = self.w.revise("alpha", 4.99, "a2", rationale="能源优惠结束")
        self.assertEqual(r2["seq"], 2)
        history = self.w.svc.list_own_submissions(self.w.forecasters["alpha"], "2026-W40", "IPCA", 2026)
        statuses = [(h["seq"], h["status"]) for h in history]
        self.assertEqual(statuses, [(1, "superseded"), (2, "active")])
        # 旧版理由仍可查（发布前可见"何时改过判断、理由是什么"）
        self.assertEqual(history[0]["rationale"], "基线")
        self.assertEqual(history[1]["rationale"], "能源优惠结束")

    def test_withdraw_keeps_participation_fact(self) -> None:
        self.w.submit("alpha", 4.92, ref="a1")
        r = self.w.withdraw("alpha", "aw1")
        self.assertEqual(r["kind"], "withdraw")
        history = self.w.svc.list_own_submissions(self.w.forecasters["alpha"], "2026-W40", "IPCA", 2026)
        self.assertEqual(history[0]["status"], "withdrawn")
        self.assertEqual(history[1]["kind"], "withdraw")
        self.assertEqual(history[1]["status"], "withdrawn")
        self.assertIsNone(history[1]["point_value"])

    def test_late_revision_does_not_override_watermark(self) -> None:
        self.w.submit("alpha", 4.92, ref="a1")
        # 截止之后到达的修订
        self.w.clock.set("2026-10-03T09:00:00+00:00")
        late = self.w.revise("alpha", 6.10, "a-late")
        self.assertTrue(late["late"])
        self.assertEqual(late["status"], "late")
        self.assertFalse(late["eligible"])
        # 水位内的有效样本仍是 4.92
        samples = self.w.svc._active_samples(
            self.w.round["id"], self.w.ipca["id"], 2026, "2026-10-03T10:00:00+00:00")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["point_value"], 4.92)

    def test_late_withdraw_does_not_retract(self) -> None:
        self.w.submit("alpha", 4.92, ref="a1")
        self.w.clock.set("2026-10-03T09:00:00+00:00")
        late = self.w.withdraw("alpha", "aw-late")
        self.assertEqual(late["status"], "late")
        samples = self.w.svc._active_samples(
            self.w.round["id"], self.w.ipca["id"], 2026, "2026-10-03T10:00:00+00:00")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["point_value"], 4.92)

    def test_closed_round_rejected(self) -> None:
        self.w.svc.close_round(self.w.publisher, self.w.round["id"])
        with self.assertRaises(ClosedRoundError):
            self.w.submit("alpha", 5.0, ref="a1")

    # ----- 幂等 / 隔离 -----

    def test_duplicate_receipt_returns_original(self) -> None:
        body = {
            "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
            "point_value": 4.92, "confidence": 0.8, "rationale": "基线",
            "client_ref": "dup-1",
        }
        first = self.w.svc.submit_forecast(self.w.forecasters["alpha"], body)
        again = self.w.svc.submit_forecast(self.w.forecasters["alpha"], dict(body))
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(again["submission_id"], first["submission_id"])
        self.assertEqual(again["seq"], first["seq"])

    def test_same_ref_different_content_quarantined(self) -> None:
        body = {
            "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
            "point_value": 4.92, "rationale": "基线", "client_ref": "q-1",
        }
        self.w.svc.submit_forecast(self.w.forecasters["alpha"], body)
        changed = dict(body, point_value=5.30)
        with self.assertRaises(QuarantineError):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], changed)
        q = self.w.svc.list_quarantine(self.w.stat)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["reason"], "回执编号 q-1 曾用于不同内容")

    def test_quarantine_discard_then_ref_free(self) -> None:
        body = {"round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 4.92, "rationale": "x", "client_ref": "q-2"}
        self.w.svc.submit_forecast(self.w.forecasters["alpha"], body)
        with self.assertRaises(QuarantineError):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], dict(body, point_value=9.0))
        qid = self.w.svc.list_quarantine(self.w.stat)[0]["id"]
        out = self.w.svc.resolve_quarantine(self.w.publisher, qid, "discard", "重复攻击")
        self.assertEqual(out["decision"], "discard")
        self.assertEqual(self.w.svc.list_quarantine(self.w.stat), [])

    def test_quarantine_accept_reenters_as_new_ref(self) -> None:
        body = {"round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 4.92, "rationale": "x", "client_ref": "q-3"}
        self.w.svc.submit_forecast(self.w.forecasters["alpha"], body)
        with self.assertRaises(QuarantineError):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], dict(body, point_value=5.0))
        qid = self.w.svc.list_quarantine(self.w.stat)[0]["id"]
        out = self.w.svc.resolve_quarantine(self.w.publisher, qid, "accepted", "人工核对为真实修订")
        self.assertEqual(out["submission"]["status"], "active")
        self.assertEqual(out["submission"]["seq"], 2)

    # ----- 输入与权限 -----

    def test_rationale_required(self) -> None:
        with self.assertRaisesRegex(DomainError, "理由"):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], {
                "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 5.0, "rationale": "  ", "client_ref": "r1"})

    def test_interval_must_contain_point(self) -> None:
        with self.assertRaisesRegex(DomainError, "区间"):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], {
                "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 5.0, "low": 5.1, "high": 5.2,
                "rationale": "x", "client_ref": "r2"})

    def test_confidence_bounds(self) -> None:
        with self.assertRaisesRegex(DomainError, "confidence"):
            self.w.svc.submit_forecast(self.w.forecasters["alpha"], {
                "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 5.0, "confidence": 1.2, "rationale": "x", "client_ref": "r3"})

    def test_staff_cannot_submit_as_institution(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.w.svc.submit_forecast(self.w.stat, {
                "round": "2026-W40", "indicator": "IPCA", "forecast_year": 2026,
                "point_value": 5.0, "rationale": "x", "client_ref": "r4"})

    def test_year_must_belong_to_round(self) -> None:
        with self.assertRaisesRegex(DomainError, "预测年份"):
            self.w.submit("alpha", 5.0, year=2030, ref="r5")


if __name__ == "__main__":
    unittest.main()
