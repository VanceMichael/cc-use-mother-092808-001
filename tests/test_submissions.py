"""提交、修订、撤回与发布水位行为。"""

from __future__ import annotations

import unittest

from src.forecast_backend import DomainError
from tests.forecast_case import ForecastCase


class SubmissionTest(ForecastCase):
    def test_submit_and_revise_keeps_full_chain(self) -> None:
        first = self.submit("bank-a", 4.8, rationale="能源优惠仍在")
        self.assertEqual(first["revision"], 1)
        self.clock.advance(hours=6)
        second = self.submit("bank-a", 4.9, rationale="能源优惠结束，上调")
        self.assertEqual(second["revision"], 2)
        self.assertFalse(second["late_revision"])

        history = self.system.revision_history(
            self.auditor,
            institution_id="bank-a",
            indicator="IPCA",
            target_year=2026,
            round_id="R1",
        )
        self.assertEqual([row["revision"] for row in history], [1, 2])
        self.assertEqual(history[0]["status"], "superseded")
        self.assertEqual(history[0]["rationale"], "能源优惠仍在")
        self.assertEqual(history[1]["status"], "active")
        self.assertLess(history[0]["submitted_at"], history[1]["submitted_at"])

    def test_forecast_validation(self) -> None:
        with self.assertRaisesRegex(DomainError, "下限不能高于预测值"):
            self.submit("bank-a", 4.8, lower=5.0)
        with self.assertRaisesRegex(DomainError, "上限不能低于预测值"):
            self.submit("bank-a", 4.8, upper=4.0)
        with self.assertRaisesRegex(DomainError, "置信度"):
            self.submit("bank-a", 4.8, confidence=1.5)
        with self.assertRaisesRegex(DomainError, "理由"):
            self.submit("bank-a", 4.8, rationale="  ")

    def test_unaccredited_and_cross_institution_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "未获准"):
            self.submit("ghost-bank", 4.8)
        with self.assertRaisesRegex(DomainError, "本机构"):
            self.system.submit_forecast(
                self.actor("bank-a"),
                receipt_id=self.receipt(),
                institution_id="bank-b",
                indicator="IPCA",
                target_year=2026,
                round_id="R1",
                value=4.8,
                rationale="x",
            )

    def test_closed_round_rejects_submission(self) -> None:
        self.system.close_round(self.statistician, round_id="R1")
        with self.assertRaisesRegex(DomainError, "已关闭"):
            self.submit("bank-a", 4.8)

    def test_withdraw_keeps_participation_fact(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.publish_round()
        self.assertEqual(publication["computed"]["n_included"], 4)

        self.system.withdraw_forecast(
            self.actor("bank-a"),
            institution_id="bank-a",
            indicator="IPCA",
            target_year=2026,
            round_id="R1",
            reason="数据口径错误",
        )
        # 已发布水位不变，且快照仍记录 bank-a 当时参与了共识
        explanation = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        self.assertEqual(explanation["watermark"]["median"], 4.95)
        contributors = {c["institution_id"]: c for c in explanation["contributors"]}
        self.assertTrue(contributors["bank-a"]["included"])

        # 撤回是幂等的
        again = self.system.withdraw_forecast(
            self.actor("bank-a"),
            institution_id="bank-a",
            indicator="IPCA",
            target_year=2026,
            round_id="R1",
            reason="重复撤回",
        )
        self.assertEqual(again["status"], "already_withdrawn")

        # 新一轮计算不再纳入 bank-a
        second = self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(second["computed"]["n_candidates"], 3)
        chain = second["samples"]["revision_chain"]
        bank_a = [entry for entry in chain if entry["pseudonym"].startswith("anon-")]
        self.assertTrue(any(entry["state"] == "effective" for entry in bank_a))

    def test_late_revision_does_not_overwrite_watermark(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.publish_round()
        self.assertEqual(publication["computed"]["median"], 4.95)

        self.clock.advance(hours=2)
        late = self.submit("bank-a", 5.5, rationale="油价冲击，迟到修订")
        self.assertTrue(late["late_revision"])

        # 水位保持不变
        watermark = self.system.watermark(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(watermark["median"], 4.95)
        self.assertEqual(watermark["unincorporated_revisions"], 1)

        # 迟到修订进入下一版本
        second = self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(second["vintage"], 2)
        self.assertEqual(second["computed"]["median"], 5.05)
        # 第一版解释仍显示旧值与旧修订顺序
        explanation = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        bank_a_chain = [
            entry
            for entry in explanation["revision_chain"]
            if entry["institution_id"] == "bank-a"
        ]
        self.assertEqual(len(bank_a_chain), 1)
        self.assertEqual(bank_a_chain[0]["value"], 4.8)


if __name__ == "__main__":
    unittest.main()
