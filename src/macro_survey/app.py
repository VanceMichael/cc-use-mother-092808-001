"""应用装配与本地启动入口。

用法：
    python -m src.macro_survey.app --db ./survey.db [--port 8080]
"""

from __future__ import annotations

import argparse
import os
from wsgiref.simple_server import make_server

from .api import HttpApi
from .service import SurveyService
from .store import Store


def build_app(db_path: str) -> tuple[HttpApi, Store]:
    store = Store(db_path)
    service = SurveyService(store)
    return HttpApi(service), store


def main() -> None:
    parser = argparse.ArgumentParser(description="宏观预测征集与发布后端")
    parser.add_argument("--db", default=os.environ.get("SURVEY_DB", "survey.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    app, store = build_app(args.db)
    httpd = make_server(args.host, args.port, app)
    print(f"macro-survey listening on http://{args.host}:{args.port} (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
