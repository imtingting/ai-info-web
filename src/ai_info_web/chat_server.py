"""Small WSGI adapter for locally validating the chat contract.

CloudBase uses the Node adapter in ``cloudbase/functions/chat`` because its
database SDK provides the production-shared quota ledger. This adapter keeps
the Python API independently executable in local and integration tests.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from wsgiref.simple_server import make_server

from ai_info_web.chat import ChatResponse, ChatService, DeepSeekChatCompletion, JsonProductCatalog, SqliteChatLedger

_MAX_BODY_BYTES = 16_384


def create_application(service: ChatService, *, allowed_origins: tuple[str, ...] = ()) -> Callable:
    """Return a WSGI app that accepts a bounded JSON POST body."""

    def application(environ, start_response):
        origin = environ.get("HTTP_ORIGIN", "")
        headers = [("Content-Type", "application/json; charset=utf-8")]
        if origin and origin in allowed_origins:
            headers.extend(
                [
                    ("Access-Control-Allow-Origin", origin),
                    ("Vary", "Origin"),
                    ("Access-Control-Allow-Headers", "Content-Type"),
                    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                ]
            )
        if environ.get("REQUEST_METHOD", "GET").upper() == "OPTIONS":
            start_response("204 No Content", headers)
            return [b""]
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
            if length < 0 or length > _MAX_BODY_BYTES:
                raise ValueError("invalid content length")
            payload = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            response = ChatResponse(400, {"error": "invalid_request"})
        else:
            response = service.handle(
                method=environ.get("REQUEST_METHOD", "GET"),
                payload=payload,
                client_ip=_client_ip(environ),
                now=datetime.now(timezone.utc),
            )
        body = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        headers.append(("Content-Length", str(len(body))))
        start_response(f"{response.status} {_status_text(response.status)}", headers)
        return [body]

    return application


def service_from_environment() -> ChatService:
    """Build the local adapter from environment variables, never static files."""
    token = os.environ.get("DEEPSEEK_API_KEY")
    salt = os.environ.get("CHAT_IP_HASH_SALT")
    catalog_path = os.environ.get("CHAT_CATALOG_PATH")
    ledger_path = os.environ.get("CHAT_LEDGER_PATH")
    if not all((token, salt, catalog_path, ledger_path)):
        raise RuntimeError("DEEPSEEK_API_KEY, CHAT_IP_HASH_SALT, CHAT_CATALOG_PATH and CHAT_LEDGER_PATH are required")
    config = json.loads((Path(__file__).parents[2] / "config" / "chat_config.json").read_text(encoding="utf-8"))
    return ChatService(
        catalog=JsonProductCatalog(Path(catalog_path)),
        ledger=SqliteChatLedger(Path(ledger_path)),
        completion=DeepSeekChatCompletion(token=token, config=config),
        config=config,
        ip_salt=salt,
        audit=lambda event: print(event, flush=True),
    )


def main() -> None:
    """Run a deliberately local-only server for contract smoke tests."""
    origins = tuple(value.strip() for value in os.environ.get("CHAT_ALLOWED_ORIGINS", "").split(",") if value.strip())
    application = create_application(service_from_environment(), allowed_origins=origins)
    with make_server("127.0.0.1", int(os.environ.get("PORT", "8788")), application) as server:
        server.serve_forever()


def _client_ip(environ) -> str:
    forwarded = str(environ.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or str(environ.get("REMOTE_ADDR") or "unknown")


def _status_text(status: int) -> str:
    return {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Error")


if __name__ == "__main__":
    main()
