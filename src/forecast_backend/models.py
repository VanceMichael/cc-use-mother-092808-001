"""宏观预测征集与发布后端的核心类型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# ---- 角色 ----
ROLE_INSTITUTION = "institution"      # 获准提交预测的市场机构
ROLE_STATISTICIAN = "statistician"    # 统计人员：定义规则、计算发布稿、登记实际值
ROLE_APPROVER = "approver"            # 规则审批人员：批准规则与发布（四眼原则）
ROLE_AUDITOR = "auditor"              # 审计人员：可见真实身份
ROLE_RESEARCHER = "researcher"        # 研究主管：可见真实身份，负责机构准入
ROLES = frozenset(
    {ROLE_INSTITUTION, ROLE_STATISTICIAN, ROLE_APPROVER, ROLE_AUDITOR, ROLE_RESEARCHER}
)

# ---- 提交状态 ----
SUBMISSION_ACTIVE = "active"
SUBMISSION_SUPERSEDED = "superseded"
SUBMISSION_WITHDRAWN = "withdrawn"

# ---- 规则状态 ----
RULE_DRAFT = "draft"
RULE_PENDING = "pending"
RULE_ACTIVE = "active"
RULE_REJECTED = "rejected"
RULE_SUPERSEDED = "superseded"

# ---- 发布状态 ----
PUB_PENDING = "pending_approval"
PUB_PUBLISHED = "published"
PUB_REJECTED = "rejected"

# ---- 调查轮次状态 ----
ROUND_OPEN = "open"
ROUND_CLOSED = "closed"

# ---- 匿名保护 ----
DEFAULT_MIN_INSTITUTIONS = 3
DEFAULT_REVIEW_THRESHOLD = 0.25

WEIGHTING_SCHEMES = frozenset({"equal", "confidence", "accuracy"})
OUTLIER_METHODS = frozenset({"none", "iqr"})


class DomainError(Exception):
    """业务规则违例；code 供程序判断，http_status 供 API 层映射。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "domain_error",
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details or {}


@dataclass(frozen=True)
class Actor:
    """一次操作的执行者。actor_id 对机构角色即机构编号。"""

    actor_id: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未知角色: {self.role}")
        if not self.actor_id:
            raise ValueError("操作者编号不能为空")


@dataclass(frozen=True)
class RuleDefinition:
    """共识规则：加权方式、中位数口径、异常值规则与匿名下限。"""

    weighting: str
    outlier_method: str
    outlier_k: float
    min_institutions: int
    review_threshold: float

    @staticmethod
    def parse(raw: dict[str, Any]) -> RuleDefinition:
        if not isinstance(raw, dict):
            raise DomainError("规则定义必须是对象", code="invalid_rule")
        weighting = raw.get("weighting", "equal")
        if weighting not in WEIGHTING_SCHEMES:
            raise DomainError(f"未知加权方式: {weighting}", code="invalid_rule")
        outlier = raw.get("outlier", {"method": "none"})
        if not isinstance(outlier, dict):
            raise DomainError("异常值规则必须是对象", code="invalid_rule")
        method = outlier.get("method", "none")
        if method not in OUTLIER_METHODS:
            raise DomainError(f"未知异常值规则: {method}", code="invalid_rule")
        k = outlier.get("k", 1.5)
        if not isinstance(k, (int, float)) or isinstance(k, bool) or k <= 0:
            raise DomainError("异常值系数必须为正数", code="invalid_rule")
        minimum = raw.get("min_institutions", DEFAULT_MIN_INSTITUTIONS)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
            raise DomainError("匿名保护下限至少为 2 家机构", code="invalid_rule")
        threshold = raw.get("review_threshold", DEFAULT_REVIEW_THRESHOLD)
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
            raise DomainError("复核阈值必须为正数", code="invalid_rule")
        return RuleDefinition(
            weighting=weighting,
            outlier_method=method,
            outlier_k=float(k),
            min_institutions=minimum,
            review_threshold=float(threshold),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "weighting": self.weighting,
            "outlier": {"method": self.outlier_method, "k": self.outlier_k},
            "min_institutions": self.min_institutions,
            "review_threshold": self.review_threshold,
        }


def canonical_json(value: Any) -> str:
    """与键顺序无关的规范 JSON，用于回执内容指纹。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def pseudonym(salt: str, round_id: str, institution_id: str) -> str:
    """按调查轮次生成匿名贡献者代号，跨轮次不可串联。"""
    digest = hashlib.sha256(f"{salt}|{round_id}|{institution_id}".encode("utf-8")).hexdigest()
    return f"anon-{digest[:12]}"
