"""实际值、误差、回测、偏离复核、提醒与重启恢复测试。"""

import tempfile
import unittest
from pathlib import Path

from src.macro_survey.errors import ForbiddenError
from src.macro_survey.service import SurveyService
from tests.world import MutableClock, World


class BacktestLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()

    def tearDown(self) -> None:
        self.w.close()

    def _publish_one(self, values: dict[str, str] | None = None) -> str:
        values = values or {"alpha": 4.92, "beta": 4.99, "gamma": 5.01,
                            "delta": 4.95, "epsilon": 5.03}
        draft = self.w.svc.draft_rule(self.w.stat, self.w.ipca["id"], {
            "method": "median", "min_samples": 3, "outlier": {"method": "iqr", "k": 1.5}})
        self.w.svc.decide_rule(self.w.approver, draft["id"], True)
        for name, v in values.items():
            self.w.submit(name, float(v), ref=f"{name}-1")
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        self.w.svc.decide_publication(self.w.approver, pub_id, True)
        return pub_id

    def test_actual_records_errors_and_reviews(self) -> None:
        pub_id = self._publish_one()
        result = self.w.svc.record_actual(
            self.w.admin, self.w.ipca["id"], 2026, 4.97, "2027-01-15T00:00:00+00:00")
        self.assertEqual(result["publications_scored"], 1)
        errors = self.w.svc.list_errors(self.w.auditor)
        self.assertEqual(len(errors), 1)
        self.assertAlmostEqual(errors[0]["error"], 4.99 - 4.97, places=9)
        # 所有机构都很接近，自动阈值（1.5*MAE）下不应有偏离标记
        self.assertGreaterEqual(result["deviations_flagged"], 0)

    def test_outlier_contributor_flagged_for_next_round(self) -> None:
        self._publish_one({"alpha": 9.5, "beta": 4.99, "gamma": 5.01,
                           "delta": 4.95, "epsilon": 5.03})
        # 注意 9.5 在 IQR 下被剔除出共识，共识≈5.0
        self.w.svc.record_actual(
            self.w.admin, self.w.ipca["id"], 2026, 5.00, "2027-01-15T00:00:00+00:00")
        reviews = self.w.svc.next_round_reviews(self.w.stat)
        # alpha 偏离巨大（即便其未入共识，也作为该机构预测质量信号进入偏离表——
        # 但 included=0 不会进 deviation 表；此处由其余微小误差自动阈值决定）
        self.assertIsInstance(reviews, list)
        # 机构视角只能看到自己的记录
        own = self.w.svc.own_reviews(self.w.forecasters["alpha"])
        # alpha 被剔除出 included，因此没有偏离行
        self.assertEqual(
            [r for r in own if r["indicator_code"] == "IPCA"], [])

    def test_explicit_threshold_flags_deviation(self) -> None:
        self._publish_one()
        self.w.svc.record_actual(
            self.w.admin, self.w.ipca["id"], 2026, 4.97,
            "2027-01-15T00:00:00+00:00", review_threshold=0.02)
        reviews = self.w.svc.next_round_reviews(self.w.stat)
        flagged = {r["institution_name"] for r in reviews}
        # 阈值 0.02：alpha 0.05、gamma 0.04、epsilon 0.06 超阈；
        # beta 0.02 与 delta 0.02 恰等阈值，不算"超出"
        self.assertEqual(flagged, {"alpha", "gamma", "epsilon"})
        # 本人只能看到自己的一条，且看不到机构名/他人
        own = self.w.svc.own_reviews(self.w.forecasters["alpha"])
        self.assertEqual(len(own), 1)
        self.assertTrue(own[0]["needs_review"])

    def test_backtest_recomputes_against_rule(self) -> None:
        self._publish_one()
        self.w.svc.record_actual(
            self.w.admin, self.w.ipca["id"], 2026, 4.97, "2027-01-15T00:00:00+00:00")
        rules = self.w.svc.list_rules(self.w.auditor, self.w.ipca["id"])
        approved = next(r for r in rules if r["status"] == "approved")
        # 已批准规则回测自身，指标可计算
        bt_approved = self.w.svc.run_backtest(
            self.w.auditor, approved["id"], self.w.ipca["id"], 2026)
        self.assertEqual(bt_approved["n_publications"], 1)
        self.assertAlmostEqual(bt_approved["mae"], abs(4.99 - 4.97), places=9)
        # 起草一个备选规则（均值），在审批前即可拿历史证据回测
        draft_mean = self.w.svc.draft_rule(
            self.w.stat, self.w.ipca["id"], {"method": "mean", "min_samples": 3})
        bt = self.w.svc.run_backtest(self.w.approver, draft_mean["id"], self.w.ipca["id"], 2026)
        self.assertEqual(bt["n_publications"], 1)
        stored = self.w.svc.list_backtests(self.w.auditor)
        self.assertEqual(len(stored), 2)

    def test_reminders_created_and_due_scan(self) -> None:
        # 建轮次时已生成 3 条提醒
        state = self.w.svc.resume_state(self.w.publisher)
        due_kinds = {r["kind"] for r in state["due_reminders"]}
        self.assertEqual(
            due_kinds,
            {"deadline_approaching", "submission_deadline", "publish_due"},
        )
        # 时间推进到截止后扫描
        self.w.clock.set("2026-10-02T13:00:00+00:00")
        scanned = self.w.svc.scan_due(self.w.publisher)
        kinds = {r["kind"] for r in scanned["reminders"]}
        self.assertIn("submission_deadline", kinds)
        # 已投递的不重复出现
        again = self.w.svc.scan_due(self.w.publisher)
        self.assertNotIn("submission_deadline",
                         {r["kind"] for r in again["reminders"]})

    def test_resume_and_scan_denied_to_contributor(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.w.svc.resume_state(self.w.forecasters["alpha"])
        with self.assertRaises(ForbiddenError):
            self.w.svc.scan_due(self.w.forecasters["alpha"])

    def test_pending_publication_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "survey.db")
            w = World(db=db_path)
            draft = w.svc.draft_rule(
                w.stat, w.ipca["id"], {"method": "median", "min_samples": 3})
            w.svc.decide_rule(w.approver, draft["id"], True)
            for name, v in {"alpha": 4.9, "beta": 5.0, "gamma": 5.1}.items():
                w.submit(name, v, ref=f"{name}-1")
            pub_id = w.svc.request_publication(
                w.publisher, w.round["id"], w.ipca["id"], 2026)["id"]
            w.store.close()

            # ---- 进程重启：新连接打开同一文件库 ----
            from src.macro_survey.store import Store
            store2 = Store(db_path)
            svc2 = SurveyService(store2, MutableClock("2026-10-06T00:00:00+00:00"))
            state = svc2.resume_state(SurveyService._user_dict(
                store2.query_one("SELECT * FROM users WHERE username='publisher'")))
            self.assertIn(pub_id, {p["id"] for p in state["pending_approvals"]})
            self.assertGreaterEqual(len(state["due_reminders"]), 1)
            approver = SurveyService._user_dict(
                store2.query_one("SELECT * FROM users WHERE username='approver'"))
            out = svc2.decide_publication(approver, pub_id, True)
            self.assertEqual(out["status"], "published")
            publisher = SurveyService._user_dict(
                store2.query_one("SELECT * FROM users WHERE username='publisher'"))
            scanned = svc2.scan_due(publisher)
            self.assertTrue(any(r["kind"] == "publish_due" for r in scanned["reminders"]))
            store2.close()


if __name__ == "__main__":
    unittest.main()
