"""宏观预测征集与发布后端的核心服务。

覆盖：机构准入、调查轮次、预测提交/修订/撤回、回执幂等与隔离、
共识规则双人审批、发布水位、匿名保护、误差/回测/复核标记、
提醒持久化与重启恢复、报告数字可解释性。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from . import consensus
from .clock import Clock, SystemClock, iso
from .models import (
    Actor,
    DomainError,
    RuleDefinition,
    canonical_json,
    payload_hash,
    pseudonym,
    PUB_PENDING,
    PUB_PUBLISHED,
    PUB_REJECTED,
    ROLE_APPROVER,
    ROLE_AUDITOR,
    ROLE_INSTITUTION,
    ROLE_RESEARCHER,
    ROLE_STATISTICIAN,
    ROUND_CLOSED,
    ROUND_OPEN,
    RULE_ACTIVE,
    RULE_DRAFT,
    RULE_PENDING,
    RULE_REJECTED,
    RULE_SUPERSEDED,
    SUBMISSION_ACTIVE,
    SUBMISSION_SUPERSEDED,
    SUBMISSION_WITHDRAWN,
)
from .storage import Store

# 提醒类型
REMINDER_ROUND_CLOSE = "round_close"
REMINDER_PUBLICATION_APPROVAL = "publication_approval"

# 复核标记状态
FLAG_OPEN = "open"
FLAG_RESOLVED = "resolved"

# 提醒状态
REMINDER_PENDING = "pending"
REMINDER_FIRED = "fired"
REMINDER_CANCELLED = "cancelled"

_PRIVILEGED_ROLES = (ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_AUDITOR, ROLE_RESEARCHER)


def _new_id() -> str:
    return uuid.uuid4().hex


class ForecastSystem:
    """领域服务门面。所有写操作经 SQLite 落盘，重启后状态完整。"""

    def __init__(
        self,
        db_path: str,
        *,
        clock: Clock | None = None,
        pseudonym_salt: str = "macro-forecast-vintage",
        approval_reminder_hours: int = 24,
    ) -> None:
        self.store = Store(db_path)
        self.clock = clock or SystemClock()
        self.salt = pseudonym_salt
        self.approval_reminder_hours = approval_reminder_hours

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return iso(self.clock.now())

    @staticmethod
    def _require(actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise DomainError(
                f"角色 {actor.role} 无权执行此操作", code="forbidden", http_status=403
            )

    def _event(self, kind: str, entity_id: str, actor: Actor | None, data: dict[str, Any]) -> None:
        self.store.insert(
            "events",
            {
                "kind": kind,
                "entity_id": entity_id,
                "at": self._now(),
                "actor": actor.actor_id if actor else None,
                "data_json": canonical_json(data),
            },
        )

    def _pseudonym(self, round_id: str, institution_id: str) -> str:
        return pseudonym(self.salt, round_id, institution_id)

    def _get_round(self, round_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM rounds WHERE round_id = ?", (round_id,))
        if row is None:
            raise DomainError(f"调查轮次不存在: {round_id}", code="not_found", http_status=404)
        return row

    def _get_institution(self, institution_id: str) -> dict[str, Any]:
        row = self.store.one(
            "SELECT * FROM institutions WHERE institution_id = ?", (institution_id,)
        )
        if row is None:
            raise DomainError(f"机构未获准参与: {institution_id}", code="forbidden", http_status=403)
        return row

    def _add_reminder(self, kind: str, due_at: str, payload: dict[str, Any]) -> str:
        reminder_id = _new_id()
        self.store.insert(
            "reminders",
            {
                "reminder_id": reminder_id,
                "kind": kind,
                "due_at": due_at,
                "payload_json": canonical_json(payload),
                "status": REMINDER_PENDING,
                "created_at": self._now(),
                "fired_at": None,
            },
        )
        return reminder_id

    def _cancel_reminders(self, kind: str, payload_key: str, payload_value: str) -> None:
        rows = self.store.query(
            "SELECT * FROM reminders WHERE kind = ? AND status = ?", (kind, REMINDER_PENDING)
        )
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get(payload_key) == payload_value:
                self.store.update(
                    "reminders",
                    {"status": REMINDER_CANCELLED, "fired_at": self._now()},
                    "reminder_id = ?",
                    (row["reminder_id"],),
                )

    # ------------------------------------------------------------------
    # 机构与调查轮次
    # ------------------------------------------------------------------
    def register_institution(
        self, actor: Actor, *, institution_id: str, display_name: str
    ) -> dict[str, Any]:
        """研究部门登记获准机构；重复登记同一编号返回原记录。"""
        self._require(actor, ROLE_RESEARCHER, ROLE_STATISTICIAN)
        existing = self.store.one(
            "SELECT * FROM institutions WHERE institution_id = ?", (institution_id,)
        )
        if existing is not None:
            return {"institution_id": institution_id, "display_name": existing["display_name"], "replayed": True}
        with self.store.transaction():
            self.store.insert(
                "institutions",
                {
                    "institution_id": institution_id,
                    "display_name": display_name,
                    "accredited": 1,
                    "created_at": self._now(),
                },
            )
            self._event("institution_registered", institution_id, actor, {"display_name": display_name})
        return {"institution_id": institution_id, "display_name": display_name, "replayed": False}

    def open_round(
        self,
        actor: Actor,
        *,
        round_id: str,
        label: str,
        opens_at: str,
        closes_at: str,
    ) -> dict[str, Any]:
        """开启一轮调查，并登记截止提醒（重启后由 recover 恢复）。"""
        self._require(actor, ROLE_STATISTICIAN, ROLE_RESEARCHER)
        if closes_at <= opens_at:
            raise DomainError("截止时间必须晚于开始时间", code="invalid_round")
        if self.store.one("SELECT round_id FROM rounds WHERE round_id = ?", (round_id,)):
            raise DomainError(f"调查轮次已存在: {round_id}", code="conflict", http_status=409)
        with self.store.transaction():
            self.store.insert(
                "rounds",
                {
                    "round_id": round_id,
                    "label": label,
                    "opens_at": opens_at,
                    "closes_at": closes_at,
                    "status": ROUND_OPEN,
                },
            )
            self._add_reminder(REMINDER_ROUND_CLOSE, closes_at, {"round_id": round_id})
            self._event("round_opened", round_id, actor, {"label": label, "closes_at": closes_at})
        return {"round_id": round_id, "status": ROUND_OPEN, "closes_at": closes_at}

    def close_round(self, actor: Actor, *, round_id: str) -> dict[str, Any]:
        self._require(actor, ROLE_STATISTICIAN, ROLE_RESEARCHER)
        self._get_round(round_id)
        with self.store.transaction():
            self.store.update(
                "rounds", {"status": ROUND_CLOSED}, "round_id = ?", (round_id,)
            )
            self._cancel_reminders(REMINDER_ROUND_CLOSE, "round_id", round_id)
            self._event("round_closed", round_id, actor, {})
        return {"round_id": round_id, "status": ROUND_CLOSED}

    # ------------------------------------------------------------------
    # 预测提交、修订与撤回
    # ------------------------------------------------------------------
    def _receipt_replay(self, receipt_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """同一回执重复到达返回原结果；编号相同内容不同进入隔离。"""
        row = self.store.one("SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,))
        if row is None:
            return None
        incoming = payload_hash(payload)
        if row["payload_hash"] == incoming:
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        with self.store.transaction():
            self.store.insert(
                "quarantine",
                {
                    "receipt_id": receipt_id,
                    "existing_hash": row["payload_hash"],
                    "incoming_hash": incoming,
                    "payload_json": canonical_json(payload),
                    "reason": "回执编号相同但内容不同",
                    "created_at": self._now(),
                },
            )
        raise DomainError(
            "回执编号与内容不匹配，已进入隔离",
            code="receipt_conflict",
            http_status=409,
            details={"receipt_id": receipt_id},
        )

    def submit_forecast(
        self,
        actor: Actor,
        *,
        receipt_id: str,
        institution_id: str,
        indicator: str,
        target_year: int,
        round_id: str,
        value: float,
        lower: float | None = None,
        upper: float | None = None,
        confidence: float | None = None,
        rationale: str,
    ) -> dict[str, Any]:
        """提交或修订预测。同一（机构,指标,年份,轮次）的再次提交构成新修订。"""
        self._require(actor, ROLE_INSTITUTION)
        if actor.actor_id != institution_id:
            raise DomainError(
                "只能提交本机构的预测", code="forbidden", http_status=403
            )
        payload = {
            "institution_id": institution_id,
            "indicator": indicator,
            "target_year": target_year,
            "round_id": round_id,
            "value": value,
            "lower": lower,
            "upper": upper,
            "confidence": confidence,
            "rationale": rationale,
        }
        replay = self._receipt_replay(receipt_id, payload)
        if replay is not None:
            return replay

        institution = self._get_institution(institution_id)
        if not institution["accredited"]:
            raise DomainError("机构资格已被暂停", code="forbidden", http_status=403)
        survey_round = self._get_round(round_id)
        if survey_round["status"] != ROUND_OPEN:
            raise DomainError("调查轮次已关闭，不再接收提交", code="round_closed", http_status=409)
        if self._now() > survey_round["closes_at"]:
            raise DomainError("调查轮次已过截止时间", code="round_closed", http_status=409)
        self._validate_forecast(value, lower, upper, confidence, rationale)

        with self.store.transaction():
            history = self.store.query(
                "SELECT * FROM submissions WHERE institution_id = ? AND indicator = ?"
                " AND target_year = ? AND round_id = ? ORDER BY revision",
                (institution_id, indicator, target_year, round_id),
            )
            revision = (history[-1]["revision"] + 1) if history else 1
            current = next(
                (row for row in history if row["status"] == SUBMISSION_ACTIVE), None
            )
            submission_id = _new_id()
            now = self._now()
            if current is not None:
                self.store.update(
                    "submissions",
                    {"status": SUBMISSION_SUPERSEDED},
                    "submission_id = ?",
                    (current["submission_id"],),
                )
            self.store.insert(
                "submissions",
                {
                    "submission_id": submission_id,
                    "institution_id": institution_id,
                    "indicator": indicator,
                    "target_year": target_year,
                    "round_id": round_id,
                    "value": value,
                    "lower": lower,
                    "upper": upper,
                    "confidence": confidence,
                    "rationale": rationale,
                    "revision": revision,
                    "supersedes": current["submission_id"] if current else None,
                    "status": SUBMISSION_ACTIVE,
                    "submitted_at": now,
                    "withdrawn_at": None,
                    "withdraw_reason": None,
                    "receipt_id": receipt_id,
                },
            )
            # 迟到修订：该键已有发布水位时，本次修订只影响后续版本
            watermark = self.store.one(
                "SELECT * FROM publications WHERE round_id = ? AND indicator = ?"
                " AND target_year = ? AND status = ? ORDER BY vintage DESC LIMIT 1",
                (round_id, indicator, target_year, PUB_PUBLISHED),
            )
            late = watermark is not None and now > watermark["cutoff_at"]
            response = {
                "status": "accepted",
                "submission_id": submission_id,
                "revision": revision,
                "late_revision": late,
                "round_id": round_id,
                "indicator": indicator,
                "target_year": target_year,
            }
            self.store.insert(
                "receipts",
                {
                    "receipt_id": receipt_id,
                    "payload_hash": payload_hash(payload),
                    "response_json": canonical_json(response),
                    "created_at": now,
                },
            )
            self._event(
                "forecast_submitted",
                submission_id,
                actor,
                {
                    "institution_id": institution_id,
                    "indicator": indicator,
                    "target_year": target_year,
                    "round_id": round_id,
                    "revision": revision,
                    "late_revision": late,
                },
            )
        return response

    @staticmethod
    def _validate_forecast(
        value: float,
        lower: float | None,
        upper: float | None,
        confidence: float | None,
        rationale: str,
    ) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DomainError("预测数值无效", code="invalid_forecast")
        if lower is not None and lower > value:
            raise DomainError("区间下限不能高于预测值", code="invalid_forecast")
        if upper is not None and upper < value:
            raise DomainError("区间上限不能低于预测值", code="invalid_forecast")
        if lower is not None and upper is not None and lower > upper:
            raise DomainError("区间下限不能高于上限", code="invalid_forecast")
        if confidence is not None and not (0 < confidence <= 1):
            raise DomainError("置信度必须位于 (0, 1]", code="invalid_forecast")
        if not isinstance(rationale, str) or not rationale.strip():
            raise DomainError("必须填写预测理由", code="invalid_forecast")

    def withdraw_forecast(
        self,
        actor: Actor,
        *,
        institution_id: str,
        indicator: str,
        target_year: int,
        round_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """撤回当前生效预测。历史发布快照不受影响，参与共识的事实保留。"""
        self._require(actor, ROLE_INSTITUTION)
        if actor.actor_id != institution_id:
            raise DomainError("只能撤回本机构的预测", code="forbidden", http_status=403)
        if not reason or not reason.strip():
            raise DomainError("必须填写撤回理由", code="invalid_request")
        active = self.store.one(
            "SELECT * FROM submissions WHERE institution_id = ? AND indicator = ?"
            " AND target_year = ? AND round_id = ? AND status = ?",
            (institution_id, indicator, target_year, round_id, SUBMISSION_ACTIVE),
        )
        if active is None:
            withdrawn = self.store.one(
                "SELECT * FROM submissions WHERE institution_id = ? AND indicator = ?"
                " AND target_year = ? AND round_id = ? AND status = ?",
                (institution_id, indicator, target_year, round_id, SUBMISSION_WITHDRAWN),
            )
            if withdrawn is not None:
                return {"status": "already_withdrawn", "submission_id": withdrawn["submission_id"]}
            raise DomainError("没有可撤回的生效预测", code="not_found", http_status=404)
        now = self._now()
        with self.store.transaction():
            self.store.update(
                "submissions",
                {"status": SUBMISSION_WITHDRAWN, "withdrawn_at": now, "withdraw_reason": reason},
                "submission_id = ?",
                (active["submission_id"],),
            )
            self._event(
                "forecast_withdrawn",
                active["submission_id"],
                actor,
                {"institution_id": institution_id, "reason": reason},
            )
        return {"status": "withdrawn", "submission_id": active["submission_id"], "withdrawn_at": now}

    # ------------------------------------------------------------------
    # 提交查询（匿名保护）
    # ------------------------------------------------------------------
    def list_own_submissions(
        self, actor: Actor, *, institution_id: str, round_id: str | None = None
    ) -> list[dict[str, Any]]:
        """机构只能查看本机构提交；审计与研究角色可代查。"""
        if actor.role == ROLE_INSTITUTION and actor.actor_id != institution_id:
            raise DomainError(
                "不能查看其他机构的提交", code="forbidden", http_status=403
            )
        if actor.role not in (ROLE_INSTITUTION, ROLE_AUDITOR, ROLE_RESEARCHER):
            raise DomainError("角色无权查看提交明细", code="forbidden", http_status=403)
        sql = "SELECT * FROM submissions WHERE institution_id = ?"
        params: list[Any] = [institution_id]
        if round_id is not None:
            sql += " AND round_id = ?"
            params.append(round_id)
        sql += " ORDER BY indicator, target_year, revision"
        return self.store.query(sql, tuple(params))

    def revision_history(
        self,
        actor: Actor,
        *,
        institution_id: str,
        indicator: str,
        target_year: int,
        round_id: str,
    ) -> list[dict[str, Any]]:
        """某机构的完整修订链：何时改过判断、理由是什么。仅本人/审计/研究可见。"""
        if actor.role == ROLE_INSTITUTION and actor.actor_id != institution_id:
            raise DomainError(
                "不能查看其他机构的修订历史", code="forbidden", http_status=403
            )
        if actor.role not in (ROLE_INSTITUTION, ROLE_AUDITOR, ROLE_RESEARCHER):
            raise DomainError("角色无权查看修订历史", code="forbidden", http_status=403)
        rows = self.store.query(
            "SELECT * FROM submissions WHERE institution_id = ? AND indicator = ?"
            " AND target_year = ? AND round_id = ? ORDER BY revision",
            (institution_id, indicator, target_year, round_id),
        )
        return [
            {
                "revision": row["revision"],
                "value": row["value"],
                "lower": row["lower"],
                "upper": row["upper"],
                "confidence": row["confidence"],
                "rationale": row["rationale"],
                "status": row["status"],
                "submitted_at": row["submitted_at"],
                "withdrawn_at": row["withdrawn_at"],
                "withdraw_reason": row["withdraw_reason"],
            }
            for row in rows
        ]

    def list_round_samples(
        self, actor: Actor, *, round_id: str, indicator: str, target_year: int
    ) -> list[dict[str, Any]]:
        """统计视角的当前生效样本：匿名代号 + 数值与理由，不含真实身份。"""
        self._require(actor, ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_AUDITOR, ROLE_RESEARCHER)
        self._get_round(round_id)
        samples, _ = self._effective_samples(round_id, indicator, target_year, self._now())
        masked = actor.role not in (ROLE_AUDITOR, ROLE_RESEARCHER)
        result = []
        for sample in samples:
            entry = {
                "contributor": self._pseudonym(round_id, sample["institution_id"]),
                "revision": sample["revision"],
                "value": sample["value"],
                "confidence": sample["confidence"],
                "rationale": sample["rationale"],
                "submitted_at": sample["submitted_at"],
                "excluded": sample["excluded_reason"] is not None,
                "excluded_reason": sample["excluded_reason"],
            }
            if not masked:
                entry["institution_id"] = sample["institution_id"]
            result.append(entry)
        return result

    def consensus_snapshot(
        self, actor: Actor, *, round_id: str, indicator: str, target_year: int
    ) -> dict[str, Any]:
        """实时共识聚合。样本不足匿名下限时拒绝，防止反推个体。"""
        rule_row = self._active_rule(indicator)
        if rule_row is None:
            raise DomainError("没有生效的共识规则", code="no_active_rule", http_status=409)
        rule = RuleDefinition.parse(json.loads(rule_row["definition_json"]))
        samples, _ = self._effective_samples(round_id, indicator, target_year, self._now())
        candidates = [s for s in samples if s["excluded_reason"] is None]
        institutions = {s["institution_id"] for s in candidates}
        if len(institutions) < rule.min_institutions:
            raise DomainError(
                "匿名保护：有效机构数不足，拒绝提供聚合",
                code="anonymity_guard",
                http_status=422,
                details={"min_institutions": rule.min_institutions},
            )
        result = consensus.compute_consensus(
            [{"key": s["institution_id"], "value": s["value"], "confidence": s["confidence"]} for s in candidates],
            rule,
            self._accuracy_mae(indicator),
        )
        return {
            "round_id": round_id,
            "indicator": indicator,
            "target_year": target_year,
            "median": result["median"],
            "weighted_mean": result["weighted_mean"],
            "contributors": len(institutions),
            "computed_at": self._now(),
        }

    # ------------------------------------------------------------------
    # 共识规则：统计人员定义，审批角色批准（四眼原则）
    # ------------------------------------------------------------------
    def propose_rule(
        self,
        actor: Actor,
        *,
        rule_id: str,
        scope_indicator: str | None = None,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        self._require(actor, ROLE_STATISTICIAN)
        parsed = RuleDefinition.parse(definition)
        row = self.store.one(
            "SELECT MAX(version) AS max_version FROM rules WHERE rule_id = ?", (rule_id,)
        )
        version = (row["max_version"] or 0) + 1
        with self.store.transaction():
            self.store.insert(
                "rules",
                {
                    "rule_id": rule_id,
                    "version": version,
                    "scope_indicator": scope_indicator,
                    "definition_json": canonical_json(parsed.to_dict()),
                    "status": RULE_DRAFT,
                    "created_by": actor.actor_id,
                    "created_at": self._now(),
                    "submitted_at": None,
                    "approved_by": None,
                    "approved_at": None,
                },
            )
            self._event("rule_proposed", rule_id, actor, {"version": version})
        return {"rule_id": rule_id, "version": version, "status": RULE_DRAFT}

    def submit_rule_for_approval(self, actor: Actor, *, rule_id: str, version: int) -> dict[str, Any]:
        self._require(actor, ROLE_STATISTICIAN)
        rule = self._get_rule(rule_id, version)
        if rule["status"] != RULE_DRAFT:
            raise DomainError("只有草稿规则可以送审", code="invalid_state", http_status=409)
        with self.store.transaction():
            self.store.update(
                "rules",
                {"status": RULE_PENDING, "submitted_at": self._now()},
                "rule_id = ? AND version = ?",
                (rule_id, version),
            )
            self._event("rule_submitted", rule_id, actor, {"version": version})
        return {"rule_id": rule_id, "version": version, "status": RULE_PENDING}

    def approve_rule(self, actor: Actor, *, rule_id: str, version: int) -> dict[str, Any]:
        self._require(actor, ROLE_APPROVER)
        rule = self._get_rule(rule_id, version)
        if rule["status"] != RULE_PENDING:
            raise DomainError("只有待审批规则可以批准", code="invalid_state", http_status=409)
        if rule["created_by"] == actor.actor_id:
            raise DomainError(
                "规则定义者不能批准自己的规则", code="four_eyes", http_status=403
            )
        now = self._now()
        with self.store.transaction():
            # 同一作用域只保留一条生效规则，旧规则保留供血缘追溯
            if rule["scope_indicator"] is None:
                self.store.update(
                    "rules",
                    {"status": RULE_SUPERSEDED},
                    "scope_indicator IS NULL AND status = ?",
                    (RULE_ACTIVE,),
                )
            else:
                self.store.update(
                    "rules",
                    {"status": RULE_SUPERSEDED},
                    "scope_indicator = ? AND status = ?",
                    (rule["scope_indicator"], RULE_ACTIVE),
                )
            self.store.update(
                "rules",
                {"status": RULE_ACTIVE, "approved_by": actor.actor_id, "approved_at": now},
                "rule_id = ? AND version = ?",
                (rule_id, version),
            )
            self._event("rule_approved", rule_id, actor, {"version": version})
        return {"rule_id": rule_id, "version": version, "status": RULE_ACTIVE}

    def reject_rule(self, actor: Actor, *, rule_id: str, version: int, reason: str) -> dict[str, Any]:
        self._require(actor, ROLE_APPROVER)
        rule = self._get_rule(rule_id, version)
        if rule["status"] != RULE_PENDING:
            raise DomainError("只有待审批规则可以驳回", code="invalid_state", http_status=409)
        with self.store.transaction():
            self.store.update(
                "rules",
                {"status": RULE_REJECTED},
                "rule_id = ? AND version = ?",
                (rule_id, version),
            )
            self._event("rule_rejected", rule_id, actor, {"version": version, "reason": reason})
        return {"rule_id": rule_id, "version": version, "status": RULE_REJECTED}

    def _get_rule(self, rule_id: str, version: int) -> dict[str, Any]:
        row = self.store.one(
            "SELECT * FROM rules WHERE rule_id = ? AND version = ?", (rule_id, version)
        )
        if row is None:
            raise DomainError(
                f"规则不存在: {rule_id} v{version}", code="not_found", http_status=404
            )
        return row

    def _active_rule(self, indicator: str) -> dict[str, Any] | None:
        """指标专属规则优先，其次全局规则；同作用域取最高版本。"""
        row = self.store.one(
            "SELECT * FROM rules WHERE scope_indicator = ? AND status = ?"
            " ORDER BY version DESC LIMIT 1",
            (indicator, RULE_ACTIVE),
        )
        if row is not None:
            return row
        return self.store.one(
            "SELECT * FROM rules WHERE scope_indicator IS NULL AND status = ?"
            " ORDER BY version DESC LIMIT 1",
            (RULE_ACTIVE,),
        )

    # ------------------------------------------------------------------
    # 发布：计算 → 审批 → 水位冻结
    # ------------------------------------------------------------------
    def _effective_samples(
        self, round_id: str, indicator: str, target_year: int, cutoff: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """截止时刻每家机构的生效修订及其完整修订链。

        返回 (samples, chain)。samples 中 withdrawn 的样本带 excluded_reason；
        chain 记录全部修订顺序，供解释报告数字使用。
        """
        rows = self.store.query(
            "SELECT * FROM submissions WHERE round_id = ? AND indicator = ? AND target_year = ?"
            " ORDER BY institution_id, revision",
            (round_id, indicator, target_year),
        )
        by_institution: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_institution.setdefault(row["institution_id"], []).append(row)

        samples: list[dict[str, Any]] = []
        chain: list[dict[str, Any]] = []
        for institution_id, revisions in by_institution.items():
            before_cutoff = [r for r in revisions if r["submitted_at"] <= cutoff]
            effective = before_cutoff[-1] if before_cutoff else None
            for row in revisions:
                if effective is not None and row["revision"] == effective["revision"]:
                    state = "effective"
                elif row["submitted_at"] > cutoff:
                    state = "after_cutoff"
                else:
                    state = "superseded"
                chain.append(
                    {
                        "institution_id": institution_id,
                        "revision": row["revision"],
                        "value": row["value"],
                        "submitted_at": row["submitted_at"],
                        "rationale": row["rationale"],
                        "state": state,
                    }
                )
            if effective is None:
                continue
            excluded_reason = None
            if (
                effective["status"] == SUBMISSION_WITHDRAWN
                and effective["withdrawn_at"] is not None
                and effective["withdrawn_at"] <= cutoff
            ):
                excluded_reason = "withdrawn"
            samples.append(
                {
                    "institution_id": institution_id,
                    "submission_id": effective["submission_id"],
                    "revision": effective["revision"],
                    "value": effective["value"],
                    "confidence": effective["confidence"],
                    "rationale": effective["rationale"],
                    "submitted_at": effective["submitted_at"],
                    "excluded_reason": excluded_reason,
                }
            )
        return samples, chain

    def _accuracy_mae(self, indicator: str) -> dict[str, float]:
        """机构在该指标上的历史平均绝对误差，供精度加权使用。"""
        rows = self.store.query(
            "SELECT subject, AVG(ABS(value)) AS mae FROM errors"
            " WHERE scope = 'institution' AND metric = 'signed_error' AND indicator = ?"
            " GROUP BY subject",
            (indicator,),
        )
        return {row["subject"]: row["mae"] for row in rows}

    def compute_publication(
        self, actor: Actor, *, round_id: str, indicator: str, target_year: int
    ) -> dict[str, Any]:
        """按生效规则计算发布稿（待审批）。快照一旦生成不再修改。"""
        self._require(actor, ROLE_STATISTICIAN)
        self._get_round(round_id)
        rule_row = self._active_rule(indicator)
        if rule_row is None:
            raise DomainError("没有生效的共识规则", code="no_active_rule", http_status=409)
        rule = RuleDefinition.parse(json.loads(rule_row["definition_json"]))
        cutoff = self._now()
        samples, chain = self._effective_samples(round_id, indicator, target_year, cutoff)
        candidates = [s for s in samples if s["excluded_reason"] is None]
        institutions = {s["institution_id"] for s in candidates}
        if len(institutions) < rule.min_institutions:
            raise DomainError(
                "有效机构数不足匿名下限，无法生成发布稿",
                code="anonymity_guard",
                http_status=422,
                details={"min_institutions": rule.min_institutions},
            )
        result = consensus.compute_consensus(
            [{"key": s["institution_id"], "value": s["value"], "confidence": s["confidence"]} for s in candidates],
            rule,
            self._accuracy_mae(indicator),
        )
        computed = {
            "median": result["median"],
            "weighted_mean": result["weighted_mean"],
            "n_candidates": result["n_candidates"],
            "n_included": result["n_included"],
            "n_outliers": result["n_outliers"],
            "fences": result["fences"],
            "cutoff_at": cutoff,
        }
        weight_by_key = {s["key"]: s for s in result["samples"]}
        contributors = []
        for sample in samples:
            stats = weight_by_key.get(sample["institution_id"])
            excluded_reason = sample["excluded_reason"]
            if excluded_reason is None and stats is not None and stats["outlier"]:
                excluded_reason = "outlier"
            contributors.append(
                {
                    "institution_id": sample["institution_id"],
                    "pseudonym": self._pseudonym(round_id, sample["institution_id"]),
                    "submission_id": sample["submission_id"],
                    "revision": sample["revision"],
                    "value": sample["value"],
                    "confidence": sample["confidence"],
                    "rationale": sample["rationale"],
                    "submitted_at": sample["submitted_at"],
                    "included": excluded_reason is None,
                    "excluded_reason": excluded_reason,
                    "weight": stats["weight"] if stats is not None and excluded_reason is None else 0.0,
                }
            )
        sample_snapshot = {
            "contributors": contributors,
            "revision_chain": [
                {**entry, "pseudonym": self._pseudonym(round_id, entry["institution_id"])}
                for entry in chain
            ],
        }
        row = self.store.one(
            "SELECT MAX(vintage) AS max_vintage FROM publications"
            " WHERE round_id = ? AND indicator = ? AND target_year = ?",
            (round_id, indicator, target_year),
        )
        vintage = (row["max_vintage"] or 0) + 1
        publication_id = _new_id()
        with self.store.transaction():
            self.store.insert(
                "publications",
                {
                    "publication_id": publication_id,
                    "round_id": round_id,
                    "indicator": indicator,
                    "target_year": target_year,
                    "vintage": vintage,
                    "rule_id": rule_row["rule_id"],
                    "rule_version": rule_row["version"],
                    "status": PUB_PENDING,
                    "cutoff_at": cutoff,
                    "computed_json": canonical_json(computed),
                    "sample_json": canonical_json(sample_snapshot),
                    "created_by": actor.actor_id,
                    "created_at": self._now(),
                    "approved_by": None,
                    "published_at": None,
                },
            )
            due = iso(self.clock.now() + timedelta(hours=self.approval_reminder_hours))
            self._add_reminder(
                REMINDER_PUBLICATION_APPROVAL, due, {"publication_id": publication_id}
            )
            self._event(
                "publication_computed",
                publication_id,
                actor,
                {"round_id": round_id, "indicator": indicator, "target_year": target_year, "vintage": vintage},
            )
        return self.get_publication(actor, publication_id=publication_id)

    def publish(self, actor: Actor, *, publication_id: str) -> dict[str, Any]:
        """审批角色发布。发布后水位冻结，迟到修订不得覆盖。"""
        self._require(actor, ROLE_APPROVER)
        publication = self._get_publication_row(publication_id)
        if publication["status"] != PUB_PENDING:
            raise DomainError("只有待审批的发布稿可以发布", code="invalid_state", http_status=409)
        if publication["created_by"] == actor.actor_id:
            raise DomainError(
                "计算者不能批准自己的发布稿", code="four_eyes", http_status=403
            )
        now = self._now()
        with self.store.transaction():
            self.store.update(
                "publications",
                {"status": PUB_PUBLISHED, "approved_by": actor.actor_id, "published_at": now},
                "publication_id = ?",
                (publication_id,),
            )
            self._cancel_reminders(REMINDER_PUBLICATION_APPROVAL, "publication_id", publication_id)
            self._event("publication_published", publication_id, actor, {})
        return self.get_publication(actor, publication_id=publication_id)

    def reject_publication(
        self, actor: Actor, *, publication_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor, ROLE_APPROVER)
        publication = self._get_publication_row(publication_id)
        if publication["status"] != PUB_PENDING:
            raise DomainError("只有待审批的发布稿可以驳回", code="invalid_state", http_status=409)
        with self.store.transaction():
            self.store.update(
                "publications",
                {"status": PUB_REJECTED},
                "publication_id = ?",
                (publication_id,),
            )
            self._cancel_reminders(REMINDER_PUBLICATION_APPROVAL, "publication_id", publication_id)
            self._event("publication_rejected", publication_id, actor, {"reason": reason})
        return {"publication_id": publication_id, "status": PUB_REJECTED}

    def _get_publication_row(self, publication_id: str) -> dict[str, Any]:
        row = self.store.one(
            "SELECT * FROM publications WHERE publication_id = ?", (publication_id,)
        )
        if row is None:
            raise DomainError(
                f"发布不存在: {publication_id}", code="not_found", http_status=404
            )
        return row

    def _mask_contributors(
        self, actor: Actor, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """按角色脱敏：机构角色不见样本，统计/审批只见匿名代号。"""
        if actor.role in (ROLE_AUDITOR, ROLE_RESEARCHER):
            return snapshot
        masked = {"contributors": [], "revision_chain": []}
        for entry in snapshot["contributors"]:
            masked["contributors"].append(
                {key: value for key, value in entry.items() if key != "institution_id"}
            )
        for entry in snapshot["revision_chain"]:
            masked["revision_chain"].append(
                {key: value for key, value in entry.items() if key != "institution_id"}
            )
        return masked

    def get_publication(self, actor: Actor, *, publication_id: str) -> dict[str, Any]:
        row = self._get_publication_row(publication_id)
        computed = json.loads(row["computed_json"])
        base = {
            "publication_id": row["publication_id"],
            "round_id": row["round_id"],
            "indicator": row["indicator"],
            "target_year": row["target_year"],
            "vintage": row["vintage"],
            "rule_id": row["rule_id"],
            "rule_version": row["rule_version"],
            "status": row["status"],
            "computed": computed,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "approved_by": row["approved_by"],
            "published_at": row["published_at"],
        }
        if actor.role == ROLE_INSTITUTION:
            return base
        base["samples"] = self._mask_contributors(actor, json.loads(row["sample_json"]))
        return base

    def explain_publication(self, actor: Actor, *, publication_id: str) -> dict[str, Any]:
        """解释报告数字：采用了哪些有效样本、修订顺序、规则版本与水位。"""
        self._require(actor, *_PRIVILEGED_ROLES)
        row = self._get_publication_row(publication_id)
        rule = self._get_rule(row["rule_id"], row["rule_version"])
        snapshot = self._mask_contributors(actor, json.loads(row["sample_json"]))
        errors = self.list_errors(actor, publication_id=publication_id)
        return {
            "publication_id": row["publication_id"],
            "status": row["status"],
            "round_id": row["round_id"],
            "indicator": row["indicator"],
            "target_year": row["target_year"],
            "vintage": row["vintage"],
            "watermark": {
                "cutoff_at": row["cutoff_at"],
                "published_at": row["published_at"],
                "median": json.loads(row["computed_json"])["median"],
                "weighted_mean": json.loads(row["computed_json"])["weighted_mean"],
            },
            "rule": {
                "rule_id": rule["rule_id"],
                "version": rule["version"],
                "definition": json.loads(rule["definition_json"]),
                "created_by": rule["created_by"],
                "approved_by": rule["approved_by"],
            },
            "computed": json.loads(row["computed_json"]),
            "contributors": snapshot["contributors"],
            "revision_chain": snapshot["revision_chain"],
            "errors": errors,
        }

    def watermark(
        self, actor: Actor, *, round_id: str, indicator: str, target_year: int
    ) -> dict[str, Any]:
        """当前发布水位与尚未纳入的迟到修订数量。"""
        self._require(actor, *_PRIVILEGED_ROLES)
        latest = self.store.one(
            "SELECT * FROM publications WHERE round_id = ? AND indicator = ?"
            " AND target_year = ? AND status = ? ORDER BY vintage DESC LIMIT 1",
            (round_id, indicator, target_year, PUB_PUBLISHED),
        )
        if latest is None:
            return {"published": False, "unincorporated_revisions": 0}
        late = self.store.query(
            "SELECT COUNT(*) AS n FROM submissions WHERE round_id = ? AND indicator = ?"
            " AND target_year = ? AND submitted_at > ?",
            (round_id, indicator, target_year, latest["cutoff_at"]),
        )
        computed = json.loads(latest["computed_json"])
        return {
            "published": True,
            "publication_id": latest["publication_id"],
            "vintage": latest["vintage"],
            "median": computed["median"],
            "weighted_mean": computed["weighted_mean"],
            "cutoff_at": latest["cutoff_at"],
            "published_at": latest["published_at"],
            "unincorporated_revisions": late[0]["n"],
        }

    # ------------------------------------------------------------------
    # 实际值、误差、回测与复核标记
    # ------------------------------------------------------------------
    def record_actual(
        self, actor: Actor, *, indicator: str, target_year: int, value: float
    ) -> dict[str, Any]:
        """登记外部实际值，保存共识与机构两级误差，并生成复核标记。"""
        self._require(actor, ROLE_STATISTICIAN)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DomainError("实际值无效", code="invalid_actual")
        if self.store.one(
            "SELECT indicator FROM actuals WHERE indicator = ? AND target_year = ?",
            (indicator, target_year),
        ):
            raise DomainError(
                "该指标年份的实际值已登记", code="conflict", http_status=409
            )
        now = self._now()
        errors_recorded = 0
        flags_created = 0
        with self.store.transaction():
            self.store.insert(
                "actuals",
                {
                    "indicator": indicator,
                    "target_year": target_year,
                    "value": value,
                    "published_at": now,
                    "recorded_by": actor.actor_id,
                },
            )
            publications = self.store.query(
                "SELECT * FROM publications WHERE indicator = ? AND target_year = ? AND status = ?"
                " ORDER BY vintage",
                (indicator, target_year, PUB_PUBLISHED),
            )
            for publication in publications:
                computed = json.loads(publication["computed_json"])
                rule = RuleDefinition.parse(
                    json.loads(self._get_rule(publication["rule_id"], publication["rule_version"])["definition_json"])
                )
                for metric, forecast in (
                    ("median_error", computed["median"]),
                    ("weighted_mean_error", computed["weighted_mean"]),
                ):
                    self.store.insert(
                        "errors",
                        {
                            "publication_id": publication["publication_id"],
                            "indicator": indicator,
                            "target_year": target_year,
                            "scope": "consensus",
                            "subject": publication["publication_id"],
                            "metric": metric,
                            "value": forecast - value,
                            "computed_at": now,
                        },
                    )
                    errors_recorded += 1
                if abs(computed["median"] - value) > rule.review_threshold:
                    self._add_review_flag(
                        indicator,
                        target_year,
                        publication["round_id"],
                        "共识偏离超过复核阈值",
                        {
                            "publication_id": publication["publication_id"],
                            "median": computed["median"],
                            "actual": value,
                            "error": computed["median"] - value,
                            "threshold": rule.review_threshold,
                        },
                    )
                    flags_created += 1
                snapshot = json.loads(publication["sample_json"])
                for contributor in snapshot["contributors"]:
                    if not contributor["included"]:
                        continue
                    error = contributor["value"] - value
                    self.store.insert(
                        "errors",
                        {
                            "publication_id": publication["publication_id"],
                            "indicator": indicator,
                            "target_year": target_year,
                            "scope": "institution",
                            "subject": contributor["institution_id"],
                            "metric": "signed_error",
                            "value": error,
                            "computed_at": now,
                        },
                    )
                    errors_recorded += 1
                    if abs(error) > rule.review_threshold:
                        self._add_review_flag(
                            indicator,
                            target_year,
                            publication["round_id"],
                            "机构预测偏离超过复核阈值",
                            {
                                "publication_id": publication["publication_id"],
                                "institution_id": contributor["institution_id"],
                                "forecast": contributor["value"],
                                "actual": value,
                                "error": error,
                                "threshold": rule.review_threshold,
                            },
                        )
                        flags_created += 1
            self._event(
                "actual_recorded",
                f"{indicator}:{target_year}",
                actor,
                {"value": value, "publications_evaluated": len(publications)},
            )
        return {
            "indicator": indicator,
            "target_year": target_year,
            "actual": value,
            "publications_evaluated": len(publications),
            "errors_recorded": errors_recorded,
            "flags_created": flags_created,
        }

    def _add_review_flag(
        self,
        indicator: str,
        target_year: int,
        round_id: str | None,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        self.store.insert(
            "review_flags",
            {
                "indicator": indicator,
                "target_year": target_year,
                "round_id": round_id,
                "reason": reason,
                "detail_json": canonical_json(detail),
                "status": FLAG_OPEN,
                "created_at": self._now(),
                "resolved_at": None,
                "resolved_by": None,
                "resolution_note": None,
            },
        )

    def list_errors(
        self,
        actor: Actor,
        *,
        publication_id: str | None = None,
        indicator: str | None = None,
    ) -> list[dict[str, Any]]:
        """误差查询。机构只见共识误差与自身误差；统计视角的机构身份脱敏。"""
        sql = "SELECT * FROM errors WHERE 1 = 1"
        params: list[Any] = []
        if publication_id is not None:
            sql += " AND publication_id = ?"
            params.append(publication_id)
        if indicator is not None:
            sql += " AND indicator = ?"
            params.append(indicator)
        sql += " ORDER BY id"
        rows = self.store.query(sql, tuple(params))
        publication_rounds = {
            row["publication_id"]: row["round_id"]
            for row in self.store.query("SELECT publication_id, round_id FROM publications")
        }
        result = []
        for row in rows:
            if actor.role == ROLE_INSTITUTION:
                if row["scope"] == "institution" and row["subject"] != actor.actor_id:
                    continue
                result.append(dict(row))
                continue
            entry = dict(row)
            if (
                row["scope"] == "institution"
                and actor.role not in (ROLE_AUDITOR, ROLE_RESEARCHER)
            ):
                round_id = publication_rounds.get(row["publication_id"], "")
                entry["subject"] = self._pseudonym(round_id, row["subject"])
            result.append(entry)
        return result

    def run_backtest(
        self,
        actor: Actor,
        *,
        rule_id: str,
        rule_version: int,
        indicator: str,
        target_year: int,
    ) -> dict[str, Any]:
        """用候选规则在历史轮次上回测：重算共识并与实际值比较。"""
        self._require(actor, ROLE_STATISTICIAN)
        rule_row = self._get_rule(rule_id, rule_version)
        rule = RuleDefinition.parse(json.loads(rule_row["definition_json"]))
        actual = self.store.one(
            "SELECT * FROM actuals WHERE indicator = ? AND target_year = ?",
            (indicator, target_year),
        )
        if actual is None:
            raise DomainError(
                "该指标年份尚无实际值，无法回测", code="no_actual", http_status=409
            )
        publications = self.store.query(
            "SELECT * FROM publications WHERE indicator = ? AND target_year = ? AND status = ?"
            " ORDER BY vintage",
            (indicator, target_year, PUB_PUBLISHED),
        )
        if not publications:
            raise DomainError("没有已发布的历史水位可供回测", code="no_data", http_status=409)
        rounds_result = []
        for publication in publications:
            samples, _ = self._effective_samples(
                publication["round_id"], indicator, target_year, publication["cutoff_at"]
            )
            candidates = [s for s in samples if s["excluded_reason"] is None]
            if len({s["institution_id"] for s in candidates}) < rule.min_institutions:
                continue
            result = consensus.compute_consensus(
                [{"key": s["institution_id"], "value": s["value"], "confidence": s["confidence"]} for s in candidates],
                rule,
                self._accuracy_mae(indicator),
            )
            rounds_result.append(
                {
                    "round_id": publication["round_id"],
                    "cutoff_at": publication["cutoff_at"],
                    "median": result["median"],
                    "weighted_mean": result["weighted_mean"],
                    "median_error": result["median"] - actual["value"],
                    "weighted_mean_error": result["weighted_mean"] - actual["value"],
                    "n_included": result["n_included"],
                }
            )
        if not rounds_result:
            raise DomainError("历史样本不足以回测该规则", code="no_data", http_status=409)
        summary = {
            "rounds": rounds_result,
            "rounds_evaluated": len(rounds_result),
            "mae_median": sum(abs(r["median_error"]) for r in rounds_result) / len(rounds_result),
            "mae_weighted_mean": sum(abs(r["weighted_mean_error"]) for r in rounds_result)
            / len(rounds_result),
            "max_abs_median_error": max(abs(r["median_error"]) for r in rounds_result),
        }
        backtest_id = _new_id()
        with self.store.transaction():
            self.store.insert(
                "backtests",
                {
                    "backtest_id": backtest_id,
                    "rule_id": rule_id,
                    "rule_version": rule_version,
                    "indicator": indicator,
                    "target_year": target_year,
                    "result_json": canonical_json(summary),
                    "created_by": actor.actor_id,
                    "created_at": self._now(),
                },
            )
            self._event(
                "backtest_run",
                backtest_id,
                actor,
                {"rule_id": rule_id, "rule_version": rule_version, "indicator": indicator},
            )
        return {"backtest_id": backtest_id, **summary}

    def list_review_flags(
        self, actor: Actor, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        """下一轮需要复核的偏离。机构只见与自身相关的标记。"""
        sql = "SELECT * FROM review_flags"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        rows = self.store.query(sql, params)
        result = []
        for row in rows:
            detail = json.loads(row["detail_json"])
            owner = detail.get("institution_id")
            if actor.role == ROLE_INSTITUTION and owner not in (None, actor.actor_id):
                continue
            entry = dict(row)
            entry["detail"] = detail
            del entry["detail_json"]
            if owner is not None and actor.role not in (ROLE_AUDITOR, ROLE_RESEARCHER, ROLE_INSTITUTION):
                round_id = row["round_id"] or ""
                entry["detail"] = {
                    **detail,
                    "institution_id": self._pseudonym(round_id, owner),
                }
            result.append(entry)
        return result

    def resolve_review_flag(
        self, actor: Actor, *, flag_id: int, note: str
    ) -> dict[str, Any]:
        self._require(actor, ROLE_STATISTICIAN, ROLE_RESEARCHER)
        row = self.store.one("SELECT * FROM review_flags WHERE id = ?", (flag_id,))
        if row is None:
            raise DomainError(f"复核标记不存在: {flag_id}", code="not_found", http_status=404)
        if row["status"] != FLAG_OPEN:
            raise DomainError("复核标记已处理", code="invalid_state", http_status=409)
        with self.store.transaction():
            self.store.update(
                "review_flags",
                {
                    "status": FLAG_RESOLVED,
                    "resolved_at": self._now(),
                    "resolved_by": actor.actor_id,
                    "resolution_note": note,
                },
                "id = ?",
                (flag_id,),
            )
            self._event("review_flag_resolved", str(flag_id), actor, {"note": note})
        return {"id": flag_id, "status": FLAG_RESOLVED}

    # ------------------------------------------------------------------
    # 提醒与重启恢复
    # ------------------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        """进程重启后调用：触发已到期提醒，并报告待批准事项。"""
        fired = self._fire_due_reminders()
        return {"fired_reminders": fired, "pending_approvals": self.pending_approvals()}

    def _fire_due_reminders(self) -> list[dict[str, Any]]:
        now = self._now()
        due = self.store.query(
            "SELECT * FROM reminders WHERE status = ? AND due_at <= ? ORDER BY due_at",
            (REMINDER_PENDING, now),
        )
        fired = []
        with self.store.transaction():
            for row in due:
                self.store.update(
                    "reminders",
                    {"status": REMINDER_FIRED, "fired_at": now},
                    "reminder_id = ?",
                    (row["reminder_id"],),
                )
                fired.append(
                    {
                        "reminder_id": row["reminder_id"],
                        "kind": row["kind"],
                        "due_at": row["due_at"],
                        "payload": json.loads(row["payload_json"]),
                    }
                )
        return fired

    def due_reminders(self, actor: Actor) -> list[dict[str, Any]]:
        """当前已到期但尚未触发的提醒（不改变状态）。"""
        self._require(actor, *_PRIVILEGED_ROLES)
        rows = self.store.query(
            "SELECT * FROM reminders WHERE status = ? AND due_at <= ? ORDER BY due_at",
            (REMINDER_PENDING, self._now()),
        )
        return [
            {
                "reminder_id": row["reminder_id"],
                "kind": row["kind"],
                "due_at": row["due_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def pending_approvals(self) -> dict[str, Any]:
        """待批准事项：规则与发布稿。重启后仍在，可直接继续审批。"""
        rules = self.store.query(
            "SELECT rule_id, version, scope_indicator, created_by, submitted_at FROM rules"
            " WHERE status = ? ORDER BY submitted_at",
            (RULE_PENDING,),
        )
        publications = self.store.query(
            "SELECT publication_id, round_id, indicator, target_year, vintage, created_by,"
            " created_at FROM publications WHERE status = ? ORDER BY created_at",
            (PUB_PENDING,),
        )
        return {"rules": rules, "publications": publications}

    def list_events(self, actor: Actor, *, entity_id: str) -> list[dict[str, Any]]:
        """审计事件流。"""
        self._require(actor, ROLE_AUDITOR, ROLE_RESEARCHER, ROLE_STATISTICIAN)
        rows = self.store.query(
            "SELECT * FROM events WHERE entity_id = ? ORDER BY id", (entity_id,)
        )
        return [
            {
                "kind": row["kind"],
                "entity_id": row["entity_id"],
                "at": row["at"],
                "actor": row["actor"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def list_quarantine(self, actor: Actor) -> list[dict[str, Any]]:
        """隔离区：回执编号相同但内容不同的到达。"""
        self._require(actor, ROLE_AUDITOR, ROLE_RESEARCHER, ROLE_STATISTICIAN)
        rows = self.store.query("SELECT * FROM quarantine ORDER BY id")
        return [
            {
                "id": row["id"],
                "receipt_id": row["receipt_id"],
                "reason": row["reason"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def close(self) -> None:
        self.store.close()
