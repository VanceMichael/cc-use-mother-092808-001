"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类，HTTP 层映射为 4xx。"""

    http_status = 400
    code = "bad_request"


class AuthError(DomainError):
    http_status = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    http_status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    http_status = 404
    code = "not_found"


class ConflictError(DomainError):
    """编号相同而内容不同等冲突，调用方应转入隔离。"""

    http_status = 409
    code = "conflict"


class QuarantineError(DomainError):
    http_status = 409
    code = "quarantined"


class RuleStateError(DomainError):
    http_status = 409
    code = "rule_state"


class PublishStateError(DomainError):
    http_status = 409
    code = "publish_state"


class ClosedRoundError(DomainError):
    http_status = 409
    code = "round_closed"
