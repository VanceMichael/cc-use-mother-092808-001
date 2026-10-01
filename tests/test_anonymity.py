"""匿名保护：单个机构不能从查询中推断其他贡献者。"""

from __future__ import annotations

import unittest

from src.forecast_backend import DomainError
from tests.forecast_case import ForecastCase


class AnonymityTest(ForecastCase):
    def test_institution_cannot_read_others_submissions(self) -> None:
        self.submit("bank-a", 4.8)
        self.submit("bank-b", 4.9)
        with self.assertRaisesRegex(DomainError, "其他机构"):
            self.system.list_own_submissions(self.actor("bank-a"), institution_id="bank-b")
        with self.assertRaisesRegex(DomainError, "其他机构"):
            self.system.revision_history(
                self.actor("bank-a"),
                institution_id="bank-b",
                indicator="IPCA",
                target_year=2026,
                round_id="R1",
            )
        own = self.system.list_own_submissions(self.actor("bank-a"), institution_id="bank-a")
        self.assertEqual(len(own), 1)

    def test_aggregate_refused_below_min_institutions(self) -> None:
        self.make_rule()
        self.submit("bank-a", 4.8)
        self.submit("bank-b", 4.9)
        with self.assertRaisesRegex(DomainError, "匿名保护"):
            self.system.consensus_snapshot(
                self.actor("bank-a"), round_id="R1", indicator="IPCA", target_year=2026
            )
        with self.assertRaisesRegex(DomainError, "匿名下限"):
            self.system.compute_publication(
                self.statistician, round_id="R1", indicator="IPCA", target_year=2026
            )

    def test_aggregate_contains_no_individual_data(self) -> None:
        self.fill_round()
        self.make_rule()
        snapshot = self.system.consensus_snapshot(
            self.actor("bank-a"), round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(snapshot["contributors"], 4)
        self.assertNotIn("samples", snapshot)
        self.assertNotIn("institutions", snapshot)

    def test_statistician_sees_only_pseudonyms(self) -> None:
        self.fill_round()
        samples = self.system.list_round_samples(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual(len(samples), 4)
        for sample in samples:
            self.assertTrue(sample["contributor"].startswith("anon-"))
            self.assertNotIn("institution_id", sample)
        # 审计角色可以映射回真实身份
        revealed = self.system.list_round_samples(
            self.auditor, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertEqual({row["institution_id"] for row in revealed}, set(self.INSTITUTIONS))

    def test_pseudonym_not_stable_across_rounds(self) -> None:
        self.fill_round()
        self.system.open_round(
            self.statistician,
            round_id="R2",
            label="第2轮",
            opens_at="2026-10-01T00:00:00+00:00",
            closes_at="2026-10-12T00:00:00+00:00",
        )
        self.fill_round(round_id="R2")
        first = self.system.list_round_samples(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        second = self.system.list_round_samples(
            self.statistician, round_id="R2", indicator="IPCA", target_year=2026
        )
        names_r1 = {row["contributor"] for row in first}
        names_r2 = {row["contributor"] for row in second}
        self.assertTrue(names_r1.isdisjoint(names_r2), "跨轮次代号不可串联")

    def test_publication_view_masks_by_role(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.publish_round()
        # 机构角色只见聚合
        institution_view = self.system.get_publication(
            self.actor("bank-a"), publication_id=publication["publication_id"]
        )
        self.assertNotIn("samples", institution_view)
        # 统计角色只见匿名代号
        statistician_view = self.system.explain_publication(
            self.statistician, publication_id=publication["publication_id"]
        )
        for contributor in statistician_view["contributors"]:
            self.assertNotIn("institution_id", contributor)
            self.assertTrue(contributor["pseudonym"].startswith("anon-"))
        # 审计角色可见真实身份
        auditor_view = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        self.assertIn("institution_id", auditor_view["contributors"][0])

    def test_institution_sees_only_own_errors(self) -> None:
        self.fill_round()
        self.make_rule()
        self.publish_round()
        self.system.record_actual(self.statistician, indicator="IPCA", target_year=2026, value=5.0)
        errors = self.system.list_errors(self.actor("bank-a"), indicator="IPCA")
        institution_errors = [row for row in errors if row["scope"] == "institution"]
        self.assertEqual({row["subject"] for row in institution_errors}, {"bank-a"})
        consensus_errors = [row for row in errors if row["scope"] == "consensus"]
        self.assertTrue(consensus_errors, "共识误差应对所有角色可见")


if __name__ == "__main__":
    unittest.main()
