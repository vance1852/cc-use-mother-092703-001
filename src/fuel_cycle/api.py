"""无第三方依赖的核燃料循环批次监管 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .errors import BatchError, ValidationFailed
from .service import FuelCycleService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    """将 HTTP 路由映射到批次监管领域服务，便于无网络单元测试。"""

    def __init__(self, service: FuelCycleService) -> None:
        self.service = service
        # 共享连接跨工作线程使用时，用应用层锁串行化全部请求。
        self._lock = threading.Lock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        with self._lock:
            return self._route(method, target, headers, body)

    def _route(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = target.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "fuel-cycle-traceability"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                ))
            if method == "POST" and path == "/declarations":
                key = normalized.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                return Response(201, self.service.register_declaration(
                    self._actor(normalized), payload, key
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "corrections":
                return Response(201, {
                    "declarations": self.service.correct_declaration(
                        self._actor(normalized), parts[1], payload
                    )
                })
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                self._actor(normalized)
                return Response(200, self.service.get_batch(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "declarations":
                return Response(200, self.service.get_declaration_history(self._actor(normalized), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "split":
                return Response(201, self.service.split_batch(
                    self._actor(normalized), payload["transform_id"], parts[1],
                    payload["parts"], payload.get("note", ""),
                ))
            if method == "POST" and path == "/merges":
                return Response(201, self.service.merge_batches(
                    self._actor(normalized), payload["transform_id"],
                    payload["parent_ids"], payload["outputs"], payload.get("note", ""),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "inspections":
                return Response(201, self.service.record_inspection(
                    self._actor(normalized), parts[1], payload["test_type"], payload["lab"],
                    payload["method"], payload["results"], payload.get("limits", {}),
                    payload.get("sampled_at"), payload.get("tested_at"),
                    payload.get("verdict"), payload.get("conclusion", ""),
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "inspections":
                return Response(200, self.service.list_inspections(self._actor(normalized), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "decisions":
                return Response(201, self.service.record_decision(
                    self._actor(normalized), parts[1], payload["decision"], payload["reason"],
                    payload.get("basis_test_id"),
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "decisions":
                return Response(200, self.service.list_decisions(self._actor(normalized), parts[1]))
            if method == "POST" and path == "/transfers":
                return Response(201, self.service.dispatch_transfer(
                    self._actor(normalized), payload["transfer_id"], payload["batch_id"],
                    payload["from_party"], payload["to_party"],
                    payload["from_location"], payload["to_location"], payload.get("manifest_ref", ""),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "transfers" and parts[2] == "receive":
                return Response(200, self.service.receive_transfer(
                    self._actor(normalized), parts[1], payload.get("note", "")
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "transfers":
                self._actor(normalized)
                return Response(200, self.service.get_transfer(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "transfers":
                return Response(200, self.service.list_transfers(self._actor(normalized), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "disposals":
                return Response(201, self.service.dispose_batch(
                    self._actor(normalized), parts[1], payload["method"],
                    payload["authority_doc_ref"], payload["reason"], payload["witness"],
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "trace":
                return Response(200, self.service.trace_batch(self._actor(normalized), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "impact":
                return Response(200, self.service.impact_analysis(self._actor(normalized), parts[1]))
            if method == "POST" and path == "/audit/verify":
                return Response(200, self.service.verify_chain(self._actor(normalized)))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except BatchError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FuelCycleTrace/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动核燃料循环批次监管 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("fuel_cycle.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    application = JsonApplication(FuelCycleService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
