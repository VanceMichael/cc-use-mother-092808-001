"""共识规则审批与发布流程。"""

from __future__ import annotations

import unittest

from src.forecast_backend import Actor, DomainError, ROLE_APPROVER
from tests.forecast_case import DEFAULT_RULE, ForecastCase


class RuleTest(ForecastCase):
    def test_rule_requires_four_eyes(self) -> None:
        self.system.propose_rule(
            self.statistician, rule_id="rule-1", scope_indicator=None, definition=DEFAULT_RULE
        )
        # 草稿不能直接生效
        with self.assertRaisesRegex(DomainError, "待审批"):
            self.system.approve_rule(self.approver, rule_id="rule-1", version=1)
        self.system.submit_rule_for_approval(self.statistician, rule_id="rule-1", version=1)
        # 定义者不能批准自己的规则
        with self.assertRaisesRegex(DomainError, "四眼|自己"):
            self.system.approve_rule(
                Actor("stat-1", ROLE_APPROVER), rule_id="rule-1", version=1
            )
        result = self.system.approve_rule(self.approver, rule_id="rule-1", version=1)
        self.assertEqual(result["status"], "active")

    def test_only_one_active_rule_per_scope(self) -> None:
        self.make_rule(rule_id="rule-1")
        self.make_rule(rule_id="rule-2")
        active = self.system._active_rule("IPCA")
        self.assertEqual(active["rule_id"], "rule-2")
        old = self.system._get_rule("rule-1", 1)
        self.assertEqual(old["status"], "superseded")

    def test_indicator_specific_rule_wins(self) -> None:
        self.make_rule(rule_id="global-rule")
        self.make_rule(rule_id="ipca-rule", scope_indicator="IPCA")
        active = self.system._active_rule("IPCA")
        self.assertEqual(active["rule_id"], "ipca-rule")
        active_gdp = self.system._active_rule("PIB")
        self.assertEqual(active_gdp["rule_id"], "global-rule")

    def test_invalid_rule_definition_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "加权"):
            self.system.propose_rule(
                self.statistician,
                rule_id="bad",
                scope_indicator=None,
                definition={"weighting": "magic"},
            )
        with self.assertRaisesRegex(DomainError, "至少为 2"):
            self.system.propose_rule(
                self.statistician,
                rule_id="bad2",
                scope_indicator=None,
                definition={"min_institutions": 1},
            )


class PublicationTest(ForecastCase):
    def test_publication_four_eyes_and_watermark(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(publication["status"], "pending_approval")
        # 计算者不能批准自己的发布稿
        with self.assertRaisesRegex(DomainError, "四眼|自己"):
            self.system.publish(
                Actor("stat-1", ROLE_APPROVER), publication_id=publication["publication_id"]
            )
        published = self.system.publish(self.approver, publication_id=publication["publication_id"])
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["computed"]["median"], 4.95)
        # 已发布不能再次发布
        with self.assertRaisesRegex(DomainError, "待审批"):
            self.system.publish(self.approver, publication_id=publication["publication_id"])

    def test_publication_requires_min_institutions(self) -> None:
        self.submit("bank-a", 4.8)
        self.submit("bank-b", 4.9)
        self.make_rule()
        with self.assertRaisesRegex(DomainError, "匿名下限"):
            self.system.compute_publication(
                self.statistician, round_id="R1", indicator="IPCA", target_year=2026
            )

    def test_explain_shows_samples_weights_and_revision_order(self) -> None:
        self.fill_round()
        self.clock.advance(hours=1)
        self.submit("bank-b", 5.2, rationale="气候风险上调")
        self.make_rule()
        publication = self.publish_round()

        explanation = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        self.assertEqual(explanation["rule"]["rule_id"], "rule-1")
        self.assertEqual(explanation["watermark"]["median"], 5.05)
        contributors = {c["institution_id"]: c for c in explanation["contributors"]}
        self.assertEqual(contributors["bank-b"]["value"], 5.2)
        self.assertAlmostEqual(sum(c["weight"] for c in explanation["contributors"]), 1.0)
        chain = [
            entry
            for entry in explanation["revision_chain"]
            if entry["institution_id"] == "bank-b"
        ]
        self.assertEqual([entry["state"] for entry in chain], ["superseded", "effective"])
        self.assertEqual(chain[0]["rationale"], "基线判断")
        self.assertEqual(chain[1]["rationale"], "气候风险上调")

    def test_outlier_exclusion_visible_in_explain(self) -> None:
        self.fill_round(values=(4.8, 4.9, 5.0, 9.9))
        self.make_rule()
        publication = self.publish_round()
        explanation = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        outliers = [c for c in explanation["contributors"] if c["excluded_reason"] == "outlier"]
        self.assertEqual(len(outliers), 1)
        self.assertEqual(outliers[0]["value"], 9.9)
        self.assertEqual(explanation["computed"]["n_included"], 3)

    def test_statistician_view_is_pseudonymized(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.publish_round()
        explanation = self.system.explain_publication(
            self.statistician, publication_id=publication["publication_id"]
        )
        for contributor in explanation["contributors"]:
            self.assertNotIn("institution_id", contributor)
            self.assertTrue(contributor["pseudonym"].startswith("anon-"))


if __name__ == "__main__":
    unittest.main()
