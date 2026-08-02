"""Provider-neutral chat contract and local quota enforcement for product discussions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ChatProduct:
    slug: str
    name: str
    summary: str
    readme_excerpt: str
    links: tuple[str, ...]


@dataclass(frozen=True)
class ChatResponse:
    status: int
    body: dict[str, Any]


class ProductCatalog(Protocol):
    def get(self, slug: str) -> ChatProduct | None: ...


class ChatLedger(Protocol):
    def reserve(self, *, ip_hash: str, timestamp: datetime, reservation_cny: float, max_requests: int, monthly_budget_cny: float) -> str | None: ...


class ChatCompletion(Protocol):
    def reply(self, *, product: ChatProduct, message: str) -> str: ...


class JsonProductCatalog:
    """Read the generated public product catalog; unknown slugs are rejected."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, slug: str) -> ChatProduct | None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for product in payload.get("products", []):
            if product.get("slug") != slug:
                continue
            context = product.get("chat_context") if isinstance(product.get("chat_context"), dict) else {}
            links = tuple(value for value in context.get("links", []) if isinstance(value, str))
            return ChatProduct(
                slug=slug,
                name=str(product.get("name") or ""),
                summary=str(product.get("summary") or ""),
                readme_excerpt=str(context.get("readme_excerpt") or "")[:2000],
                links=links[:4],
            )
        return None


class SqliteChatLedger:
    """Durable local ledger used for tests and any non-CloudBase deployment."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_ip_window (
                  window TEXT NOT NULL,
                  ip_hash TEXT NOT NULL,
                  request_count INTEGER NOT NULL,
                  PRIMARY KEY(window, ip_hash)
                );
                CREATE TABLE IF NOT EXISTS chat_monthly_usage (
                  month TEXT PRIMARY KEY,
                  reserved_cny REAL NOT NULL
                );
                """
            )

    def reserve(self, *, ip_hash: str, timestamp: datetime, reservation_cny: float, max_requests: int, monthly_budget_cny: float) -> str | None:
        hour = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        month = timestamp.astimezone(timezone.utc).strftime("%Y-%m")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT request_count FROM chat_ip_window WHERE window = ? AND ip_hash = ?", (hour, ip_hash)
            ).fetchone()
            if count is not None and int(count[0]) >= max_requests:
                connection.execute("ROLLBACK")
                return "rate_limited"
            usage = connection.execute("SELECT reserved_cny FROM chat_monthly_usage WHERE month = ?", (month,)).fetchone()
            current_usage = float(usage[0]) if usage is not None else 0.0
            if current_usage + reservation_cny > monthly_budget_cny:
                connection.execute("ROLLBACK")
                return "budget_exhausted"
            connection.execute(
                """
                INSERT INTO chat_ip_window(window, ip_hash, request_count) VALUES (?, ?, 1)
                ON CONFLICT(window, ip_hash) DO UPDATE SET request_count = request_count + 1
                """,
                (hour, ip_hash),
            )
            connection.execute(
                """
                INSERT INTO chat_monthly_usage(month, reserved_cny) VALUES (?, ?)
                ON CONFLICT(month) DO UPDATE SET reserved_cny = reserved_cny + excluded.reserved_cny
                """,
                (month, reservation_cny),
            )
            connection.execute("COMMIT")
        return None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None)


class DeepSeekChatCompletion:
    """Small DeepSeek client; its token stays in the server process only."""

    def __init__(self, *, token: str, config: Mapping[str, Any], transport: Callable[[Request, float], Mapping[str, Any]] | None = None) -> None:
        self.token = token
        self.config = config
        self.transport = transport or _urlopen_json

    def reply(self, *, product: ChatProduct, message: str) -> str:
        prompt = _chat_prompt(product, message)
        request = Request(
            str(self.config["endpoint"]),
            data=json.dumps(
                {
                    "model": self.config["model"],
                    "messages": [
                        {"role": "system", "content": "You answer product questions in factual Simplified Chinese."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": int(self.config["max_output_tokens"]),
                }
            ).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error = "DeepSeek request failed"
        for attempt in range(int(self.config["max_retries"]) + 1):
            try:
                response = self.transport(request, float(self.config["request_timeout_seconds"]))
                text = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if text:
                    return text[:4000]
                raise ChatError("DeepSeek response did not contain content")
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, ChatError) as error:
                last_error = str(error)
                if attempt < int(self.config["max_retries"]):
                    time.sleep(attempt + 1)
        raise ChatError(last_error)


class ChatService:
    """Validate a narrow JSON API before calling a model or recording any usage."""

    def __init__(self, *, catalog: ProductCatalog, ledger: ChatLedger, completion: ChatCompletion, config: Mapping[str, Any], ip_salt: str, audit: Callable[[str], None] | None = None) -> None:
        self.catalog = catalog
        self.ledger = ledger
        self.completion = completion
        self.config = config
        self.ip_salt = ip_salt
        self.audit = audit or (lambda _message: None)

    def handle(self, *, method: str, payload: Mapping[str, Any], client_ip: str, now: datetime | None = None) -> ChatResponse:
        if method.upper() != "POST":
            return ChatResponse(405, {"error": "method_not_allowed"})
        slug = payload.get("product_slug")
        message = payload.get("message")
        if not isinstance(slug, str) or not isinstance(message, str) or not message.strip():
            return ChatResponse(400, {"error": "invalid_request"})
        if len(message) > int(self.config["max_message_characters"]):
            return ChatResponse(400, {"error": "message_too_long"})
        product = self.catalog.get(slug)
        if product is None:
            return ChatResponse(404, {"error": "unknown_product"})
        timestamp = now or datetime.now(timezone.utc)
        reason = self.ledger.reserve(
            ip_hash=_hash_ip(client_ip, self.ip_salt),
            timestamp=timestamp,
            reservation_cny=float(self.config["reservation_cny"]),
            max_requests=int(self.config["max_requests_per_ip_per_hour"]),
            monthly_budget_cny=float(self.config["monthly_budget_cny"]),
        )
        if reason == "rate_limited":
            return ChatResponse(429, {"error": reason})
        if reason == "budget_exhausted":
            return ChatResponse(503, {"error": reason})
        try:
            reply = self.completion.reply(product=product, message=message.strip())
        except ChatError:
            self.audit(f"chat_failed product={product.slug} message_chars={len(message)}")
            return ChatResponse(502, {"error": "model_unavailable"})
        self.audit(f"chat_ok product={product.slug} message_chars={len(message)} reply_chars={len(reply)}")
        return ChatResponse(200, {"reply": reply})


class ChatError(RuntimeError):
    """A request-scoped model failure with no user prompt in its message."""


def _chat_prompt(product: ChatProduct, message: str) -> str:
    links = "\n".join(f"- {link}" for link in product.links)
    return (
        "仅依据以下项目资料回答；若资料不足，请明确说明未知，不要编造。\n"
        f"项目名称：{product.name}\n项目摘要：{product.summary}\nREADME 截要：{product.readme_excerpt or '未提供'}\n"
        f"项目链接：\n{links}\n\n用户问题：{message}"
    )


def _hash_ip(client_ip: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{client_ip}".encode("utf-8")).hexdigest()


def _urlopen_json(request: Request, timeout: float) -> Mapping[str, Any]:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured API endpoint
        return json.loads(response.read().decode("utf-8"))
