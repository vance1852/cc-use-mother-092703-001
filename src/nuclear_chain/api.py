"""无第三方依赖的核燃料循环批次监管 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from .errors import BatchError, ValidationFailed
from .service import BatchService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: BatchService) -> None:
        self.service = service

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
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = target.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "nuclear-chain"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    service.create_user(
                        payload["user_id"], payload["display_name"], payload["role"]
                    ),
                )
            if method == "POST" and path == "/batches":
                return Response(201, service.register_batch(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                return Response(200, service.get_batch(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "declarations":
                return Response(
                    201,
                    service.correct_declaration(
                        actor,
                        parts[1],
                        int(payload["expected_revision"]),
                        payload["reason"],
                        payload,
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "tests":
                return Response(
                    201,
                    service.record_test(
                        actor,
                        parts[1],
                        payload["idempotency_key"],
                        payload["method"],
                        payload["instrument"],
                        payload["results"],
                        payload["verdict"],
                        payload["basis_doc"],
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "decisions":
                return Response(
                    201,
                    service.decide(
                        actor,
                        parts[1],
                        payload["decision"],
                        payload["reason"],
                        payload.get("test_id"),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "handoffs":
                return Response(201, service.handoff(actor, {**payload, "batch_id": parts[1]}))
            if method == "POST" and path == "/splits":
                return Response(201, service.split_batch(actor, payload))
            if method == "POST" and path == "/merges":
                return Response(201, service.merge_batches(actor, payload))
            if method == "POST" and path == "/dispositions":
                return Response(201, service.dispose(actor, payload))
            if method == "POST" and path == "/manifests/import":
                return Response(201, service.import_manifest(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "trace":
                return Response(200, service.trace(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "lineage":
                return Response(200, service.lineage(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except BatchError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NuclearChain/1"

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
    parser = argparse.ArgumentParser(description="启动核燃料循环批次监管服务")
    parser.add_argument("--database", type=Path, default=Path("nuclear_chain.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(BatchService(connection))))
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
