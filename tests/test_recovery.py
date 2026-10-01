"""进程重启后的恢复：待批准事项与到期提醒不丢失。"""

from __future__ import annotations

import unittest

from src.forecast_backend import ForecastSystem
from tests.forecast_case import ForecastCase


class RecoveryTest(ForecastCase):
    def _restart(self) -> None:
        """关闭并用同一数据库文件重建系统（模拟进程重启）。"""
        self.system.close()
        self.system = ForecastSystem(self.db_path, clock=self.clock)

    def test_pending_publication_survives_restart(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self._restart()

        pending = self.system.pending_approvals()
        self.assertEqual(
            [row["publication_id"] for row in pending["publications"]],
            [publication["publication_id"]],
        )
        # 重启后可直接继续审批
        published = self.system.publish(self.approver, publication_id=publication["publication_id"])
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["computed"]["median"], 4.95)

    def test_pending_rule_survives_restart(self) -> None:
        self.system.propose_rule(
            self.statistician,
            rule_id="rule-1",
            scope_indicator=None,
            definition={"weighting": "equal", "min_institutions": 3},
        )
        self.system.submit_rule_for_approval(self.statistician, rule_id="rule-1", version=1)
        self._restart()
        pending = self.system.pending_approvals()
        self.assertEqual([row["rule_id"] for row in pending["rules"]], ["rule-1"])
        result = self.system.approve_rule(self.approver, rule_id="rule-1", version=1)
        self.assertEqual(result["status"], "active")

    def test_due_reminders_fire_on_recover(self) -> None:
        self.fill_round()
        self.make_rule()
        self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        # 推进时间：越过审批提醒(24h)与轮次截止
        self.clock.advance(days=5)
        self._restart()

        report = self.system.recover()
        kinds = {reminder["kind"] for reminder in report["fired_reminders"]}
        self.assertIn("publication_approval", kinds)
        self.assertIn("round_close", kinds)
        # 再次恢复不会重复触发
        second = self.system.recover()
        self.assertEqual(second["fired_reminders"], [])

    def test_reminders_cancelled_when_handled_before_due(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.system.compute_publication(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.system.publish(self.approver, publication_id=publication["publication_id"])
        self.system.close_round(self.statistician, round_id="R1")
        self.clock.advance(days=5)
        self._restart()
        report = self.system.recover()
        self.assertEqual(report["fired_reminders"], [], "已处理的事项不应再提醒")

    def test_submissions_and_watermark_survive_restart(self) -> None:
        self.fill_round()
        self.make_rule()
        publication = self.publish_round()
        self._restart()
        explanation = self.system.explain_publication(
            self.auditor, publication_id=publication["publication_id"]
        )
        self.assertEqual(len(explanation["contributors"]), 4)
        watermark = self.system.watermark(
            self.statistician, round_id="R1", indicator="IPCA", target_year=2026
        )
        self.assertTrue(watermark["published"])
        self.assertEqual(watermark["median"], 4.95)


if __name__ == "__main__":
    unittest.main()
