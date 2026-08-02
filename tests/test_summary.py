from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_info_web.db import connect, initialize_database, upsert_source_item
from ai_info_web.summary import DeepSeekSummaryProvider, SummaryResponse


class FakeTransport:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, _timeout):
        self.requests.append(request)
        return self.responses.pop(0)


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"
        self.config = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "summary_config.json").read_text()
        )
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_cache_prevents_a_second_request_and_records_monthly_cost(self) -> None:
        transport = FakeTransport(
            [
                self._success(
                    "这是一个用于团队自动化流程的 AI 产品摘要，帮助用户快速理解核心功能、"
                    "适用场景和日常协作方式，并根据公开资料提供简洁可靠的产品说明。"
                )
            ]
        )
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Cached Product", "First description")
            provider = self._provider(transport=transport)
            first = provider.run(connection, run_date=date(2026, 8, 2))
            second = provider.run(connection, run_date=date(2026, 8, 2))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()
            cache_count = connection.execute("SELECT COUNT(*) AS count FROM summary_cache").fetchone()["count"]
            usage = connection.execute("SELECT estimated_cost FROM summary_usage WHERE month = '2026-08'").fetchone()

        self.assertEqual(1, first.generated)
        self.assertEqual(1, second.cache_hits)
        self.assertEqual(1, provider.request_count)
        self.assertEqual(1, len(transport.requests))
        self.assertEqual("ok", product["summary_status"])
        self.assertIn("AI 产品摘要", product["summary_zh"])
        self.assertEqual(1, cache_count)
        self.assertGreater(usage["estimated_cost"], 0.0)

    def test_budget_exhaustion_skips_without_request_or_cache_entry(self) -> None:
        transport = FakeTransport([])
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Budget Product", "Useful description")
            result = self._provider(transport=transport, monthly_budget_cny=0.0).run(
                connection, run_date=date(2026, 8, 2)
            )
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()
            cache_count = connection.execute("SELECT COUNT(*) AS count FROM summary_cache").fetchone()["count"]

        self.assertEqual("degraded", result.status)
        self.assertEqual(1, result.skipped)
        self.assertEqual([], transport.requests)
        self.assertEqual("skipped", product["summary_status"])
        self.assertEqual(0, cache_count)

    def test_failed_completion_is_cached_and_does_not_block_later_products(self) -> None:
        config = {**self.config, "max_retries": 0}
        transport = FakeTransport(
            [
                SummaryResponse(status=500, headers={}, body={}),
                self._success(
                    "这是第二个产品的中文摘要，说明其自动化能力、目标用户、主要使用方式，"
                    "并帮助团队根据公开项目资料判断适用场景和实际价值与落地方式。"
                ),
            ]
        )
        with connect(self.database_path) as connection, connection:
            failed_id = self._product_with_source(connection, "Failed Product", "First description")
            successful_id = self._product_with_source(connection, "Successful Product", "Second description")
            provider = DeepSeekSummaryProvider(
                enabled=True,
                token="test-token",
                monthly_budget_cny=20.0,
                config=config,
                transport=transport,
            )
            result = provider.run(connection, run_date=date(2026, 8, 2))
            failed = connection.execute("SELECT * FROM product WHERE id = ?", (failed_id,)).fetchone()
            successful = connection.execute("SELECT * FROM product WHERE id = ?", (successful_id,)).fetchone()
            failed_cache = connection.execute(
                "SELECT status FROM summary_cache WHERE status = 'failed'"
            ).fetchone()

        self.assertEqual("degraded", result.status)
        self.assertEqual(1, result.failed)
        self.assertEqual(1, result.generated)
        self.assertEqual("failed", failed["summary_status"])
        self.assertEqual("ok", successful["summary_status"])
        self.assertEqual("failed", failed_cache["status"])
        self.assertEqual(2, provider.request_count)

    def test_disabled_or_missing_key_degrades_without_request(self) -> None:
        transport = FakeTransport([])
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Disabled Product", "Description")
            result = DeepSeekSummaryProvider(
                enabled=True,
                token=None,
                monthly_budget_cny=20.0,
                config=self.config,
                transport=transport,
            ).run(connection, run_date=date(2026, 8, 2))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()

        self.assertEqual("degraded", result.status)
        self.assertEqual(1, result.skipped)
        self.assertEqual([], transport.requests)
        self.assertEqual("skipped", product["summary_status"])

    def test_timeout_marks_only_the_affected_product_as_failed(self) -> None:
        class TimeoutTransport:
            def __call__(self, _request, _timeout):
                raise TimeoutError("timed out")

        config = {**self.config, "max_retries": 0}
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Timeout Product", "Description")
            result = DeepSeekSummaryProvider(
                enabled=True,
                token="test-token",
                monthly_budget_cny=20.0,
                config=config,
                transport=TimeoutTransport(),
            ).run(connection, run_date=date(2026, 8, 2))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()

        self.assertEqual("degraded", result.status)
        self.assertEqual(1, result.failed)
        self.assertEqual("failed", product["summary_status"])

    def test_overlong_completion_is_trimmed_to_the_public_summary_budget(self) -> None:
        overlong_summary = (
            "这是一个面向产品团队的 AI 情报工具，用于汇集公开项目资料、完成分类和热度分析，"
            "帮助用户快速筛选值得关注的新产品与开源项目，并提供可追溯的来源链接和简明信息。"
        )
        transport = FakeTransport([self._success(overlong_summary)])
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Length Product", "Useful description")
            result = self._provider(transport=transport).run(connection, run_date=date(2026, 8, 2))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual("ok", product["summary_status"])
        self.assertGreaterEqual(len(product["summary_zh"]), 60)
        self.assertLessEqual(len(product["summary_zh"]), 100)
        self.assertTrue(product["summary_zh"].endswith(("。", "！", "？", "；", "…")))

    def _provider(self, *, transport, monthly_budget_cny: float = 20.0):
        return DeepSeekSummaryProvider(
            enabled=True,
            token="test-token",
            monthly_budget_cny=monthly_budget_cny,
            config=self.config,
            transport=transport,
        )

    def _product_with_source(self, connection, name: str, description: str) -> int:
        product_id = connection.execute(
            "INSERT INTO product(name, first_seen_at, last_updated_at) VALUES (?, '2026-08-02T00:00:00+00:00', '2026-08-02T00:00:00+00:00')",
            (name,),
        ).lastrowid
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id=name,
            name=name,
            description=description,
            url=f"https://github.com/example/{name}",
            raw_json={},
            observed_at="2026-08-02T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
            (product_id, source_item_id),
        )
        return product_id

    def _success(self, summary: str) -> SummaryResponse:
        return SummaryResponse(
            status=200,
            headers={},
            body={
                "choices": [{"message": {"content": summary}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )


if __name__ == "__main__":
    unittest.main()
