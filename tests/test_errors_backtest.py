"""实际值登记、预测误差、复核标记与规则回测。"""

from __future__ import annotations

import unittest

from src.forecast_backend import DomainError
from tests.forecast_case import ForecastCase


class ErrorAndBacktestTest(ForecastCase):
    def _published(self, values: tuple[float, ...] = (4.8, 4.9, 5.0, 5.1)) -> dict:
        self.fill_round(values)
        self.make_rule()
        return self.publish_round()

    def test_record_actual_stores_consensus_and_institution_errors(self) -> None:
        publication = self._published()
        summary = self.system.record_actual(
            self.statistician, indicator="IPCA", target_year=2026, value=5.0
        )
        self.assertEqual(summary["publications_evaluated"], 1)
        # 2 条共识误差 + 4 条机构误差
        self.assertEqual(summary["errors_recorded"], 6)

        errors = self.system.list_errors(
            self.auditor, publication_id=publication["publication_id"]
        )
        consensus = {row["metric"]: row["value"] for row in errors if row["scope"] == "consensus"}
        self.assertAlmostEqual(consensus["median_error"], 4.95 - 5.0)
        institution = [row for row in errors if row["scope"] == "institution"]
        self.assertEqual(len(institution), 4)
        by_subject = {row["subject"]: row["value"] for row in institution}
        self.assertAlmostEqual(by_subject["bank-a"], 4.8 - 5.0)

    def test_duplicate_actual_rejected(self) -> None:
        self._published()
        self.system.record_actual(self.statistician, indicator="IPCA", target_year=2026, value=5.0)
        with self.assertRaisesRegex(DomainError, "已登记"):
            self.system.record_actual(
                self.statistician, indicator="IPCA", target_year=2026, value=5.1
            )

    def test_deviation_flags_raised_for_next_round_review(self) -> None:
        self._published()
        # 实际值大幅偏离：bank-a(4.8) 与共识(4.95) 都超过 0.25 阈值
        summary = self.system.record_actual(
            self.statistician, indicator="IPCA", target_year=2026, value=5.4
        )
        self.assertGreater(summary["flags_created"], 0)
        flags = self.system.list_review_flags(self.statistician, status="open")
        reasons = {flag["reason"] for flag in flags}
        self.assertIn("共识偏离超过复核阈值", reasons)
        self.assertIn("机构预测偏离超过复核阈值", reasons)
        # 统计视角的机构身份已脱敏
        institution_flags = [f for f in flags if f["reason"].startswith("机构")]
        self.assertTrue(
            all(f["detail"]["institution_id"].startswith("anon-") for f in institution_flags)
        )
        # 复核后可关闭
        resolved = self.system.resolve_review_flag(
            self.statistician, flag_id=flags[0]["id"], note="已在第2轮复核"
        )
        self.assertEqual(resolved["status"], "resolved")
        remaining = self.system.list_review_flags(self.statistician, status="open")
        self.assertEqual(len(remaining), len(flags) - 1)

    def test_institution_sees_only_own_flags(self) -> None:
        self._published()
        self.system.record_actual(self.statistician, indicator="IPCA", target_year=2026, value=5.4)
        flags = self.system.list_review_flags(self.actor("bank-a"), status="open")
        owners = {
            flag["detail"].get("institution_id")
            for flag in flags
            if "institution_id" in flag["detail"]
        }
        self.assertEqual(owners, {"bank-a"})

    def test_backtest_over_multiple_rounds(self) -> None:
        # 第1轮发布
        self._published()
        # 第2轮：另一组数值
        self.system.open_round(
            self.statistician,
            round_id="R2",
            label="第2轮",
            opens_at="2026-10-01T00:00:00+00:00",
            closes_at="2026-10-12T00:00:00+00:00",
        )
        self.fill_round((4.9, 5.0, 5.1, 5.2), round_id="R2")
        self.publish_round(round_id="R2")
        self.system.record_actual(self.statistician, indicator="IPCA", target_year=2026, value=5.0)

        result = self.system.run_backtest(
            self.statistician, rule_id="rule-1", rule_version=1, indicator="IPCA", target_year=2026
        )
        self.assertEqual(result["rounds_evaluated"], 2)
        self.assertAlmostEqual(result["rounds"][0]["median"], 4.95)
        self.assertAlmostEqual(result["rounds"][1]["median"], 5.05)
        self.assertAlmostEqual(result["mae_median"], (0.05 + 0.05) / 2)

    def test_backtest_compares_candidate_rule(self) -> None:
        self._published()
        self.system.record_actual(self.statistician, indicator="IPCA", target_year=2026, value=5.0)
        # 未批准的草稿规则也可回测（批准前先评估）
        self.system.propose_rule(
            self.statistician,
            rule_id="rule-2",
            scope_indicator=None,
            definition={
                "weighting": "confidence",
                "outlier": {"method": "none"},
                "min_institutions": 3,
                "review_threshold": 0.25,
            },
        )
        result = self.system.run_backtest(
            self.statistician, rule_id="rule-2", rule_version=1, indicator="IPCA", target_year=2026
        )
        self.assertEqual(result["rounds_evaluated"], 1)
        self.assertIn("mae_weighted_mean", result)

    def test_backtest_requires_actual(self) -> None:
        self._published()
        with self.assertRaisesRegex(DomainError, "尚无实际值"):
            self.system.run_backtest(
                self.statistician,
                rule_id="rule-1",
                rule_version=1,
                indicator="IPCA",
                target_year=2026,
            )


if __name__ == "__main__":
    unittest.main()
