"""规则职责分离、发布水位、快照与匿名解释测试。"""

import unittest

from src.macro_survey.errors import (
    DomainError,
    ForbiddenError,
    PublishStateError,
    RuleStateError,
)
from tests.world import World


class RuleAndPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()

    def tearDown(self) -> None:
        self.w.close()

    def _approved_rule(self, params=None) -> dict:
        params = params or {"method": "median", "min_samples": 3,
                            "outlier": {"method": "iqr", "k": 1.5}}
        draft = self.w.svc.draft_rule(self.w.stat, self.w.ipca["id"], params)
        return self.w.svc.decide_rule(self.w.approver, draft["id"], True)

    def _five_submissions(self) -> None:
        values = {"alpha": 4.92, "beta": 4.99, "gamma": 5.01, "delta": 4.95, "epsilon": 5.03}
        for name, v in values.items():
            self.w.submit(name, v, ref=f"{name}-1",
                          rationale=f"{name} 对能源价格的判断")

    def test_rule_lifecycle_and_separation_of_duty(self) -> None:
        draft = self.w.svc.draft_rule(
            self.w.stat, self.w.ipca["id"], {"method": "mean", "min_samples": 3})
        self.assertEqual(draft["status"], "draft")
        # 起草人不能批准自己的规则
        with self.assertRaises(ForbiddenError):
            self.w.svc.decide_rule(self.w.stat, draft["id"], True)
        # 统计员不能充当审批人
        with self.assertRaises(ForbiddenError):
            self.w.svc.decide_rule(self.w.publisher, draft["id"], True)
        approved = self.w.svc.decide_rule(self.w.approver, draft["id"], True)
        self.assertEqual(approved["status"], "approved")
        # 不可重复审批
        with self.assertRaises(RuleStateError):
            self.w.svc.decide_rule(self.w.approver, draft["id"], True)

    def test_reject_requires_reason(self) -> None:
        draft = self.w.svc.draft_rule(self.w.stat, self.w.ipca["id"], {"method": "mean"})
        with self.assertRaises(DomainError):
            self.w.svc.decide_rule(self.w.approver, draft["id"], False)
        rejected = self.w.svc.decide_rule(self.w.approver, draft["id"], False, "异常值规则过松")
        self.assertEqual(rejected["status"], "rejected")

    def test_publish_requires_approved_rule(self) -> None:
        self._five_submissions()
        with self.assertRaisesRegex(RuleStateError, "经审批生效"):
            self.w.svc.request_publication(
                self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)

    def test_request_then_approve_publication(self) -> None:
        self._approved_rule()
        self._five_submissions()
        pub = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)
        self.assertEqual(pub["status"], "pending_approval")
        self.assertEqual(pub["n_samples"], 5)
        # 申请人不能批准
        with self.assertRaises(ForbiddenError):
            self.w.svc.decide_publication(self.w.publisher, pub["id"], True)
        out = self.w.svc.decide_publication(self.w.approver, pub["id"], True)
        self.assertEqual(out["status"], "published")
        self.assertAlmostEqual(out["consensus_value"], 4.99)

    def test_watermark_freezes_samples(self) -> None:
        self._approved_rule()
        self.w.submit("alpha", 4.92, ref="alpha-1")
        self.w.submit("beta", 4.99, ref="beta-1")
        self.w.submit("gamma", 5.01, ref="gamma-1")
        self.w.submit("delta", 4.95, ref="delta-1")
        self.w.submit("epsilon", 5.03, ref="epsilon-1")
        # 申请时定格水位
        pub = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)
        # 水位之后的新修订（未迟到，但发布单已建）不影响已冻结快照
        self.w.revise("alpha", 9.99, "alpha-2")
        explanation = self.w.svc.explain_publication(self.w.auditor, pub["id"])
        alpha_rows = [s for s in explanation["samples"]
                      if s["revision_seq"] == 1 and s["point_value"] == 4.92]
        self.assertEqual(len(alpha_rows), 1)
        self.assertEqual(explanation["consensus_value"], pub["consensus_value"])

    def test_late_submission_never_enters_publication(self) -> None:
        self._approved_rule()
        for name in ("alpha", "beta", "gamma", "delta"):
            self.w.submit(name, 5.0, ref=f"{name}-1")
        # epsilon 迟到
        self.w.clock.set("2026-10-03T08:00:00+00:00")
        self.w.submit("epsilon", 1.0, ref="epsilon-1")
        # 发布人在截止后申请，epsilon 的迟到记录不得入水位
        pub = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)
        self.assertEqual(pub["n_samples"], 4)

    def test_withdrawn_kept_in_snapshot_history(self) -> None:
        self._approved_rule()
        self.w.submit("alpha", 4.92, ref="alpha-1")
        self.w.submit("beta", 4.99, ref="beta-1")
        self.w.withdraw("alpha", "alpha-w")
        self.w.submit("gamma", 5.01, ref="gamma-1")
        self.w.submit("delta", 4.95, ref="delta-1")
        # alpha 撤回后有效样本只有 3 家，满足 min_samples=3
        pub = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)
        self.assertEqual(pub["n_samples"], 3)
        # 但"曾参与共识征集"的事实在 staff 提交视图中完整保留
        all_subs = self.w.svc.staff_list_submissions(self.w.admin, "2026-W40")
        alpha = [s for s in all_subs if s["institution"] == "alpha"]
        self.assertEqual([s["status"] for s in alpha], ["withdrawn", "withdrawn"])

    def test_min_samples_blocks_publication(self) -> None:
        self._approved_rule({"method": "median", "min_samples": 4})
        self.w.submit("alpha", 1.0, ref="a")
        self.w.submit("beta", 2.0, ref="b")
        self.w.submit("gamma", 3.0, ref="c")
        with self.assertRaisesRegex(PublishStateError, "低于规则下限"):
            self.w.svc.request_publication(
                self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)

    # ----- 匿名化 -----

    def test_institution_cannot_infer_others(self) -> None:
        self._approved_rule()
        self._five_submissions()
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        self.w.svc.decide_publication(self.w.approver, pub_id, True)
        view = self.w.svc.explain_publication(self.w.forecasters["alpha"], pub_id)
        self.assertEqual(view["view"], "institution")
        others = [s for s in view["samples"] if s["anon_code"] != view["your_anon_code"]]
        self.assertTrue(others)
        self.assertTrue(all(s["rationale"] is None for s in others))
        self.assertTrue(all(s["institution"] is None for s in others))
        # 自己的理由可见，自己的匿名码可知
        own = [s for s in view["samples"] if s["anon_code"] == view["your_anon_code"]]
        self.assertEqual(own[0]["rationale"], "alpha 对能源价格的判断")

    def test_institution_cannot_see_unpublished(self) -> None:
        self._approved_rule()
        self._five_submissions()
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        with self.assertRaises(ForbiddenError):
            self.w.svc.explain_publication(self.w.forecasters["beta"], pub_id)
        # 列表里也看不到待批单
        pubs = self.w.svc.list_publications(self.w.forecasters["beta"])
        self.assertEqual(pubs, [])

    def test_explain_lists_rule_and_revision_order(self) -> None:
        self._approved_rule()
        self.w.submit("alpha", 4.90, ref="alpha-1")
        self.w.revise("alpha", 4.92, "alpha-2")
        self.w.submit("beta", 4.99, ref="beta-1")
        self.w.submit("gamma", 5.01, ref="gamma-1")
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        explanation = self.w.svc.explain_publication(self.w.auditor, pub_id)
        self.assertEqual(explanation["rule"]["version"], 1)
        # 快照采用的是 alpha 的第 2 版
        alpha = [s for s in explanation["samples"] if s["institution"] ==
                 self.w.institutions["alpha"]["id"]][0]
        self.assertEqual(alpha["revision_seq"], 2)
        self.assertEqual(alpha["point_value"], 4.92)
        self.assertEqual(len(explanation["revision_timeline"]), 3)

    def test_recompute_matches_and_hash_chain(self) -> None:
        self._approved_rule()
        self._five_submissions()
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        integrity = self.w.svc.verify_publication_integrity(self.w.auditor, pub_id)
        self.assertTrue(integrity["matches"])
        self.assertTrue(integrity["hash_chain_intact"])

    def test_explain_revision_history_shows_superseded(self) -> None:
        self._approved_rule()
        self.w.submit("alpha", 4.90, ref="alpha-1", rationale="初版")
        self.w.revise("alpha", 4.92, "alpha-2", rationale="能源优惠结束")
        self.w.submit("beta", 4.99, ref="beta-1")
        self.w.submit("gamma", 5.01, ref="gamma-1")
        pub_id = self.w.svc.request_publication(
            self.w.publisher, self.w.round["id"], self.w.ipca["id"], 2026)["id"]
        explanation = self.w.svc.explain_publication(self.w.auditor, pub_id)
        alpha_hist = [h for h in explanation["revision_history"]
                      if h["institution"] == self.w.institutions["alpha"]["id"]]
        self.assertEqual([(h["seq"], h["status"]) for h in alpha_hist],
                         [(1, "superseded"), (2, "active")])
        self.assertFalse(alpha_hist[0]["in_watermark"])
        self.assertTrue(alpha_hist[1]["in_watermark"])
        self.assertEqual(alpha_hist[0]["rationale"], "初版")


if __name__ == "__main__":
    unittest.main()
