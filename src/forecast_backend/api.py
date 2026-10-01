"""HTTP JSON API 适配层：仅做协议转换，业务规则全部在 ForecastSystem。

操作者身份经请求头传递：X-Actor-Id、X-Actor-Role。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from .models import Actor, DomainError
from .services import ForecastSystem

# (方法, 路径模式, 处理函数名)。路径参数形如 {name}。
ROUTES: list[tuple[str, str, str]] = [
    ("POST", "/institutions", "register_institution"),
    ("POST", "/rounds", "open_round"),
    ("POST", "/rounds/{round_id}/close", "close_round"),
    ("POST", "/submissions", "submit_forecast"),
    ("POST", "/submissions/withdraw", "withdraw_forecast"),
    ("GET", "/submissions", "list_own_submissions"),
    ("GET", "/revision-history", "revision_history"),
    ("GET", "/samples", "list_round_samples"),
    ("GET", "/consensus", "consensus_snapshot"),
    ("POST", "/rules", "propose_rule"),
    ("POST", "/rules/{rule_id}/versions/{version}/submit", "submit_rule_for_approval"),
    ("POST", "/rules/{rule_id}/versions/{version}/approve", "approve_rule"),
    ("POST", "/rules/{rule_id}/versions/{version}/reject", "reject_rule"),
    ("POST", "/publications/compute", "compute_publication"),
    ("POST", "/publications/{publication_id}/publish", "publish"),
    ("POST", "/publications/{publication_id}/reject", "reject_publication"),
    ("GET", "/publications/{publication_id}", "get_publication"),
    ("GET", "/publications/{publication_id}/explain", "explain_publication"),
    ("GET", "/watermark", "watermark"),
    ("POST", "/actuals", "record_actual"),
    ("GET", "/errors", "list_errors"),
    ("POST", "/backtests", "run_backtest"),
    ("GET", "/review-flags", "list_review_flags"),
    ("POST", "/review-flags/{flag_id}/resolve", "resolve_review_flag"),
    ("GET", "/reminders/due", "due_reminders"),
    ("POST", "/reminders/recover", "recover"),
    ("GET", "/approvals/pending", "pending_approvals"),
    ("GET", "/events", "list_events"),
    ("GET", "/quarantine", "list_quarantine"),
]

_INT_PARAMS = {"target_year", "version", "flag_id"}
_FLOAT_PARAMS = {"value", "lower", "upper", "confidence"}


def _compile(pattern: str) -> re.Pattern[str]:
    regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
    return re.compile(f"^{regex}$")


class _Api:
    """把 HTTP 请求映射到 ForecastSystem 方法。"""

    def __init__(self, system: ForecastSystem) -> None:
        self.system = system
        self.routes = [
            (method, _compile(pattern), getattr(self, name)) for method, pattern, name in ROUTES
        ]

    # -- 端点实现：kwargs 来自路径参数与请求体/查询串的合并 --
    def register_institution(self, actor, p, q):
        return self.system.register_institution(actor, **p)

    def open_round(self, actor, p, q):
        return self.system.open_round(actor, **p)

    def close_round(self, actor, p, q):
        return self.system.close_round(actor, **p)

    def submit_forecast(self, actor, p, q):
        return self.system.submit_forecast(actor, **p)

    def withdraw_forecast(self, actor, p, q):
        return self.system.withdraw_forecast(actor, **p)

    def list_own_submissions(self, actor, p, q):
        return self.system.list_own_submissions(actor, **p)

    def revision_history(self, actor, p, q):
        return self.system.revision_history(actor, **p)

    def list_round_samples(self, actor, p, q):
        return self.system.list_round_samples(actor, **p)

    def consensus_snapshot(self, actor, p, q):
        return self.system.consensus_snapshot(actor, **p)

    def propose_rule(self, actor, p, q):
        return self.system.propose_rule(actor, **p)

    def submit_rule_for_approval(self, actor, p, q):
        return self.system.submit_rule_for_approval(actor, **p)

    def approve_rule(self, actor, p, q):
        return self.system.approve_rule(actor, **p)

    def reject_rule(self, actor, p, q):
        return self.system.reject_rule(actor, **p)

    def compute_publication(self, actor, p, q):
        return self.system.compute_publication(actor, **p)

    def publish(self, actor, p, q):
        return self.system.publish(actor, **p)

    def reject_publication(self, actor, p, q):
        return self.system.reject_publication(actor, **p)

    def get_publication(self, actor, p, q):
        return self.system.get_publication(actor, **p)

    def explain_publication(self, actor, p, q):
        return self.system.explain_publication(actor, **p)

    def watermark(self, actor, p, q):
        return self.system.watermark(actor, **p)

    def record_actual(self, actor, p, q):
        return self.system.record_actual(actor, **p)

    def list_errors(self, actor, p, q):
        return self.system.list_errors(actor, **p)

    def run_backtest(self, actor, p, q):
        return self.system.run_backtest(actor, **p)

    def list_review_flags(self, actor, p, q):
        return self.system.list_review_flags(actor, **p)

    def resolve_review_flag(self, actor, p, q):
        return self.system.resolve_review_flag(actor, **p)

    def due_reminders(self, actor, p, q):
        return self.system.due_reminders(actor)

    def recover(self, actor, p, q):
        return self.system.recover()

    def pending_approvals(self, actor, p, q):
        return self.system.pending_approvals()

    def list_events(self, actor, p, q):
        return self.system.list_events(actor, **p)

    def list_quarantine(self, actor, p, q):
        return self.system.list_quarantine(actor)


def _coerce(params: dict[str, Any]) -> dict[str, Any]:
    converted = {}
    for key, value in params.items():
        if value is None:
            converted[key] = None
        elif key in _INT_PARAMS:
            converted[key] = int(value)
        elif key in _FLOAT_PARAMS:
            converted[key] = float(value)
        else:
            converted[key] = value
    return converted


def make_handler(system: ForecastSystem) -> type[BaseHTTPRequestHandler]:
    api = _Api(system)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _actor(self) -> Actor:
            actor_id = self.headers.get("X-Actor-Id", "")
            role = self.headers.get("X-Actor-Role", "")
            try:
                return Actor(actor_id=actor_id, role=role)
            except ValueError as exc:
                raise DomainError(str(exc), code="unauthenticated", http_status=401) from exc

        def _dispatch(self, method: str) -> None:
            try:
                path, _, query = self.path.partition("?")
                for route_method, regex, handler in api.routes:
                    if route_method != method:
                        continue
                    match = regex.match(path)
                    if match is None:
                        continue
                    path_params = match.groupdict()
                    if method == "POST":
                        length = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(length) or b"{}")
                        if not isinstance(body, dict):
                            raise DomainError("请求体必须是 JSON 对象", code="invalid_request")
                        params = {**body, **path_params}
                    else:
                        params = {
                            key: values[0]
                            for key, values in parse_qs(query).items()
                        }
                        params.update(path_params)
                    result = handler(self._actor(), _coerce(params), {})
                    self._respond(200, result)
                    return
                self._respond(404, {"error": "not_found", "message": "路由不存在"})
            except DomainError as exc:
                self._respond(
                    exc.http_status,
                    {"error": exc.code, "message": str(exc), "details": exc.details},
                )
            except (json.JSONDecodeError, ValueError) as exc:
                self._respond(400, {"error": "invalid_request", "message": str(exc)})
            except TypeError as exc:
                self._respond(400, {"error": "invalid_request", "message": f"参数错误: {exc}"})
            except Exception as exc:  # pragma: no cover - 兜底
                self._respond(500, {"error": "internal", "message": str(exc)})

        def _respond(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def create_server(
    system: ForecastSystem, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(system))


def main() -> None:  # pragma: no cover - 手工运行入口
    import argparse

    parser = argparse.ArgumentParser(description="宏观预测征集与发布后端")
    parser.add_argument("--db", default="forecast.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    system = ForecastSystem(args.db)
    recovered = system.recover()
    print(
        f"启动恢复：触发 {len(recovered['fired_reminders'])} 条到期提醒，"
        f"待审批规则 {len(recovered['pending_approvals']['rules'])} 项，"
        f"待审批发布 {len(recovered['pending_approvals']['publications'])} 项"
    )
    server = create_server(system, args.host, args.port)
    print(f"监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        system.close()


if __name__ == "__main__":  # pragma: no cover
    main()
