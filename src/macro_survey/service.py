"""宏观预测征集与发布领域服务。

所有写操作在 store.transaction() 内完成；时间通过 clock 注入，便于测试截止/重启语义。
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

from .consensus import RuleSpec, compute_consensus
from .errors import (
    AuthError,
    ClosedRoundError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    PublishStateError,
    QuarantineError,
    RuleStateError,
)
from .store import Store

ROLE_ADMIN = "ADMIN"
ROLE_RESEARCH = "RESEARCH"
ROLE_CONTRIBUTOR = "CONTRIBUTOR"
ROLE_STATISTICIAN = "STATISTICIAN"
ROLE_APPROVER = "APPROVER"
ROLE_PUBLISHER = "PUBLISHER"
ROLE_AUDITOR = "AUDITOR"

STAFF_ROLES = (
    ROLE_RESEARCH, ROLE_STATISTICIAN, ROLE_PUBLISHER, ROLE_APPROVER, ROLE_AUDITOR, ROLE_ADMIN,
)


def utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return uuid.uuid4().hex


def canonical_fp(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SurveyService:
    def __init__(self, store: Store, clock: Callable[[], str] = utcnow_iso) -> None:
        self.store = store
        self.now = clock

    # ============================ 基础工具 ============================

    def _audit(self, actor: dict[str, Any], action: str, entity: str,
               entity_id: str | None, detail: dict[str, Any] | None = None) -> None:
        self.store.execute(
            "INSERT INTO audit_log(at, actor_user_id, actor_name, action, entity, entity_id, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.now(), actor["id"], actor["username"], action, entity, entity_id,
             self.store.dumps(detail or {})),
        )

    def require_any_role(self, user: dict[str, Any], *roles: str) -> None:
        if not set(user["roles"]).intersection(roles):
            raise ForbiddenError(f"需要角色之一: {', '.join(roles)}")

    def authenticate(self, token: str | None) -> dict[str, Any]:
        if not token:
            raise AuthError("缺少令牌")
        row = self.store.query_one(
            "SELECT u.*, t.revoked AS token_revoked FROM auth_tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token = ?",
            (token,),
        )
        if row is None or row["token_revoked"]:
            raise AuthError("令牌无效或已撤销")
        return self._user_dict(row)

    @staticmethod
    def _user_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "institution_id": row["institution_id"],
            "roles": json.loads(row["roles"]),
        }

    def _is_staff(self, actor: dict[str, Any] | None) -> bool:
        return actor is not None and bool(set(actor["roles"]).intersection(STAFF_ROLES))

    # ============================ 主数据管理 ============================

    def create_institution(self, actor: dict[str, Any], name: str) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_ADMIN, ROLE_RESEARCH)
        inst_id = new_id()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO institutions(id, name, active, created_at) VALUES (?,?,1,?)",
                (inst_id, name, self.now()),
            )
            self._audit(actor, "institution.create", "institution", inst_id, {"name": name})
        return {"id": inst_id, "name": name, "active": True}

    def create_user(self, actor: dict[str, Any] | None, username: str, roles: list[str],
                    institution_id: str | None) -> dict[str, Any]:
        """actor=None 用于种子引导（首个 ADMIN）。"""
        if actor is not None:
            self.require_any_role(actor, ROLE_ADMIN)
        valid = {
            ROLE_ADMIN, ROLE_RESEARCH, ROLE_CONTRIBUTOR, ROLE_STATISTICIAN,
            ROLE_APPROVER, ROLE_PUBLISHER, ROLE_AUDITOR,
        }
        roles = list(dict.fromkeys(roles))
        if not roles or any(r not in valid for r in roles):
            raise DomainError("角色非法")
        if ROLE_CONTRIBUTOR in roles and not institution_id:
            raise DomainError("贡献者必须归属机构")
        if institution_id and not self.store.query_one(
            "SELECT 1 FROM institutions WHERE id = ? AND active = 1", (institution_id,)
        ):
            raise NotFoundError("机构不存在或已停用")
        user_id, token = new_id(), new_id() + new_id()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO users(id, institution_id, username, roles, created_at) VALUES (?,?,?,?,?)",
                (user_id, institution_id, username, json.dumps(roles), self.now()),
            )
            self.store.execute(
                "INSERT INTO auth_tokens(token, user_id, issued_at, revoked) VALUES (?,?,?,0)",
                (token, user_id, self.now()),
            )
            self._audit(
                actor or {"id": "bootstrap", "username": "bootstrap"},
                "user.create", "user", user_id,
                {"username": username, "roles": roles, "institution_id": institution_id},
            )
        return {"id": user_id, "username": username, "roles": roles,
                "institution_id": institution_id, "token": token}

    def create_indicator(self, actor: dict[str, Any], code: str, name: str, unit: str) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_ADMIN, ROLE_RESEARCH)
        ind_id = new_id()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO indicators(id, code, name, unit, created_at) VALUES (?,?,?,?,?)",
                (ind_id, code, name, unit, self.now()),
            )
            self._audit(actor, "indicator.create", "indicator", ind_id, {"code": code})
        return {"id": ind_id, "code": code, "name": name, "unit": unit}

    def create_round(self, actor: dict[str, Any], label: str, opens_at: str,
                     submission_deadline: str, publish_at: str, forecast_years: list[int]) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_ADMIN, ROLE_RESEARCH)
        if not forecast_years:
            raise DomainError("至少需要一个预测年份")
        for ts in (opens_at, submission_deadline, publish_at):
            self._parse_ts(ts)
        if not (opens_at <= submission_deadline <= publish_at):
            raise DomainError("时间顺序应为 开启 <= 截止 <= 发布")
        round_id = new_id()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO rounds(id, label, opens_at, submission_deadline, publish_at, years_json,"
                " status, created_at) VALUES (?,?,?,?,?,?,'open',?)",
                (round_id, label, opens_at, submission_deadline, publish_at,
                 json.dumps(sorted(forecast_years)), self.now()),
            )
            # 到期提醒在创建时即持久化，进程重启后不丢
            self._add_reminder("deadline_approaching", round_id,
                               f"轮次 {label} 征集截止临近",
                               self._shift(submission_deadline, hours=-24, floor=opens_at))
            self._add_reminder("submission_deadline", round_id,
                               f"轮次 {label} 征集已截止", submission_deadline)
            self._add_reminder("publish_due", round_id,
                               f"轮次 {label} 已到计划发布时间", publish_at)
            self._audit(actor, "round.create", "round", round_id,
                        {"label": label, "years": forecast_years})
        return self.round_dict(self.store.query_one("SELECT * FROM rounds WHERE id = ?", (round_id,)))

    def close_round(self, actor: dict[str, Any], round_id: str) -> None:
        self.require_any_role(actor, ROLE_ADMIN, ROLE_RESEARCH, ROLE_PUBLISHER)
        r = self._require_round(round_id)
        with self.store.transaction():
            self.store.execute("UPDATE rounds SET status='closed' WHERE id=?", (round_id,))
            self._audit(actor, "round.close", "round", round_id, {"label": r["label"]})

    def set_weight(self, actor: dict[str, Any], indicator_id: str, institution_id: str,
                   weight: float) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_STATISTICIAN)
        if weight <= 0:
            raise DomainError("权重必须为正")
        if not self.store.query_one("SELECT 1 FROM indicators WHERE id=?", (indicator_id,)):
            raise NotFoundError("指标不存在")
        if not self.store.query_one("SELECT 1 FROM institutions WHERE id=?", (institution_id,)):
            raise NotFoundError("机构不存在")
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO institution_weights(indicator_id, institution_id, weight, updated_by, updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(indicator_id, institution_id)"
                " DO UPDATE SET weight=excluded.weight, updated_by=excluded.updated_by,"
                " updated_at=excluded.updated_at",
                (indicator_id, institution_id, float(weight), actor["id"], self.now()),
            )
            self._audit(actor, "weight.set", "institution_weight", institution_id,
                        {"indicator_id": indicator_id, "weight": weight})
        return {"indicator_id": indicator_id, "institution_id": institution_id,
                "weight": float(weight)}

    # ============================ 征集：提交/修订/撤回 ============================

    def _require_round(self, round_id: str) -> Any:
        row = self.store.query_one("SELECT * FROM rounds WHERE id=?", (round_id,))
        if row is None:
            raise NotFoundError("轮次不存在")
        return row

    def _resolve_target(self, actor: dict[str, Any], body: dict[str, Any]) -> tuple[Any, Any]:
        if not actor.get("institution_id"):
            raise ForbiddenError("只有归属机构的贡献者可以提交")
        round_row = self.store.query_one("SELECT * FROM rounds WHERE label = ?", (body.get("round"),))
        if round_row is None:
            raise NotFoundError("轮次不存在")
        ind = self.store.query_one("SELECT * FROM indicators WHERE code = ?", (body.get("indicator"),))
        if ind is None:
            raise NotFoundError("指标不存在")
        year = body.get("forecast_year")
        if not isinstance(year, int) or isinstance(year, bool):
            raise DomainError("forecast_year 必须为整数")
        if year not in json.loads(round_row["years_json"]):
            raise DomainError("该轮次不征集此预测年份")
        return round_row, ind

    @staticmethod
    def _validate_forecast(body: dict[str, Any]) -> tuple[float, float | None, float | None, float, str]:
        point = body.get("point_value")
        if not isinstance(point, (int, float)) or isinstance(point, bool):
            raise DomainError("point_value 必须为数值")
        point = float(point)
        low, high = body.get("low"), body.get("high")
        low = None if low is None else float(low)
        high = None if high is None else float(high)
        if (low is None) != (high is None):
            raise DomainError("区间上下界必须同时提供")
        if low is not None and not (low <= point <= high):
            raise DomainError("区间需满足 low <= point_value <= high")
        conf = body.get("confidence", 1.0)
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0.0 <= float(conf) <= 1.0):
            raise DomainError("confidence 必须在 0 到 1 之间")
        rationale = body.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise DomainError("提交时必须填写理由")
        return point, low, high, float(conf), rationale.strip()

    def _receipt_fp(self, action: str, round_id: str, indicator_id: str,
                    year: int, body: dict[str, Any]) -> str:
        return canonical_fp({
            "action": action,
            "round_id": round_id,
            "indicator_id": indicator_id,
            "forecast_year": year,
            "point_value": body.get("point_value"),
            "low": body.get("low"),
            "high": body.get("high"),
            "confidence": body.get("confidence"),
            "rationale": body.get("rationale"),
        })

    def submit_forecast(self, actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_CONTRIBUTOR)
        client_ref = self._require_client_ref(body)
        round_row, ind = self._resolve_target(actor, body)
        point, low, high, conf, rationale = self._validate_forecast(body)
        fp = self._receipt_fp("upsert", round_row["id"], ind["id"], body["forecast_year"], body)
        return self._ingest(
            actor, round_row, ind, int(body["forecast_year"]), client_ref, fp, body,
            kind="upsert", point=point, low=low, high=high, confidence=conf, rationale=rationale,
        )

    def withdraw_forecast(self, actor: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_CONTRIBUTOR)
        client_ref = self._require_client_ref(body)
        round_row, ind = self._resolve_target(actor, body)
        year = int(body["forecast_year"])
        fp = self._receipt_fp("withdraw", round_row["id"], ind["id"], year, body)
        rationale = body.get("rationale")
        if rationale is not None and not isinstance(rationale, str):
            raise DomainError("理由必须为字符串")
        return self._ingest(
            actor, round_row, ind, year, client_ref, fp, body,
            kind="withdraw", point=None, low=None, high=None, confidence=None,
            rationale=(rationale or "").strip(),
        )

    @staticmethod
    def _require_client_ref(body: dict[str, Any]) -> str:
        client_ref = body.get("client_ref")
        if not isinstance(client_ref, str) or not client_ref.strip():
            raise DomainError("client_ref 回执编号必填")
        return client_ref

    def _ingest(self, actor: dict[str, Any], round_row: Any, ind: Any, year: int, client_ref: str,
                fp: str, body: dict[str, Any], *, kind: str, point: float | None, low: float | None,
                high: float | None, confidence: float | None, rationale: str) -> dict[str, Any]:
        inst_id = actor["institution_id"]

        # 阶段一：幂等判定。隔离记录必须独立提交——随后抛出的业务异常
        # 不能把"已隔离"这一事实一并回滚掉。
        quarantine_reason: str | None = None
        with self.store.transaction():
            receipt = self.store.query_one(
                "SELECT * FROM idempotent_receipts WHERE institution_id=? AND client_ref=?",
                (inst_id, client_ref),
            )
            if receipt is not None:
                if receipt["request_fingerprint"] == fp:
                    return {**json.loads(receipt["response_json"]), "replayed": True}
                quarantine_reason = f"回执编号 {client_ref} 曾用于不同内容"
            elif self.store.query_one(
                "SELECT id FROM quarantined_messages WHERE institution_id=? AND client_ref=?"
                " AND status='open'",
                (inst_id, client_ref),
            ) is not None:
                quarantine_reason = f"回执编号 {client_ref} 存在未决隔离记录"
            if quarantine_reason is not None:
                self._quarantine(actor, round_row["id"], ind["id"], client_ref, body,
                                 reason=quarantine_reason, fp=fp)
        if quarantine_reason is not None:
            raise QuarantineError(quarantine_reason + "，已进入隔离")

        # 阶段二：正式入库
        with self.store.transaction():
            if round_row["status"] == "closed":
                raise ClosedRoundError("轮次已关闭")

            now = self.now()
            late = now > round_row["submission_deadline"]

            prev = self.store.query_one(
                "SELECT * FROM submissions WHERE round_id=? AND indicator_id=? AND forecast_year=?"
                " AND institution_id=? ORDER BY seq DESC LIMIT 1",
                (round_row["id"], ind["id"], year, inst_id),
            )
            seq = 1 if prev is None else prev["seq"] + 1
            prev_hash = None if prev is None else prev["row_hash"]

            if late:
                # 截止之后：记录但不改变有效水位（不覆盖、不撤回）
                status, supersedes = "late", None
            else:
                status = "withdrawn" if kind == "withdraw" else "active"
                supersedes = prev["id"] if prev is not None and prev["status"] == "active" else None
                if prev is not None and prev["status"] == "active":
                    self.store.execute(
                        "UPDATE submissions SET status=? WHERE id=?",
                        ("withdrawn" if kind == "withdraw" else "superseded", prev["id"]),
                    )

            row_hash = self._row_hash(
                prev_hash, inst_id, round_row["id"], ind["id"], year, seq, kind,
                point, low, high, confidence, rationale, client_ref, now,
            )
            sub_id = new_id()
            self.store.execute(
                "INSERT INTO submissions(id, institution_id, indicator_id, forecast_year, round_id, seq,"
                " kind, point_value, low, high, confidence, rationale, client_ref, idempotency_key,"
                " status, created_at, created_by, supersedes_id, prev_hash, row_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sub_id, inst_id, ind["id"], year, round_row["id"], seq, kind,
                 point, low, high, confidence, rationale, client_ref, body.get("idempotency_key"),
                 status, now, actor["id"], supersedes, prev_hash, row_hash),
            )
            result = {
                "submission_id": sub_id, "seq": seq, "kind": kind, "status": status,
                "late": late, "eligible": status == "active", "received_at": now,
                "replayed": False,
            }
            self.store.execute(
                "INSERT INTO idempotent_receipts(institution_id, client_ref, request_fingerprint,"
                " response_json, status_code, created_at) VALUES (?,?,?,?,?,?)",
                (inst_id, client_ref, fp, json.dumps(result, ensure_ascii=False), 200, now),
            )
            self._audit(actor, f"submission.{kind}", "submission", sub_id,
                        {"round": round_row["label"], "indicator": ind["code"], "year": year,
                         "seq": seq, "status": status, "late": late})
            return result

    def _quarantine(self, actor: dict[str, Any], round_id: str | None, indicator_id: str | None,
                    client_ref: str | None, body: dict[str, Any], *, reason: str, fp: str) -> str:
        qid = new_id()
        self.store.execute(
            "INSERT INTO quarantined_messages(id, institution_id, round_id, indicator_id, client_ref,"
            " reason, payload_json, received_at, status) VALUES (?,?,?,?,?,?,?,?,'open')",
            (qid, actor["institution_id"], round_id, indicator_id, client_ref,
             reason, json.dumps(body, ensure_ascii=False), self.now()),
        )
        self._audit(actor, "quarantine.hold", "quarantined_message", qid,
                    {"client_ref": client_ref, "reason": reason, "fingerprint": fp[:12]})
        return qid

    @staticmethod
    def _row_hash(prev_hash: str | None, inst_id: str, round_id: str, indicator_id: str, year: int,
                  seq: int, kind: str, point: float | None, low: float | None, high: float | None,
                  confidence: float | None, rationale: str, client_ref: str, created_at: str) -> str:
        h = hashlib.sha256()
        h.update((prev_hash or "GENESIS").encode())
        for part in (inst_id, round_id, indicator_id, str(year), str(seq), kind,
                     "" if point is None else repr(point),
                     "" if low is None else repr(low), "" if high is None else repr(high),
                     "" if confidence is None else repr(confidence),
                     rationale, client_ref, created_at):
            h.update(b"|")
            h.update(part.encode("utf-8"))
        return h.hexdigest()

    def list_own_submissions(self, actor: dict[str, Any], round_label: str | None,
                             indicator_code: str | None, year: int | None) -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_CONTRIBUTOR)
        sql = (
            "SELECT s.*, i.code AS indicator_code, r.label AS round_label FROM submissions s"
            " JOIN indicators i ON i.id=s.indicator_id JOIN rounds r ON r.id=s.round_id"
            " WHERE s.institution_id=?"
        )
        params: list[Any] = [actor["institution_id"]]
        if round_label:
            sql += " AND r.label=?"; params.append(round_label)
        if indicator_code:
            sql += " AND i.code=?"; params.append(indicator_code)
        if year is not None:
            sql += " AND s.forecast_year=?"; params.append(year)
        sql += " ORDER BY s.created_at, s.seq"
        return [self._submission_dict(row) for row in self.store.query_all(sql, tuple(params))]

    @staticmethod
    def _submission_dict(row: Any) -> dict[str, Any]:
        return {
            "submission_id": row["id"], "round": row["round_label"],
            "indicator": row["indicator_code"], "forecast_year": row["forecast_year"],
            "seq": row["seq"], "kind": row["kind"], "status": row["status"],
            "point_value": row["point_value"], "low": row["low"], "high": row["high"],
            "confidence": row["confidence"], "rationale": row["rationale"],
            "client_ref": row["client_ref"], "created_at": row["created_at"],
            "supersedes_id": row["supersedes_id"],
        }

    def staff_list_submissions(self, actor: dict[str, Any], round_label: str | None = None) -> list[dict[str, Any]]:
        """工作人员视角：含机构身份与完整修订链（发布前即可见理由与时间）。"""
        self.require_any_role(actor, *STAFF_ROLES)
        sql = (
            "SELECT s.*, i.code AS indicator_code, r.label AS round_label, ins.name AS institution_name"
            " FROM submissions s JOIN indicators i ON i.id=s.indicator_id"
            " JOIN rounds r ON r.id=s.round_id JOIN institutions ins ON ins.id=s.institution_id"
        )
        params: list[Any] = []
        if round_label:
            sql += " WHERE r.label=?"; params.append(round_label)
        sql += " ORDER BY r.label, i.code, s.forecast_year, ins.name, s.seq"
        out = []
        for row in self.store.query_all(sql, tuple(params)):
            d = self._submission_dict(row)
            d["institution"] = row["institution_name"]
            out.append(d)
        return out

    # ============================ 规则起草与审批 ============================

    def draft_rule(self, actor: dict[str, Any], indicator_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_STATISTICIAN)
        if not self.store.query_one("SELECT 1 FROM indicators WHERE id=?", (indicator_id,)):
            raise NotFoundError("指标不存在")
        RuleSpec.from_dict(params)  # 参数校验
        version = self.store.query_one(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM rule_versions WHERE indicator_id=?",
            (indicator_id,),
        )["v"]
        rid = new_id()
        fp = canonical_fp(params)
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO rule_versions(id, indicator_id, version, params_json, status,"
                " created_by, created_by_name, created_at) VALUES (?,?,?,?,'draft',?,?,?)",
                (rid, indicator_id, version,
                 json.dumps({**params, "params_fingerprint": fp}, ensure_ascii=False, sort_keys=True),
                 actor["id"], actor["username"], self.now()),
            )
            self._audit(actor, "rule.draft", "rule_version", rid,
                        {"indicator_id": indicator_id, "version": version})
        return self.get_rule(rid)

    def get_rule(self, rule_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT * FROM rule_versions WHERE id=?", (rule_id,))
        if row is None:
            raise NotFoundError("规则版本不存在")
        return self._rule_dict(row)

    def list_rules(self, actor: dict[str, Any], indicator_id: str | None = None) -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_RESEARCH,
                              ROLE_PUBLISHER, ROLE_AUDITOR, ROLE_ADMIN)
        sql = "SELECT * FROM rule_versions"
        params: tuple = ()
        if indicator_id:
            sql += " WHERE indicator_id=?"; params = (indicator_id,)
        sql += " ORDER BY created_at"
        return [self._rule_dict(r) for r in self.store.query_all(sql, params)]

    @staticmethod
    def _rule_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "indicator_id": row["indicator_id"], "version": row["version"],
            "status": row["status"], "params": json.loads(row["params_json"]),
            "created_by": row["created_by_name"], "approved_by": row["approved_by_name"],
            "created_at": row["created_at"], "approved_at": row["approved_at"],
            "reject_reason": row["reject_reason"],
        }

    def decide_rule(self, actor: dict[str, Any], rule_id: str, approve: bool,
                    reason: str | None = None) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_APPROVER)
        row = self.store.query_one("SELECT * FROM rule_versions WHERE id=?", (rule_id,))
        if row is None:
            raise NotFoundError("规则版本不存在")
        if row["status"] != "draft":
            raise RuleStateError(f"规则已处于 {row['status']} 状态，不可重复审批")
        if row["created_by"] == actor["id"]:
            raise ForbiddenError("起草人与审批人必须为不同用户，职责分离")
        if not approve and not (reason or "").strip():
            raise DomainError("驳回必须填写原因")
        with self.store.transaction():
            self.store.execute(
                "UPDATE rule_versions SET status=?, approved_by=?, approved_by_name=?, approved_at=?,"
                " reject_reason=? WHERE id=?",
                ("approved" if approve else "rejected", actor["id"], actor["username"],
                 self.now() if approve else None, None if approve else reason, rule_id),
            )
            self._audit(actor, f"rule.{'approve' if approve else 'reject'}", "rule_version", rule_id,
                        {"reason": reason})
        return self.get_rule(rule_id)

    def _latest_approved_rule(self, indicator_id: str) -> Any:
        row = self.store.query_one(
            "SELECT * FROM rule_versions WHERE indicator_id=? AND status='approved'"
            " ORDER BY version DESC LIMIT 1",
            (indicator_id,),
        )
        if row is None:
            raise RuleStateError("该指标尚无经审批生效的共识规则")
        return row

    # ============================ 发布申请/批准/水位快照 ============================

    def request_publication(self, actor: dict[str, Any], round_id: str, indicator_id: str,
                            forecast_year: int) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_PUBLISHER, ROLE_STATISTICIAN)
        round_row = self._require_round(round_id)
        if not self.store.query_one("SELECT 1 FROM indicators WHERE id=?", (indicator_id,)):
            raise NotFoundError("指标不存在")
        if self.store.query_one(
            "SELECT * FROM publications WHERE round_id=? AND indicator_id=? AND forecast_year=?"
            " AND status IN ('pending_approval','published')",
            (round_id, indicator_id, forecast_year),
        ) is not None:
            raise PublishStateError("该目标已存在待批或已发布的发布单")

        rule_row = self._latest_approved_rule(indicator_id)
        rule = RuleSpec.from_dict(json.loads(rule_row["params_json"]))
        watermark = self.now()
        samples = self._active_samples(round_id, indicator_id, forecast_year, watermark)
        if len(samples) < rule.min_samples:
            raise PublishStateError(
                f"有效样本 {len(samples)} 个，低于规则下限 {rule.min_samples}，不能申请发布"
            )
        consensus = compute_consensus([self._sample_for_calc(s) for s in samples], rule)
        if not consensus.sufficient or consensus.value is None:
            raise PublishStateError("共识计算未通过：" + consensus.detail.get("reason", "样本不足"))

        pub_id = new_id()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO publications(id, round_id, indicator_id, forecast_year, rule_version_id,"
                " consensus_value, status, requested_by, watermark_at, requested_at, n_samples, n_included)"
                " VALUES (?,?,?,?,?,?,'pending_approval',?,?,?,?,?)",
                (pub_id, round_id, indicator_id, forecast_year, rule_row["id"],
                 consensus.value, actor["id"], watermark, self.now(),
                 len(samples), consensus.included_samples),
            )
            self._snapshot_samples(pub_id, samples, consensus)
            # 待批决定到期提醒（计划发布时间），批准/驳回时取消
            self._add_reminder("publish_decision_due", pub_id,
                               "发布单等待批准，已到计划发布时刻", round_row["publish_at"])
            self._audit(actor, "publication.request", "publication", pub_id,
                        {"round": round_row["label"], "watermark": watermark,
                         "n_samples": len(samples), "consensus": consensus.value})
        return self.get_publication(pub_id)

    def _active_samples(self, round_id: str, indicator_id: str, year: int, watermark: str) -> list[Any]:
        """截止水位内的有效贡献：每个机构取水位之前最后一条 active 记录。"""
        rows = self.store.query_all(
            "SELECT s.*, ins.name AS institution_name FROM submissions s"
            " JOIN institutions ins ON ins.id=s.institution_id"
            " WHERE s.round_id=? AND s.indicator_id=? AND s.forecast_year=? AND s.status='active'"
            " AND s.created_at<=? ORDER BY s.institution_id, s.seq",
            (round_id, indicator_id, year, watermark),
        )
        latest: dict[str, Any] = {}
        for row in rows:
            latest[row["institution_id"]] = row
        return list(latest.values())

    def _weight_for(self, indicator_id: str, institution_id: str) -> float:
        row = self.store.query_one(
            "SELECT weight FROM institution_weights WHERE indicator_id=? AND institution_id=?",
            (indicator_id, institution_id),
        )
        return 1.0 if row is None else float(row["weight"])

    def _sample_for_calc(self, row: Any) -> dict[str, Any]:
        return {
            "contributor_anon": row["institution_id"],  # 计算阶段用内部 id，落库时换成匿名码
            "value": row["point_value"],
            "weight": self._weight_for(row["indicator_id"], row["institution_id"]),
            "confidence": row["confidence"],
            "revision_seq": row["seq"],
        }

    def _snapshot_samples(self, pub_id: str, samples: list[Any], consensus: Any) -> None:
        # consensus 排除键是 institution_id；每次发布重新洗牌匿名码，防止跨报告比对
        reasons = consensus.detail.get("exclusion_reasons", {})
        codes = [f"C{i + 1:02d}" for i in range(len(samples))]
        random.SystemRandom().shuffle(codes)
        code_by_inst = {row["institution_id"]: anon for row, anon in zip(samples, codes)}
        for row in samples:
            anon = code_by_inst[row["institution_id"]]
            reason = reasons.get(row["institution_id"])
            self.store.execute(
                "INSERT INTO publication_samples(publication_id, institution_id, anon_code, revision_seq,"
                " kind, point_value, low, high, confidence, rationale, effective_value, included,"
                " excluded_reason, weight, submitted_at, row_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pub_id, row["institution_id"], anon, row["seq"], row["kind"],
                 row["point_value"], row["low"], row["high"], row["confidence"], row["rationale"],
                 row["point_value"], 0 if reason else 1, reason,
                 self._weight_for(row["indicator_id"], row["institution_id"]),
                 row["created_at"], row["row_hash"]),
            )

    def decide_publication(self, actor: dict[str, Any], pub_id: str, approve: bool,
                           reason: str | None = None) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_APPROVER)
        row = self.store.query_one("SELECT * FROM publications WHERE id=?", (pub_id,))
        if row is None:
            raise NotFoundError("发布单不存在")
        if row["status"] != "pending_approval":
            raise PublishStateError(f"发布单已为 {row['status']} 状态")
        if row["requested_by"] == actor["id"]:
            raise ForbiddenError("申请人与批准人必须为不同用户")
        if not approve and not (reason or "").strip():
            raise DomainError("驳回必须填写原因")
        now = self.now()
        with self.store.transaction():
            self.store.execute(
                "UPDATE publications SET status=?, decided_by=?, decided_at=?, published_at=?,"
                " reject_reason=? WHERE id=?",
                ("published" if approve else "rejected", actor["id"], now,
                 now if approve else None, None if approve else reason, pub_id),
            )
            self._cancel_reminders("publish_decision_due", pub_id)
            self._audit(actor, f"publication.{'approve' if approve else 'reject'}",
                        "publication", pub_id, {"reason": reason})
        return self.get_publication(pub_id)

    def get_publication(self, pub_id: str) -> dict[str, Any]:
        row = self.store.query_one(
            "SELECT p.*, i.code AS indicator_code, i.name AS indicator_name, i.unit,"
            " r.label AS round_label, rv.params_json AS rule_params, rv.version AS rule_version"
            " FROM publications p JOIN indicators i ON i.id=p.indicator_id"
            " JOIN rounds r ON r.id=p.round_id JOIN rule_versions rv ON rv.id=p.rule_version_id"
            " WHERE p.id=?",
            (pub_id,),
        )
        if row is None:
            raise NotFoundError("发布单不存在")
        return {
            "id": row["id"], "round": row["round_label"], "indicator": row["indicator_code"],
            "indicator_name": row["indicator_name"], "unit": row["unit"],
            "forecast_year": row["forecast_year"], "status": row["status"],
            "consensus_value": row["consensus_value"], "rule_version": row["rule_version"],
            "rule_params": json.loads(row["rule_params"]), "watermark_at": row["watermark_at"],
            "requested_at": row["requested_at"], "published_at": row["published_at"],
            "reject_reason": row["reject_reason"], "n_samples": row["n_samples"],
            "n_included": row["n_included"],
        }

    def list_publications(self, actor: dict[str, Any] | None = None,
                          status: str | None = None) -> list[dict[str, Any]]:
        # 机构只能看到已发布报告；待批/驳回水位属于工作人员视图
        if actor is not None and not self._is_staff(actor):
            status = "published"
        sql = (
            "SELECT p.*, i.code AS indicator_code, r.label AS round_label FROM publications p"
            " JOIN indicators i ON i.id=p.indicator_id JOIN rounds r ON r.id=p.round_id"
        )
        params: tuple = ()
        if status:
            sql += " WHERE p.status=?"; params = (status,)
        sql += " ORDER BY p.requested_at DESC"
        return [{
            "id": row["id"], "round": row["round_label"], "indicator": row["indicator_code"],
            "forecast_year": row["forecast_year"], "status": row["status"],
            "consensus_value": row["consensus_value"], "watermark_at": row["watermark_at"],
            "published_at": row["published_at"], "n_samples": row["n_samples"],
            "n_included": row["n_included"],
        } for row in self.store.query_all(sql, params)]

    def explain_publication(self, actor: dict[str, Any] | None, pub_id: str) -> dict[str, Any]:
        """解释报告数字：规则、有效样本、剔除原因、修订顺序。

        机构视角：其他贡献者仅显示匿名码与数值，理由一律遮蔽；只告知其自身匿名码归属。
        """
        pub = self.get_publication(pub_id)
        staff = self._is_staff(actor)
        if not staff and pub["status"] != "published":
            raise ForbiddenError("报告尚未发布，仅工作人员可查看")
        rows = self.store.query_all(
            "SELECT * FROM publication_samples WHERE publication_id=?", (pub_id,),
        )
        own_inst = None if actor is None else actor.get("institution_id")

        samples, own_code = [], None
        for row in rows:
            is_own = row["institution_id"] == own_inst
            if is_own:
                own_code = row["anon_code"]
            samples.append({
                "anon_code": row["anon_code"],
                "revision_seq": row["revision_seq"],
                "kind": row["kind"],
                "point_value": row["point_value"],
                "low": row["low"],
                "high": row["high"],
                "confidence": row["confidence"],
                "weight": row["weight"],
                "included": bool(row["included"]),
                "excluded_reason": row["excluded_reason"],
                "submitted_at": row["submitted_at"],
                "rationale": row["rationale"] if (staff or is_own) else None,
                "institution": row["institution_id"] if staff else ("(self)" if is_own else None),
            })
        samples.sort(key=lambda s: (s["submitted_at"], s["anon_code"]))
        timeline = [{
            "anon_code": s["anon_code"], "revision_seq": s["revision_seq"],
            "event": f"第{s['revision_seq']}版于{s['submitted_at']}进入水位",
            "at": s["submitted_at"],
        } for s in sorted(samples, key=lambda s: s["submitted_at"])]

        # 完整修订链：工作人员可看所有机构；机构视图只补全自己的历史
        # （含被取代、撤回、迟到版本），用于回答"何时改过判断"。
        history_rows = self.store.query_all(
            "SELECT * FROM submissions WHERE round_id=(SELECT id FROM rounds WHERE label=?)"
            " AND indicator_id=(SELECT id FROM indicators WHERE code=?) AND forecast_year=?"
            " ORDER BY created_at, seq",
            (pub["round"], pub["indicator"], pub["forecast_year"]),
        )
        code_by_inst = {r["institution_id"]: r["anon_code"] for r in rows}
        watermark_hashes = {r["row_hash"] for r in rows}
        revision_history = []
        for hr in history_rows:
            anon = code_by_inst.get(hr["institution_id"])
            is_own = hr["institution_id"] == own_inst
            if not staff and not is_own:
                continue
            revision_history.append({
                "anon_code": anon if (staff or is_own) else None,
                "institution": hr["institution_id"] if staff else ("(self)" if is_own else None),
                "seq": hr["seq"], "kind": hr["kind"], "status": hr["status"],
                "point_value": hr["point_value"], "confidence": hr["confidence"],
                "rationale": hr["rationale"] if (staff or is_own) else None,
                "created_at": hr["created_at"],
                "in_watermark": hr["row_hash"] in watermark_hashes,
            })
        return {
            "publication": pub,
            "rule": {"version": pub["rule_version"], "params": pub["rule_params"]},
            "watermark_at": pub["watermark_at"],
            "consensus_value": pub["consensus_value"],
            "samples": samples,
            "revision_timeline": timeline,
            "revision_history": revision_history,
            "your_anon_code": own_code,
            "view": "staff" if staff else "institution",
        }

    def verify_publication_integrity(self, actor: dict[str, Any], pub_id: str) -> dict[str, Any]:
        """用快照样本与已批准规则重算发布数字，核对未被事后改动，并校验提交哈希链。"""
        self.require_any_role(actor, ROLE_AUDITOR, ROLE_RESEARCH, ROLE_APPROVER, ROLE_ADMIN)
        pub = self.get_publication(pub_id)
        rule = RuleSpec.from_dict(pub["rule_params"])
        rows = self.store.query_all(
            "SELECT * FROM publication_samples WHERE publication_id=?", (pub_id,),
        )
        calc_rows = [{
            "contributor_anon": r["institution_id"], "value": r["point_value"],
            "weight": r["weight"], "confidence": r["confidence"], "revision_seq": r["revision_seq"],
        } for r in rows]
        result = compute_consensus(calc_rows, rule)
        recomputed, stored = result.value, pub["consensus_value"]
        match = recomputed is not None and abs(recomputed - stored) < 1e-9
        broken = self._verify_hash_chain(pub)
        return {
            "publication_id": pub_id,
            "stored_value": stored,
            "recomputed_value": recomputed,
            "matches": match,
            "excluded_now": list(result.excluded),
            "hash_chain_intact": not broken,
            "chain_breaks": broken,
        }

    def _verify_hash_chain(self, pub: dict[str, Any]) -> list[dict[str, Any]]:
        """重放该目标下各机构提交链，检查 prev_hash/row_hash。"""
        rows = self.store.query_all(
            "SELECT * FROM submissions WHERE round_id=(SELECT id FROM rounds WHERE label=?)"
            " AND indicator_id=(SELECT id FROM indicators WHERE code=?)"
            " AND forecast_year=? ORDER BY institution_id, seq",
            (pub["round"], pub["indicator"], pub["forecast_year"]),
        )
        by_inst: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            by_inst[row["institution_id"]].append(row)
        breaks: list[dict[str, Any]] = []
        for inst_id, chain in by_inst.items():
            prev = None
            for row in chain:
                expect_hash = self._row_hash(
                    prev, inst_id, row["round_id"], row["indicator_id"], row["forecast_year"],
                    row["seq"], row["kind"], row["point_value"], row["low"], row["high"],
                    row["confidence"], row["rationale"] or "", row["client_ref"], row["created_at"],
                )
                if row["prev_hash"] != prev or row["row_hash"] != expect_hash:
                    breaks.append({"institution_id": inst_id, "seq": row["seq"],
                                   "submission_id": row["id"]})
                prev = row["row_hash"]
        return breaks

    # ============================ 实际值 / 误差 / 回测 / 复核 ============================

    def record_actual(self, actor: dict[str, Any], indicator_id: str, forecast_year: int,
                      actual_value: float, released_at: str,
                      review_threshold: float | None = None) -> dict[str, Any]:
        self.require_any_role(actor, ROLE_RESEARCH, ROLE_ADMIN)
        self._parse_ts(released_at)
        if not self.store.query_one("SELECT 1 FROM indicators WHERE id=?", (indicator_id,)):
            raise NotFoundError("指标不存在")
        if self.store.query_one(
            "SELECT id FROM actuals WHERE indicator_id=? AND forecast_year=?",
            (indicator_id, forecast_year),
        ) is not None:
            raise ConflictError("实际值已记录；更正请走更正流程")
        actual_id, now = new_id(), self.now()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO actuals(id, indicator_id, forecast_year, actual_value, review_threshold,"
                " released_at, recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (actual_id, indicator_id, forecast_year, float(actual_value),
                 review_threshold, released_at, actor["id"], now),
            )
            pubs = self.store.query_all(
                "SELECT * FROM publications WHERE indicator_id=? AND forecast_year=? AND status='published'",
                (indicator_id, forecast_year),
            )
            threshold = float(review_threshold) if review_threshold is not None else None
            flagged = 0
            for pub in pubs:
                error = pub["consensus_value"] - float(actual_value)
                unit_scale = abs(float(actual_value)) or 1.0
                rule = self.store.query_one(
                    "SELECT * FROM rule_versions WHERE id=?", (pub["rule_version_id"],)
                )
                self.store.execute(
                    "INSERT INTO forecast_errors(publication_id, actual_value, error, abs_error, pct_error,"
                    " rule_version_id, rule_params_json, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (pub["id"], float(actual_value), error, abs(error), abs(error) / unit_scale,
                     pub["rule_version_id"], rule["params_json"], now),
                )
                samples = self.store.query_all(
                    "SELECT * FROM publication_samples WHERE publication_id=? AND included=1",
                    (pub["id"],),
                )
                errors = [abs(s["effective_value"] - float(actual_value)) for s in samples]
                auto_threshold = threshold if threshold is not None else (
                    (sum(errors) / len(errors)) * 1.5 if errors else 0.0
                )
                pub_flagged = False
                for s in samples:
                    dev = abs(s["effective_value"] - float(actual_value))
                    needs = dev > auto_threshold + 1e-9  # 浮点容差：恰等阈值不算"超出"
                    pub_flagged = pub_flagged or needs
                    flagged += int(needs)
                    self.store.execute(
                        "INSERT INTO contributor_deviations(publication_id, institution_id, anon_code,"
                        " forecast_value, actual_value, abs_error, threshold, needs_review, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (pub["id"], s["institution_id"], s["anon_code"], s["effective_value"],
                         float(actual_value), dev, auto_threshold, 1 if needs else 0, now),
                    )
                if pub_flagged:
                    self._add_reminder(
                        "deviation_review", pub["id"],
                        f"报告 {pub['id'][:8]} 存在超出阈值的机构偏离，下一轮前复核", now,
                    )
            self._audit(actor, "actual.record", "actual", actual_id,
                        {"indicator_id": indicator_id, "year": forecast_year,
                         "actual": actual_value, "publications_scored": len(pubs), "flagged": flagged})
        return {"actual_id": actual_id, "actual_value": float(actual_value),
                "publications_scored": len(pubs), "deviations_flagged": flagged}

    def list_errors(self, actor: dict[str, Any], indicator_id: str | None = None) -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_RESEARCH, ROLE_STATISTICIAN, ROLE_APPROVER,
                              ROLE_AUDITOR, ROLE_ADMIN)
        sql = (
            "SELECT fe.*, i.code AS indicator_code, p.forecast_year, r.label AS round_label"
            " FROM forecast_errors fe JOIN publications p ON p.id=fe.publication_id"
            " JOIN indicators i ON i.id=p.indicator_id JOIN rounds r ON r.id=p.round_id"
        )
        params: tuple = ()
        if indicator_id:
            sql += " WHERE p.indicator_id=?"; params = (indicator_id,)
        sql += " ORDER BY fe.computed_at DESC"
        return [dict(r) for r in self.store.query_all(sql, params)]

    def run_backtest(self, actor: dict[str, Any], rule_version_id: str, indicator_id: str,
                     forecast_year: int | None = None) -> dict[str, Any]:
        """用历史发布快照回测任一规则版本（含草案，便于审批前拿证据）。"""
        self.require_any_role(actor, ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_RESEARCH,
                              ROLE_AUDITOR, ROLE_ADMIN)
        rule_row = self.store.query_one("SELECT * FROM rule_versions WHERE id=?", (rule_version_id,))
        if rule_row is None:
            raise NotFoundError("规则版本不存在")
        rule = RuleSpec.from_dict(json.loads(rule_row["params_json"]))
        sql = (
            "SELECT p.* FROM publications p JOIN actuals a ON a.indicator_id=p.indicator_id"
            " AND a.forecast_year=p.forecast_year WHERE p.indicator_id=? AND p.status='published'"
        )
        params: list[Any] = [indicator_id]
        if forecast_year is not None:
            sql += " AND p.forecast_year=?"; params.append(forecast_year)
        sql += " ORDER BY p.published_at"
        scored: list[float] = []
        cover_hits = cover_total = 0
        for pub in self.store.query_all(sql, tuple(params)):
            rows = self.store.query_all(
                "SELECT * FROM publication_samples WHERE publication_id=?", (pub["id"],)
            )
            calc_rows = [{
                "contributor_anon": r["institution_id"], "value": r["point_value"],
                "weight": r["weight"], "confidence": r["confidence"], "revision_seq": r["revision_seq"],
            } for r in rows]
            cons = compute_consensus(calc_rows, rule)
            if cons.value is None:
                continue
            actual = self.store.query_one(
                "SELECT actual_value FROM actuals WHERE indicator_id=? AND forecast_year=?",
                (indicator_id, pub["forecast_year"]),
            )["actual_value"]
            scored.append(cons.value - actual)
            spread = cons.detail.get("spread") or {}
            if "min" in spread:
                cover_total += 1
                if spread["min"] <= actual <= spread["max"]:
                    cover_hits += 1
        if not scored:
            raise DomainError("没有可回测的已发布历史样本")
        mae = sum(abs(e) for e in scored) / len(scored)
        rmse = (sum(e * e for e in scored) / len(scored)) ** 0.5
        bias = sum(scored) / len(scored)
        coverage = cover_hits / cover_total if cover_total else None
        bt_id, now = new_id(), self.now()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO rule_backtests(id, rule_version_id, indicator_id, forecast_year,"
                " n_publications, mae, rmse, bias, interval_coverage, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule_version_id, indicator_id, forecast_year)"
                " DO UPDATE SET n_publications=excluded.n_publications, mae=excluded.mae,"
                " rmse=excluded.rmse, bias=excluded.bias, interval_coverage=excluded.interval_coverage,"
                " computed_at=excluded.computed_at",
                (bt_id, rule_version_id, indicator_id, forecast_year, len(scored),
                 mae, rmse, bias, coverage, now),
            )
            self._audit(actor, "rule.backtest", "rule_backtest", bt_id,
                        {"rule_version_id": rule_version_id, "n": len(scored), "mae": mae})
        return {"id": bt_id, "rule_version_id": rule_version_id, "indicator_id": indicator_id,
                "forecast_year": forecast_year, "n_publications": len(scored),
                "mae": mae, "rmse": rmse, "bias": bias, "interval_coverage": coverage}

    def list_backtests(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_RESEARCH,
                              ROLE_AUDITOR, ROLE_ADMIN)
        return [dict(r) for r in self.store.query_all(
            "SELECT * FROM rule_backtests ORDER BY computed_at DESC")]

    def next_round_reviews(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        """下一轮开始前需要复核的偏离（工作人员可见机构身份）。"""
        self.require_any_role(actor, *STAFF_ROLES)
        rows = self.store.query_all(
            "SELECT d.*, ins.name AS institution_name, i.code AS indicator_code,"
            " r.label AS round_label FROM contributor_deviations d"
            " JOIN institutions ins ON ins.id=d.institution_id"
            " JOIN publications p ON p.id=d.publication_id"
            " JOIN indicators i ON i.id=p.indicator_id JOIN rounds r ON r.id=p.round_id"
            " WHERE d.needs_review=1 ORDER BY d.abs_error DESC"
        )
        return [dict(r) for r in rows]

    def own_reviews(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        """机构只能看到关于自己的复核记录，无法借此推断他人。"""
        self.require_any_role(actor, ROLE_CONTRIBUTOR)
        rows = self.store.query_all(
            "SELECT d.anon_code, d.forecast_value, d.actual_value, d.abs_error, d.threshold,"
            " d.needs_review, i.code AS indicator_code, r.label AS round_label, p.forecast_year"
            " FROM contributor_deviations d JOIN publications p ON p.id=d.publication_id"
            " JOIN indicators i ON i.id=p.indicator_id JOIN rounds r ON r.id=p.round_id"
            " WHERE d.institution_id=? ORDER BY d.created_at DESC",
            (actor["institution_id"],),
        )
        return [dict(r) for r in rows]

    # ============================ 隔离区处理 ============================

    def list_quarantine(self, actor: dict[str, Any], status: str = "open") -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_RESEARCH, ROLE_STATISTICIAN, ROLE_PUBLISHER,
                              ROLE_ADMIN, ROLE_AUDITOR)
        rows = self.store.query_all(
            "SELECT q.*, ins.name AS institution_name FROM quarantined_messages q"
            " JOIN institutions ins ON ins.id=q.institution_id WHERE q.status=? ORDER BY q.received_at",
            (status,),
        )
        return [dict(r) for r in rows]

    def resolve_quarantine(self, actor: dict[str, Any], quarantine_id: str, decision: str,
                           note: str | None = None) -> dict[str, Any]:
        """discard：丢弃；accepted：以原载荷按处理时刻重新进入征集（若已迟到则按迟到处理）。"""
        self.require_any_role(actor, ROLE_STATISTICIAN, ROLE_PUBLISHER, ROLE_RESEARCH, ROLE_ADMIN)
        if decision not in ("discard", "accepted"):
            raise DomainError("decision 只能是 discard 或 accepted")
        row = self.store.query_one("SELECT * FROM quarantined_messages WHERE id=?", (quarantine_id,))
        if row is None:
            raise NotFoundError("隔离记录不存在")
        if row["status"] != "open":
            raise ConflictError("该隔离记录已处理")
        result: dict[str, Any] = {"quarantine_id": quarantine_id, "decision": decision}
        with self.store.transaction():
            if decision == "accepted":
                payload = json.loads(row["payload_json"])
                service_user = self.store.query_one(
                    "SELECT u.* FROM users u WHERE u.institution_id=?"
                    " AND instr(u.roles, 'CONTRIBUTOR')>0 ORDER BY u.created_at LIMIT 1",
                    (row["institution_id"],),
                )
                if service_user is None:
                    raise DomainError("该机构没有可用贡献账号，无法受理")
                payload = dict(payload)
                payload["client_ref"] = f"q-{row['id'][:12]}-{payload.get('client_ref', 'x')}"
                user = self._user_dict(service_user)
                sub = (self.withdraw_forecast if "point_value" not in payload
                       else self.submit_forecast)(user, payload)
                result["submission"] = sub
            self.store.execute(
                "UPDATE quarantined_messages SET status=?, resolution_note=?, resolved_by=?,"
                " resolved_at=? WHERE id=?",
                ("accepted" if decision == "accepted" else "discarded", note, actor["id"],
                 self.now(), quarantine_id),
            )
            self._audit(actor, f"quarantine.{decision}", "quarantined_message", quarantine_id,
                        {"note": note})
        return result

    # ============================ 提醒 / 重启恢复 ============================

    def _add_reminder(self, kind: str, ref_id: str, message: str, due_at: str) -> str:
        rid = new_id()
        self.store.execute(
            "INSERT INTO reminders(id, due_at, kind, ref_id, message, status, created_at)"
            " VALUES (?,?,?,?,?,'due',?)",
            (rid, due_at, kind, ref_id, message, self.now()),
        )
        return rid

    def _cancel_reminders(self, kind: str, ref_id: str) -> None:
        self.store.execute(
            "UPDATE reminders SET status='cancelled' WHERE kind=? AND ref_id=? AND status='due'",
            (kind, ref_id),
        )

    def scan_due(self, actor: dict[str, Any] | None = None, mark_delivered: bool = True) -> dict[str, Any]:
        """汇总到期事项并标记投递；重启后只依据库里持久状态继续。仅工作人员。"""
        self.require_any_role(actor, *STAFF_ROLES)
        now = self.now()
        with self.store.transaction():
            due_rows = self.store.query_all(
                "SELECT * FROM reminders WHERE status='due' AND due_at<=? ORDER BY due_at", (now,)
            )
            reminders = [dict(r) for r in due_rows]
            if mark_delivered and due_rows:
                self.store.execute(
                    "UPDATE reminders SET status='delivered', delivered_at=?"
                    " WHERE status='due' AND due_at<=?",
                    (now, now),
                )
            pending = self.store.query_all(
                "SELECT p.id, p.consensus_value, p.watermark_at, p.requested_at, r.label AS round_label,"
                " i.code AS indicator_code, p.forecast_year FROM publications p"
                " JOIN rounds r ON r.id=p.round_id JOIN indicators i ON i.id=p.indicator_id"
                " WHERE p.status='pending_approval' AND r.publish_at<=?",
                (now,),
            )
        return {"scanned_at": now, "reminders": reminders,
                "pending_approvals_overdue": [dict(r) for r in pending]}

    def cancel_reminder(self, actor: dict[str, Any], reminder_id: str) -> None:
        self.require_any_role(actor, ROLE_PUBLISHER, ROLE_RESEARCH, ROLE_ADMIN)
        row = self.store.query_one("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        if row is None:
            raise NotFoundError("提醒不存在")
        with self.store.transaction():
            self.store.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (reminder_id,))
            self._audit(actor, "reminder.cancel", "reminder", reminder_id, {"kind": row["kind"]})

    def resume_state(self, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """进程重启后调用：看待完成的批准、未投递提醒与未决隔离。仅工作人员。"""
        self.require_any_role(actor, *STAFF_ROLES)
        pending = self.store.query_all(
            "SELECT p.id, r.label AS round_label, i.code AS indicator_code, p.forecast_year,"
            " p.requested_at, r.publish_at FROM publications p JOIN rounds r ON r.id=p.round_id"
            " JOIN indicators i ON i.id=p.indicator_id WHERE p.status='pending_approval'"
            " ORDER BY p.requested_at"
        )
        reminders = self.store.query_all(
            "SELECT id, due_at, kind, ref_id, message FROM reminders WHERE status='due' ORDER BY due_at"
        )
        open_q = self.store.query_one(
            "SELECT COUNT(*) AS c FROM quarantined_messages WHERE status='open'"
        )["c"]
        return {"at": self.now(), "pending_approvals": [dict(r) for r in pending],
                "due_reminders": [dict(r) for r in reminders], "open_quarantine": open_q}

    def audit_log(self, actor: dict[str, Any], limit: int = 200) -> list[dict[str, Any]]:
        self.require_any_role(actor, ROLE_AUDITOR, ROLE_RESEARCH, ROLE_ADMIN)
        rows = self.store.query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    # ============================ 杂项 ============================

    @staticmethod
    def round_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "label": row["label"], "opens_at": row["opens_at"],
            "submission_deadline": row["submission_deadline"], "publish_at": row["publish_at"],
            "forecast_years": json.loads(row["years_json"]), "status": row["status"],
        }

    @staticmethod
    def _parse_ts(value: str) -> Any:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise DomainError(f"时间格式非法: {value}")

    @staticmethod
    def _shift(ts: str, *, hours: float, floor: str | None = None) -> str:
        result = (datetime.fromisoformat(ts) + timedelta(hours=hours)).isoformat()
        if floor and result < floor:
            return floor
        return result
