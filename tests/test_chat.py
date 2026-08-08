from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from ai_info_web.chat import ChatError, ChatProduct, ChatService, DeepSeekChatCompletion, SqliteChatLedger
from ai_info_web.chat_server import create_application


class StaticCatalog:
    def __init__(self, product: ChatProduct | None) -> None:
        self.product = product

    def get(self, slug: str) -> ChatProduct | None:
        return self.product if self.product and slug == self.product.slug else None


class FakeCompletion:
    def __init__(self, reply: str = "这是基于项目资料的回答。") -> None:
        self.reply_text = reply
        self.calls = []

    def reply(self, *, product, message):
        self.calls.append((product, message))
        return self.reply_text


class FailingCompletion:
    def reply(self, *, product, message):
        raise ChatError("provider unavailable")


class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.product = ChatProduct(
            slug="product-0123456789",
            name="Stable Agent",
            summary="A product summary.",
            readme_excerpt="README context.",
            links=("https://github.com/example/stable-agent",),
        )
        self.config = {
            "max_message_characters": 20,
            "max_requests_per_ip_per_hour": 2,
            "monthly_budget_cny": 1.0,
            "reservation_cny": 0.1,
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_known_product_is_answered_without_logging_prompt_or_ip(self) -> None:
        completion = FakeCompletion()
        audit = []
        service = self._service(completion=completion, audit=audit.append)
        response = service.handle(
            method="POST",
            payload={"product_slug": self.product.slug, "message": "它适合什么团队？"},
            client_ip="203.0.113.5",
            now=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(200, response.status)
        self.assertEqual("这是基于项目资料的回答。", response.body["reply"])
        self.assertEqual([(self.product, "它适合什么团队？")], completion.calls)
        self.assertNotIn("它适合什么团队", audit[0])
        self.assertNotIn("203.0.113.5", audit[0])

    def test_unknown_product_and_invalid_message_do_not_consume_quota(self) -> None:
        completion = FakeCompletion()
        service = self._service(completion=completion)
        unknown = service.handle(method="POST", payload={"product_slug": "product-missing", "message": "问题"}, client_ip="203.0.113.5")
        invalid = service.handle(method="POST", payload={"product_slug": self.product.slug, "message": ""}, client_ip="203.0.113.5")

        self.assertEqual(404, unknown.status)
        self.assertEqual(400, invalid.status)
        self.assertEqual([], completion.calls)

    def test_rate_limit_and_budget_are_enforced_before_provider_call(self) -> None:
        completion = FakeCompletion()
        service = self._service(completion=completion, max_requests_per_ip_per_hour=1)
        first = service.handle(method="POST", payload={"product_slug": self.product.slug, "message": "问题"}, client_ip="203.0.113.5")
        second = service.handle(method="POST", payload={"product_slug": self.product.slug, "message": "再问一次"}, client_ip="203.0.113.5")
        budget_service = self._service(completion=FakeCompletion(), monthly_budget_cny=0.05)
        budget = budget_service.handle(method="POST", payload={"product_slug": self.product.slug, "message": "问题"}, client_ip="198.51.100.9")

        self.assertEqual(200, first.status)
        self.assertEqual({"error": "rate_limited"}, second.body)
        self.assertEqual(503, budget.status)
        self.assertEqual({"error": "budget_exhausted"}, budget.body)
        self.assertEqual(1, len(completion.calls))

    def test_provider_failure_returns_generic_error(self) -> None:
        service = self._service(completion=FailingCompletion())
        response = service.handle(method="POST", payload={"product_slug": self.product.slug, "message": "问题"}, client_ip="203.0.113.5")

        self.assertEqual(502, response.status)
        self.assertEqual({"error": "model_unavailable"}, response.body)

    def test_wsgi_adapter_bounds_json_and_allows_only_configured_cors_origin(self) -> None:
        application = create_application(self._service(completion=FakeCompletion()), allowed_origins=("https://site.example.com",))
        captured = {}
        body = '{"product_slug":"product-0123456789","message":"问题"}'.encode("utf-8")

        response = application(
            {
                "REQUEST_METHOD": "POST",
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": BytesIO(body),
                "REMOTE_ADDR": "203.0.113.5",
                "HTTP_ORIGIN": "https://site.example.com",
            },
            lambda status, headers: captured.update(status=status, headers=dict(headers)),
        )

        self.assertTrue(captured["status"].startswith("200"))
        self.assertEqual("https://site.example.com", captured["headers"]["Access-Control-Allow-Origin"])
        self.assertIn("reply", b"".join(response).decode("utf-8"))
        malformed = application(
            {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": "2", "wsgi.input": BytesIO(b"[]"), "REMOTE_ADDR": "203.0.113.5"},
            lambda status, headers: captured.update(status=status, headers=dict(headers)),
        )
        self.assertTrue(captured["status"].startswith("400"))
        self.assertEqual('{"error": "invalid_request"}', b"".join(malformed).decode("utf-8"))

    def test_deepseek_completion_injects_only_the_published_product_context(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return {"choices": [{"message": {"content": "基于资料的回答"}}]}

        completion = DeepSeekChatCompletion(
            token="test-token",
            config={"endpoint": "https://api.deepseek.com/chat/completions", "model": "deepseek-chat", "max_output_tokens": 400, "request_timeout_seconds": 20, "max_retries": 0},
            transport=transport,
        )
        reply = completion.reply(product=self.product, message="它适合什么团队？")

        self.assertEqual("基于资料的回答", reply)
        self.assertIn("Stable Agent", captured["body"])
        self.assertIn("README context.", captured["body"])
        self.assertIn("https://github.com/example/stable-agent", captured["body"])
        self.assertIn("Bearer test-token", captured["headers"]["Authorization"])
        self.assertEqual(20.0, captured["timeout"])

    def _service(self, *, completion, audit=None, **overrides):
        return ChatService(
            catalog=StaticCatalog(self.product),
            ledger=SqliteChatLedger(Path(self.temporary_directory.name) / f"{len(overrides)}.sqlite3"),
            completion=completion,
            config={**self.config, **overrides},
            ip_salt="test-salt",
            audit=audit,
        )


if __name__ == "__main__":
    unittest.main()
