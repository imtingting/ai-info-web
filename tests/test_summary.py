from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_info_web.db import connect, initialize_database, update_source_enrichment, upsert_source_item
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
                self._success(self._analysis())
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
        self.assertIn("可追溯的产品档案", product["summary_zh"])
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

    def test_max_items_bounds_only_uncached_model_requests(self) -> None:
        transport = FakeTransport([self._success(self._analysis())])
        with connect(self.database_path) as connection, connection:
            first_id = self._product_with_source(connection, "First Product", "First description")
            second_id = self._product_with_source(connection, "Second Product", "Second description")
            result = self._provider(transport=transport).run(
                connection, run_date=date(2026, 8, 2), max_items=1
            )
            first = connection.execute("SELECT * FROM product WHERE id = ?", (first_id,)).fetchone()
            second = connection.execute("SELECT * FROM product WHERE id = ?", (second_id,)).fetchone()

        self.assertEqual(1, result.generated)
        self.assertEqual(1, result.request_count)
        self.assertEqual(1, len(transport.requests))
        self.assertEqual("ok", first["summary_status"])
        self.assertEqual("pending", second["summary_status"])

    def test_failed_completion_is_cached_and_does_not_block_later_products(self) -> None:
        config = {**self.config, "max_retries": 0}
        transport = FakeTransport(
            [
                SummaryResponse(status=500, headers={}, body={}),
                self._success(self._analysis("第二个产品")),
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

    def test_overlong_completion_is_trimmed_to_the_analysis_budget(self) -> None:
        overlong_summary = self._analysis() * 3
        transport = FakeTransport([self._success(overlong_summary)])
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "Length Product", "Useful description")
            result = self._provider(transport=transport).run(connection, run_date=date(2026, 8, 2))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual("ok", product["summary_status"])
        self.assertGreaterEqual(len(product["summary_zh"]), 150)
        self.assertLessEqual(len(product["summary_zh"]), 300)
        self.assertTrue(product["summary_zh"].endswith(("。", "！", "？", "；", "…")))

    def test_readme_is_in_prompt_and_invalidates_the_cached_summary(self) -> None:
        transport = FakeTransport([self._success(self._analysis()), self._success(self._analysis("更新后的项目"))])
        with connect(self.database_path) as connection, connection:
            product_id = self._product_with_source(connection, "README Product", "Useful description")
            source_item_id = connection.execute(
                "SELECT source_item_id FROM product_source WHERE product_id = ?", (product_id,)
            ).fetchone()["source_item_id"]
            update_source_enrichment(connection, source_item_id=source_item_id, readme_text="# README Product\nFirst implementation detail")
            provider = self._provider(transport=transport)
            first = provider.run(connection, run_date=date(2026, 8, 2))
            update_source_enrichment(connection, source_item_id=source_item_id, readme_text="# README Product\nChanged implementation detail")
            second = provider.run(connection, run_date=date(2026, 8, 2))

        first_request = json.loads(transport.requests[0].data.decode("utf-8"))
        self.assertEqual(1, first.generated)
        self.assertEqual(1, second.generated)
        self.assertEqual(2, provider.request_count)
        self.assertIn("README 摘要", first_request["messages"][1]["content"])
        self.assertIn("First implementation detail", first_request["messages"][1]["content"])
        self.assertIn("项目是什么", first_request["messages"][1]["content"])

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

    @staticmethod
    def _analysis(name: str = "这个项目") -> str:
        return (
            f"{name} 是一个面向团队协作的 AI 工具，用于把分散的公开项目信息整理成可追溯的产品档案。"
            "它解决了研发和产品人员需要在多个来源之间反复核对功能、活跃度与适用边界的问题，让初步判断更快完成。"
            "项目通过采集仓库描述、README 和公开指标生成结构化分析，用户可继续查看原始链接核实细节。"
            "它受到关注的原因在于同时覆盖发现、比较与证据回溯，适合需要持续追踪 AI 工具变化的团队。"
        )


if __name__ == "__main__":
    unittest.main()
