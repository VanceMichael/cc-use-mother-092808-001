"""WSGI HTTP API（仅依赖标准库）。

鉴权：Authorization: Bearer <token>
所有请求/响应使用 JSON。时间为 ISO-8601。
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qs

from .errors import DomainError
from .service import (
    ROLE_ADMIN,
    ROLE_RESEARCH,
    SurveyService,
)

class HttpApi:
    def __init__(self, service: SurveyService) -> None:
        self.svc = service
        self.routes: list[tuple[str, str, Callable[..., Any], bool]] = []
        self._register()

    def route(self, method: str, pattern: str, handler: Callable[..., Any], auth: bool = True) -> None:
        self.routes.append((method, pattern, handler, auth))

    # ----- WSGI -----

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        try:
            return self._dispatch(environ, start_response)
        except DomainError as exc:
            return self._json(start_response, exc.http_status,
                              {"error": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 边界兜底
            self.svc.store.rollback()
            return self._json(start_response, 500,
                              {"error": "internal_error", "message": str(exc)})

    def _dispatch(self, environ: dict, start_response: Callable) -> list[bytes]:
        method = environ["REQUEST_METHOD"]
        path = environ["PATH_INFO"].rstrip("/") or "/"
        body = self._read_json(environ)
        query = parse_qs(environ.get("QUERY_STRING", ""))

        for m, pattern, handler, needs_auth in self.routes:
            if m == method and path == pattern:
                actor = None
                if needs_auth:
                    token = environ.get("HTTP_AUTHORIZATION", "")
                    token = token.removeprefix("Bearer ").strip() if token.startswith("Bearer ") else None
                    actor = self.svc.authenticate(token)
                result = handler(actor, body, query)
                status = 200
                if isinstance(result, tuple):
                    result, status = result
                return self._json(start_response, status, result)
        return self._json(start_response, 404, {"error": "not_found", "message": f"无此端点: {method} {path}"})

    @staticmethod
    def _read_json(environ: dict) -> dict[str, Any]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length == 0:
            return {}
        raw = environ["wsgi.input"].read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    @staticmethod
    def _json(start_response: Callable, status: int, payload: Any) -> list[bytes]:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        start_response(f"{status} {_STATUS_TEXT.get(status, 'OK')}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
        ])
        return [encoded]

    # ----- 路由表 -----

    def _register(self) -> None:
        r = self.route
        r("POST", "/admin/bootstrap", self.bootstrap, auth=False)
        r("POST", "/institutions", self.create_institution)
        r("POST", "/users", self.create_user)
        r("POST", "/indicators", self.create_indicator)
        r("POST", "/rounds", self.create_round)
        r("POST", "/rounds/close", self.close_round)
        r("POST", "/weights", self.set_weight)

        r("POST", "/submissions", self.submit)
        r("POST", "/submissions/withdraw", self.withdraw)
        r("GET", "/submissions/mine", self.list_mine)
        r("GET", "/staff/submissions", self.staff_submissions)

        r("POST", "/rules", self.draft_rule)
        r("GET", "/rules", self.list_rules)
        r("POST", "/rules/decide", self.decide_rule)

        r("POST", "/publications/request", self.request_publication)
        r("POST", "/publications/decide", self.decide_publication)
        r("GET", "/publications", self.list_publications)
        r("GET", "/publications/explain", self.explain_publication)
        r("POST", "/publications/verify", self.verify_publication)

        r("POST", "/actuals", self.record_actual)
        r("GET", "/errors", self.list_errors)
        r("POST", "/backtests", self.run_backtest)
        r("GET", "/backtests", self.list_backtests)
        r("GET", "/reviews/next-round", self.next_round_reviews)
        r("GET", "/reviews/mine", self.own_reviews)

        r("GET", "/quarantine", self.list_quarantine)
        r("POST", "/quarantine/resolve", self.resolve_quarantine)

        r("GET", "/reminders/due", self.scan_due)
        r("POST", "/reminders/cancel", self.cancel_reminder)
        r("GET", "/resume", self.resume_state)
        r("GET", "/audit", self.audit_log)

    # ----- 处理器 -----

    def bootstrap(self, actor: None, body: dict, query: dict) -> tuple[dict, int]:
        if not body.get("confirm"):
            raise DomainError("引导必须传 confirm=true，且仅允许在无任何用户时使用")
        existing = self.svc.store.query_one("SELECT COUNT(*) AS c FROM users")["c"]
        if existing:
            from .errors import ForbiddenError

            raise ForbiddenError("系统已存在用户，禁止再次引导")
        user = self.svc.create_user(None, body.get("username", "root"),
                                   [ROLE_ADMIN, ROLE_RESEARCH], None)
        return user, 201

    def create_institution(self, actor, body, query):
        return self.svc.create_institution(actor, body["name"]), 201

    def create_user(self, actor, body, query):
        return self.svc.create_user(actor, body["username"], body["roles"],
                                    body.get("institution_id")), 201

    def create_indicator(self, actor, body, query):
        return self.svc.create_indicator(actor, body["code"], body["name"], body["unit"]), 201

    def create_round(self, actor, body, query):
        return self.svc.create_round(
            actor, body["label"], body["opens_at"], body["submission_deadline"],
            body["publish_at"], body["forecast_years"]), 201

    def close_round(self, actor, body, query):
        self.svc.close_round(actor, body["round_id"])
        return {"closed": body["round_id"]}

    def set_weight(self, actor, body, query):
        return self.svc.set_weight(actor, body["indicator_id"],
                                   body["institution_id"], float(body["weight"]))

    def submit(self, actor, body, query):
        result = self.svc.submit_forecast(actor, body)
        return result, 200 if result.get("replayed") else 201

    def withdraw(self, actor, body, query):
        result = self.svc.withdraw_forecast(actor, body)
        return result, 200 if result.get("replayed") else 201

    def list_mine(self, actor, body, query):
        return self.svc.list_own_submissions(
            actor, query.get("round", [None])[0], query.get("indicator", [None])[0],
            _maybe_int(query.get("year", [None])[0]))

    def staff_submissions(self, actor, body, query):
        return self.svc.staff_list_submissions(actor, query.get("round", [None])[0])

    def draft_rule(self, actor, body, query):
        return self.svc.draft_rule(actor, body["indicator_id"], body["params"]), 201

    def list_rules(self, actor, body, query):
        return self.svc.list_rules(actor, query.get("indicator_id", [None])[0])

    def decide_rule(self, actor, body, query):
        return self.svc.decide_rule(actor, body["rule_id"], bool(body["approve"]),
                                    body.get("reason"))

    def request_publication(self, actor, body, query):
        return self.svc.request_publication(
            actor, body["round_id"], body["indicator_id"], int(body["forecast_year"])), 201

    def decide_publication(self, actor, body, query):
        return self.svc.decide_publication(actor, body["publication_id"],
                                           bool(body["approve"]), body.get("reason"))

    def list_publications(self, actor, body, query):
        return self.svc.list_publications(actor, query.get("status", [None])[0])

    def explain_publication(self, actor, body, query):
        pub_id = query.get("id", [None])[0]
        return self.svc.explain_publication(actor, pub_id)

    def verify_publication(self, actor, body, query):
        return self.svc.verify_publication_integrity(actor, body["publication_id"])

    def record_actual(self, actor, body, query):
        return self.svc.record_actual(
            actor, body["indicator_id"], int(body["forecast_year"]),
            float(body["actual_value"]), body["released_at"],
            _maybe_float(body.get("review_threshold"))), 201

    def list_errors(self, actor, body, query):
        return self.svc.list_errors(actor, query.get("indicator_id", [None])[0])

    def run_backtest(self, actor, body, query):
        return self.svc.run_backtest(
            actor, body["rule_version_id"], body["indicator_id"],
            _maybe_int(body.get("forecast_year"))), 201

    def list_backtests(self, actor, body, query):
        return self.svc.list_backtests(actor)

    def next_round_reviews(self, actor, body, query):
        return self.svc.next_round_reviews(actor)

    def own_reviews(self, actor, body, query):
        return self.svc.own_reviews(actor)

    def list_quarantine(self, actor, body, query):
        return self.svc.list_quarantine(actor, query.get("status", ["open"])[0])

    def resolve_quarantine(self, actor, body, query):
        return self.svc.resolve_quarantine(actor, body["quarantine_id"],
                                           body["decision"], body.get("note"))

    def scan_due(self, actor, body, query):
        return self.svc.scan_due(actor)

    def cancel_reminder(self, actor, body, query):
        self.svc.cancel_reminder(actor, body["reminder_id"])
        return {"cancelled": body["reminder_id"]}

    def resume_state(self, actor, body, query):
        return self.svc.resume_state(actor)

    def audit_log(self, actor, body, query):
        return self.svc.audit_log(actor, int(query.get("limit", ["200"])[0]))


def _maybe_int(value: str | None) -> int | None:
    return None if value is None else int(value)


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


_STATUS_TEXT = {
    200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 409: "Conflict", 500: "Internal Server Error",
}
