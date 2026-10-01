"""测试共享夹具：可控时钟与一套已建好主数据的世界。"""

from __future__ import annotations

from src.macro_survey.service import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_AUDITOR,
    ROLE_CONTRIBUTOR,
    ROLE_PUBLISHER,
    ROLE_RESEARCH,
    ROLE_STATISTICIAN,
    SurveyService,
)
from src.macro_survey.store import Store


class MutableClock:
    def __init__(self, value: str = "2026-09-01T10:00:00+00:00") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class World:
    """标准角色/机构/指标/轮次的装配器。"""

    def __init__(self, db: str = ":memory:", clock: MutableClock | None = None) -> None:
        self.clock = clock or MutableClock()
        self.store = Store(db)
        self.svc = SurveyService(self.store, self.clock)
        self._build()

    def _build(self) -> None:
        self.admin = self.svc.create_user(
            None, "root", [ROLE_ADMIN, ROLE_RESEARCH], None
        )
        self.stat = self.svc.create_user(
            self.admin, "stat", [ROLE_STATISTICIAN], None
        )
        self.approver = self.svc.create_user(
            self.admin, "approver", [ROLE_APPROVER], None
        )
        self.publisher = self.svc.create_user(
            self.admin, "publisher", [ROLE_PUBLISHER], None
        )
        self.auditor = self.svc.create_user(
            self.admin, "auditor", [ROLE_AUDITOR], None
        )
        self.institutions = {}
        self.forecasters = {}
        for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
            inst = self.svc.create_institution(self.admin, name)
            self.institutions[name] = inst
            user = self.svc.create_user(
                self.admin, f"u_{name}", [ROLE_CONTRIBUTOR], inst["id"]
            )
            self.forecasters[name] = user
        self.ipca = self.svc.create_indicator(
            self.admin, "IPCA", "综合通胀指数", "%"
        )
        self.pib = self.svc.create_indicator(
            self.admin, "PIB", "国内生产总值增速", "%"
        )
        self.round = self.svc.create_round(
            self.admin, "2026-W40",
            "2026-09-28T09:00:00+00:00",
            "2026-10-02T12:00:00+00:00",
            "2026-10-05T18:00:00+00:00",
            [2026, 2027],
        )

    # ----- 便捷提交 -----

    def submit(self, name: str, value: float, *, code: str = "IPCA", year: int = 2026,
               round_label: str = "2026-W40", ref: str | None = None, confidence: float = 1.0,
               low: float | None = None, high: float | None = None,
               rationale: str = "基线预测") -> dict:
        ref = ref or f"{name}-{code}-{year}-1"
        return self.svc.submit_forecast(self.forecasters[name], {
            "round": round_label, "indicator": code, "forecast_year": year,
            "point_value": value, "low": low, "high": high, "confidence": confidence,
            "rationale": rationale, "client_ref": ref,
        })

    def revise(self, name: str, value: float, seq_ref: str, *, code: str = "IPCA",
               year: int = 2026, round_label: str = "2026-W40", confidence: float = 1.0,
               rationale: str = "因能源优惠结束而上调") -> dict:
        return self.svc.submit_forecast(self.forecasters[name], {
            "round": round_label, "indicator": code, "forecast_year": year,
            "point_value": value, "confidence": confidence,
            "rationale": rationale, "client_ref": seq_ref,
        })

    def withdraw(self, name: str, ref: str, *, code: str = "IPCA", year: int = 2026,
                 round_label: str = "2026-W40") -> dict:
        return self.svc.withdraw_forecast(self.forecasters[name], {
            "round": round_label, "indicator": code, "forecast_year": year,
            "client_ref": ref,
        })

    def close(self) -> None:
        self.store.close()
