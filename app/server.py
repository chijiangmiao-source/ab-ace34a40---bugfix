"""HTTP 服务: 真实 REST API + 静态页面 (仅依赖 Python 标准库)。

接口
----
GET  /                         页面 (由 scripts/build_page.py 构建到 web/dist)
GET  /api/health               健康检查 (反映持久层/接口可用性)
GET  /api/state                规程完整状态 + 最近裁决
POST /api/facts                {"id": "f1"}                       新建事实
POST /api/rules                {"id","conclusion","antecedents"}  新建规则
POST /api/retract              {"fact_id": "f1"}                  撤回事实 (事务传播)
GET  /api/conclusions/<id>     单个结论的完整依据
"""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import tms as tms_mod
from .store import Store

DIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "dist")

# 引擎校验错误 -> HTTP 400 + 稳定错误码
ERROR_CODES = [
    (tms_mod.UnknownFactError, "unknown_fact", HTTPStatus.BAD_REQUEST),
    (tms_mod.DanglingRuleError, "dangling_reference", HTTPStatus.BAD_REQUEST),
    (tms_mod.CyclicRuleError, "cyclic_rule", HTTPStatus.BAD_REQUEST),
    (tms_mod.DuplicateIdError, "duplicate_id", HTTPStatus.BAD_REQUEST),
    (tms_mod.TMSError, "invalid_procedure", HTTPStatus.BAD_REQUEST),
]


class AppState:
    """进程级共享状态 (Store 内部已加锁, last_verdict 另加锁)。"""

    def __init__(self, db_path: str) -> None:
        self.store = Store(db_path)
        self._verdict_lock = threading.Lock()
        self.last_verdict: Optional[dict] = None

    def set_verdict(self, verdict: dict) -> None:
        with self._verdict_lock:
            self.last_verdict = verdict

    def get_verdict(self) -> Optional[dict]:
        with self._verdict_lock:
            return None if self.last_verdict is None else json.loads(
                json.dumps(self.last_verdict))


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    store = state.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "SafetyTMS/1.0"

        # ------------------------------------------------------------ #
        # 工具
        # ------------------------------------------------------------ #
        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求体必须是 JSON 对象")
            return data

        def _error(self, exc: Exception) -> None:
            for exc_type, code, status in ERROR_CODES:
                if isinstance(exc, exc_type):
                    self._json({"error": code, "message": str(exc)}, status)
                    return
            # 未预期错误: 返回 500 JSON, 不让连接裸断
            print(f"[server] 未预期错误: {type(exc).__name__}: {exc}", flush=True)
            self._json({"error": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
            prefix = os.environ.get("LOG_PREFIX", "[http] ")
            print(f"{prefix}{self.address_string()} {fmt % args}", flush=True)

        # ------------------------------------------------------------ #
        # 路由
        # ------------------------------------------------------------ #
        def do_GET(self) -> None:  # noqa: N802
            try:
                path = urlparse(self.path).path
                if path == "/api/health":
                    db_ok = store.ping()
                    snap = store.snapshot() if db_ok else {"facts": [], "rules": []}
                    self._json({
                        "status": "ok" if db_ok else "degraded",
                        "database": "ok" if db_ok else "unavailable",
                        "facts": len(snap["facts"]),
                        "rules": len(snap["rules"]),
                    })
                elif path == "/api/state":
                    snap = store.snapshot()
                    snap["last_verdict"] = state.get_verdict() or store.latest_verdict()
                    self._json(snap)
                elif path.startswith("/api/conclusions/"):
                    node = path.rsplit("/", 1)[-1]
                    st = store.node(node)
                    if st is None or st.is_fact:
                        raise _HttpError(HTTPStatus.NOT_FOUND, "unknown_conclusion",
                                         f"结论 {node!r} 不存在")
                    self._json({
                        "id": node,
                        "status": st.status,
                        "reason": st.reason,
                        "supports": [store.support_dict(s) for s in st.supports],
                        "retired_supports": [store.support_dict(s)
                                             for s in st.retired_supports],
                    })
                else:
                    self._serve_static(path)
            except _HttpError as e:
                self._json({"error": e.code, "message": e.message}, e.status)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = urlparse(self.path).path
                data = self._read_json()
                if path == "/api/facts":
                    fact_id = str(data.get("id", "")).strip()
                    store.add_fact(fact_id)
                    self._json({"ok": True, "fact": fact_id}, HTTPStatus.CREATED)
                elif path == "/api/rules":
                    rule_id = str(data.get("id", "")).strip()
                    conclusion = str(data.get("conclusion", "")).strip()
                    ants = data.get("antecedents", [])
                    if not isinstance(ants, list) or not all(isinstance(a, str) for a in ants):
                        raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid_procedure",
                                         "antecedents 必须是字符串数组")
                    rule = store.add_rule(rule_id, conclusion, ants)
                    self._json({"ok": True, "rule": {
                        "id": rule.rule_id,
                        "conclusion": rule.conclusion,
                        "antecedents": list(rule.antecedents),
                    }}, HTTPStatus.CREATED)
                elif path == "/api/retract":
                    fact_id = str(data.get("fact_id", "")).strip()
                    record = store.retract_fact(fact_id)
                    payload = Store._record_to_dict(record)
                    state.set_verdict(payload)
                    self._json({"ok": True, "verdict": payload,
                                "state": store.snapshot()})
                else:
                    raise _HttpError(HTTPStatus.NOT_FOUND, "not_found", f"未知路径 {path}")
            except _HttpError as e:
                self._json({"error": e.code, "message": e.message}, e.status)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        # ------------------------------------------------------------ #
        # 静态页面
        # ------------------------------------------------------------ #
        def _serve_static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            # 防目录穿越
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                raise _HttpError(HTTPStatus.FORBIDDEN, "forbidden", "非法路径")
            full = os.path.join(DIST_DIR, rel)
            if not os.path.isfile(full):
                full = os.path.join(DIST_DIR, "index.html")  # 单页应用回退
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def seed_demo(state: AppState) -> None:
    """空库时写入安全员演示规程: 结论含两条独立支持路径, 下游仅依赖该结论。"""
    store = state.store
    if store.tms.list_facts() or store.tms.list_rules():
        return
    demo_facts = ["manual_alarm_ok", "smoke_detector_ok", "sprinkler_pressure_ok"]
    demo_rules = [
        ("r_alarm_manual", "fire_confirmed", ["manual_alarm_ok"]),
        ("r_alarm_auto", "fire_confirmed",
         ["smoke_detector_ok", "sprinkler_pressure_ok"]),
        ("r_evacuate", "must_evacuate", ["fire_confirmed"]),
    ]
    for f in demo_facts:
        store.add_fact(f)
    for rid, concl, ants in demo_rules:
        store.add_rule(rid, concl, ants)
    print("[server] 已写入演示规程 (两条独立支持路径)", flush=True)


def serve(host: str, port: int, db_path: str) -> None:
    state = AppState(db_path)
    if os.environ.get("SEED_DEMO") == "1":
        seed_demo(state)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"[server] 安全规程 TMS 监听 http://{host}:{port} (db={db_path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.store.close()


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "/data/procedure.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    serve(host, port, db_path)


if __name__ == "__main__":
    main()
