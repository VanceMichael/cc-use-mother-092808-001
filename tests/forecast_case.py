"""测试共享基座：内存时钟、临时数据库与常用角色。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from src.forecast_backend import (
    Actor,
    ForecastSystem,
    ManualClock,
    ROLE_APPROVER,
    ROLE_AUDITOR,
    ROLE_INSTITUTION,
    ROLE_RESEARCHER,
    ROLE_STATISTICIAN,
)

INSTITUTIONS = ("bank-a", "bank-b", "bank-c", "bank-d")

DEFAULT_RULE = {
    "weighting": "equal",
    "outlier": {"method": "iqr", "k": 1.5},
    "min_institutions": 3,
    "review_threshold": 0.25,
}


class ForecastCase(unittest.TestCase):
    """预置：4 家获准机构、1 轮开放调查、常用角色。"""

    INSTITUTIONS = INSTITUTIONS

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.clock = ManualClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.system = ForecastSystem(self.db_path, clock=self.clock)
        self.addCleanup(self.system.close)
        self.researcher = Actor("research-1", ROLE_RESEARCHER)
        self.statistician = Actor("stat-1", ROLE_STATISTICIAN)
        self.approver = Actor("appr-1", ROLE_APPROVER)
        self.auditor = Actor("audit-1", ROLE_AUDITOR)
        self._receipt_counter = 0
        for institution in INSTITUTIONS:
            self.system.register_institution(
                self.researcher, institution_id=institution, display_name=institution
            )
        self.system.open_round(
            self.statistician,
            round_id="R1",
            label="第1轮",
            opens_at="2026-09-28T00:00:00+00:00",
            closes_at="2026-10-05T00:00:00+00:00",
        )

    def actor(self, institution: str) -> Actor:
        return Actor(institution, ROLE_INSTITUTION)

    def receipt(self) -> str:
        self._receipt_counter += 1
        return f"receipt-{self._receipt_counter}"

    def submit(
        self,
        institution: str,
        value: float,
        *,
        receipt_id: str | None = None,
        round_id: str = "R1",
        indicator: str = "IPCA",
        target_year: int = 2026,
        rationale: str = "基线判断",
        **kwargs: object,
    ) -> dict:
        return self.system.submit_forecast(
            self.actor(institution),
            receipt_id=receipt_id or self.receipt(),
            institution_id=institution,
            indicator=indicator,
            target_year=target_year,
            round_id=round_id,
            value=value,
            rationale=rationale,
            **kwargs,
        )

    def fill_round(
        self,
        values: tuple[float, ...] = (4.8, 4.9, 5.0, 5.1),
        *,
        round_id: str = "R1",
        indicator: str = "IPCA",
        target_year: int = 2026,
    ) -> None:
        for institution, value in zip(INSTITUTIONS, values):
            self.submit(
                institution, value, round_id=round_id, indicator=indicator, target_year=target_year
            )

    def make_rule(
        self,
        definition: dict | None = None,
        *,
        rule_id: str = "rule-1",
        scope_indicator: str | None = None,
    ) -> dict:
        self.system.propose_rule(
            self.statistician,
            rule_id=rule_id,
            scope_indicator=scope_indicator,
            definition=definition or DEFAULT_RULE,
        )
        self.system.submit_rule_for_approval(self.statistician, rule_id=rule_id, version=1)
        return self.system.approve_rule(self.approver, rule_id=rule_id, version=1)

    def publish_round(
        self, *, round_id: str = "R1", indicator: str = "IPCA", target_year: int = 2026
    ) -> dict:
        publication = self.system.compute_publication(
            self.statistician, round_id=round_id, indicator=indicator, target_year=target_year
        )
        return self.system.publish(self.approver, publication_id=publication["publication_id"])
