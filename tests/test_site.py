from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ai_info_web.db import (
    connect,
    initialize_database,
    update_source_enrichment,
    upsert_metric_snapshot,
    upsert_rank_history,
    upsert_source_item,
)
from ai_info_web.heat import record_rank_history
from ai_info_web.site import PublicationError, build_static_site, publish_site, stable_slug, verify_static_site


class StaticSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "state.sqlite3"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_build_exports_filterable_list_and_stable_detail_page(self) -> None:
        with connect(self.database_path) as connection, connection:
            product_id, source_item_id = self._product_with_source(connection)
            upsert_metric_snapshot(
                connection, source_item_id=source_item_id, snapshot_date=date(2026, 8, 2), stars=120, forks=8
            )
            record_rank_history(connection, listed_at=datetime(2026, 8, 2, tzinfo=timezone.utc))
            product = connection.execute("SELECT * FROM product WHERE id = ?", (product_id,)).fetchone()
            source = connection.execute("SELECT * FROM source_item WHERE id = ?", (source_item_id,)).fetchone()
            slug_before = stable_slug(product, [source])
            renamed_product = {**dict(product), "name": "Renamed Primary Source"}
            self.assertEqual(slug_before, stable_slug(renamed_product, [source]))
            output = self.root / "batch"
            result = build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))

        self.assertEqual({"products": 1, "details": 1}, result)
        self.assertTrue((output / "index.html").is_file())
        self.assertTrue((output / "products" / slug_before / "index.html").is_file())
        index = (output / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="tab active" role="tab" aria-selected="true" data-tab="weekly"', index)
        self.assertIn("本周新品", index)
        self.assertIn("热门榜", index)
        self.assertIn("全部", index)
        self.assertIn("data-category=\"agent\"", index)
        self.assertIn("AI Agent", index)
        self.assertIn("Stars <strong>120</strong>", index)
        self.assertIn("创建 2026-08-01", index)
        self.assertIn("data-history-card", index)
        self.assertIn("GitHub Trending 周观察", index)
        self.assertIn("本周观察暂不可用", index)
        self.assertIn("每周 AI 开源雷达", index)
        self.assertIn("精选近期值得关注的新工具、新框架和增长项目。", index)
        self.assertIn("data-page-particles", index)
        self.assertNotIn("data-hero-particles", index)
        self.assertNotIn("每周发现近期值得关注的 AI 开源项目，帮你快速发现新工具、新框架和增长最快的项目。", index)
        self.assertNotIn("GitHub: ok", index)
        self.assertNotIn("Product Hunt: degraded", index)
        self.assertNotIn("摘要: degraded", index)
        detail = (output / "products" / slug_before / "index.html").read_text(encoding="utf-8")
        self.assertIn("data-page-particles", detail)
        self.assertNotIn("项目图片", detail)
        self.assertIn('data-chat data-endpoint=""', detail)
        self.assertIn("服务配置中", detail)
        self.assertIn('name="message" maxlength="1000"', detail)
        self.assertIn('name="use_web"', detail)
        self.assertIn('role="log" aria-live="polite" aria-relevant="additions text" hidden', detail)
        self.assertIn("问问这个项目", detail)
        self.assertIn("想快速了解它适合什么场景、怎么开始用，或者最近有什么更新，可以直接提问。", detail)
        self.assertIn("它适合什么场景？", detail)
        verify_static_site(output, forbidden_values=("not-in-output",))

    def test_detail_publishes_only_an_https_chat_endpoint(self) -> None:
        with connect(self.database_path) as connection, connection:
            _product_id, source_item_id = self._product_with_source(connection)
            output = self.root / "chat-batch"
            with patch.dict("os.environ", {"CHAT_API_URL": "https://chat.example.com/api/chat"}, clear=False):
                build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))
            product = connection.execute("SELECT * FROM product LIMIT 1").fetchone()
            source = connection.execute("SELECT * FROM source_item WHERE id = ?", (source_item_id,)).fetchone()

        detail = (output / "products" / stable_slug(product, [source]) / "index.html").read_text(encoding="utf-8")
        catalog = (output / "data" / "products.json").read_text(encoding="utf-8")
        self.assertIn('data-endpoint="https://chat.example.com/api/chat"', detail)
        self.assertIn("服务已连接", detail)
        self.assertIn("chat_context", catalog)
        self.assertNotIn("DEEPSEEK_API_KEY", detail)
        script = (output / "assets" / "site.js").read_text(encoding="utf-8")
        self.assertIn("正在生成回答...", script)
        self.assertIn("回答中...", script)
        self.assertIn("scrollIntoView", script)
        self.assertIn("textarea.value = message", script)
        self.assertNotIn("if (!tabs.length) return;", script)
        self.assertIn("if (tabs.length) render();", script)
        self.assertIn("use_web: Boolean(webToggle?.checked)", script)
        self.assertIn("appendChatSources(reply, payload.sources)", script)
        self.assertIn("rel = 'noopener noreferrer'", script)
        self.assertIn("form.requestSubmit()", script)
        self.assertIn("messages.hidden = false", script)
        with connect(self.database_path) as connection:
            insecure_output = self.root / "insecure-chat-batch"
            with patch.dict("os.environ", {"CHAT_API_URL": "http://chat.example.com/api/chat"}, clear=False):
                build_static_site(connection, insecure_output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))
        insecure_detail = (insecure_output / "products" / stable_slug(product, [source]) / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-endpoint=""', insecure_detail)

    def test_build_uses_text_fallback_when_product_and_source_summaries_are_missing(self) -> None:
        with connect(self.database_path) as connection, connection:
            product_id, source_item_id = self._product_with_source(connection)
            connection.execute("UPDATE product SET summary_zh = NULL WHERE id = ?", (product_id,))
            connection.execute("UPDATE source_item SET description = NULL WHERE id = ?", (source_item_id,))
            output = self.root / "missing-summary-batch"
            build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))

        products = json.loads((output / "data" / "products.json").read_text(encoding="utf-8"))["products"]
        self.assertEqual("暂无可展示的产品描述。", products[0]["summary"])
        self.assertEqual("暂无可展示的产品描述。", products[0]["card_summary"])

    def test_failed_build_keeps_the_previous_publication(self) -> None:
        output = self.root / "public"
        output.mkdir()
        (output / "index.html").write_text("previous batch", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            publish_site(output, lambda _staging: (_ for _ in ()).throw(RuntimeError("build failure")))

        self.assertEqual("previous batch", (output / "index.html").read_text(encoding="utf-8"))

    def test_verification_rejects_runtime_secret(self) -> None:
        output = self.root / "invalid"
        output.mkdir()
        (output / "index.html").write_text("secret-value", encoding="utf-8")
        (output / "data").mkdir()
        (output / "data" / "products.json").write_text('{"products": []}', encoding="utf-8")
        (output / "data" / "status.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(PublicationError):
            verify_static_site(output, forbidden_values=("secret-value",))

    def test_all_tab_exports_history_for_client_side_thirty_item_pagination(self) -> None:
        with connect(self.database_path) as connection, connection:
            for index in range(31):
                source_item_id = upsert_source_item(
                    connection,
                    source="github",
                    external_id=f"history-{index}",
                    name=f"History {index}",
                    description="Historical AI project",
                    raw_json={"created_at": "2026-01-01T00:00:00+00:00"},
                    github_created_at="2026-01-01T00:00:00+00:00",
                    observed_at="2026-08-02T00:00:00+00:00",
                )
                product_id = connection.execute(
                    "INSERT INTO product(name, category, first_seen_at, last_updated_at) VALUES (?, 'agent', ?, ?)",
                    (f"History {index}", "2026-01-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
                ).lastrowid
                connection.execute(
                    "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
                    (product_id, source_item_id),
                )
                upsert_metric_snapshot(
                    connection,
                    source_item_id=source_item_id,
                    snapshot_date=date(2026, 8, 2),
                    stars=index,
                )
                upsert_rank_history(
                    connection,
                    source_item_id=source_item_id,
                    list_name="hot",
                    listed_at="2026-08-02T00:00:00+00:00",
                    rank=index + 1,
                )
            output = self.root / "history-batch"
            build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))

        index = (output / "index.html").read_text(encoding="utf-8")
        script = (output / "assets" / "site.js").read_text(encoding="utf-8")
        self.assertEqual(31, index.count("data-history-card"))
        self.assertIn('data-tab="all">全部<span>31</span>', index)
        self.assertIn("const pageSize = 30", script)
        self.assertIn('data-page-action="next"', index)

    def test_trending_observation_is_a_separate_hot_tab_section(self) -> None:
        with connect(self.database_path) as connection, connection:
            source_item_id = upsert_source_item(
                connection,
                source="github_trending_observation",
                external_id="example/trending",
                name="Trending Project",
                description="A project observed on the weekly GitHub Trending page.",
                raw_json={"created_at": "2026-08-01T00:00:00+00:00"},
                github_created_at="2026-08-01T00:00:00+00:00",
                observed_at="2026-08-02T00:40:00+00:00",
            )
            product_id = connection.execute(
                "INSERT INTO product(name, category, first_seen_at, last_updated_at) VALUES ('Trending Project', 'agent', ?, ?)",
                ("2026-08-01T00:00:00+00:00", "2026-08-02T00:40:00+00:00"),
            ).lastrowid
            connection.execute(
                "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github_trending_observation', 1)",
                (product_id, source_item_id),
            )
            upsert_metric_snapshot(
                connection,
                source_item_id=source_item_id,
                snapshot_date=date(2026, 8, 2),
                stars=120,
            )
            output = self.root / "trending-batch"
            build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))

        index = (output / "index.html").read_text(encoding="utf-8")
        self.assertIn("GitHub Trending 周观察，抓取于 2026-08-02 UTC · 非 API 数据源", index)
        self.assertIn("<span class=\"flag observation\">周观察</span>", index)
        self.assertNotIn("{html.escape(trending_observation)}", index)

    def test_detail_prefers_readme_images_and_falls_back_to_homepage_og_image(self) -> None:
        with connect(self.database_path) as connection, connection:
            readme_product, readme_source = self._product_with_source(connection)
            update_source_enrichment(
                connection,
                source_item_id=readme_source,
                readme_text="# Stable Agent\nImplementation details",
                readme_images=["http://cdn.example.com/insecure.png", "https://cdn.example.com/readme-shot.png"],
                og_image="https://cdn.example.com/og-fallback.png",
            )
            connection.execute(
                "UPDATE product SET score_breakdown = ? WHERE id = ?",
                ('{"github":{"raw":{"stars_delta":35,"forks_delta":4},"window_days":7,"used_prefill":false},"freshness":{"enabled":true,"age_days":2,"multiplier":0.94}}', readme_product),
            )
            og_source = upsert_source_item(
                connection,
                source="github",
                external_id="og-only",
                name="OG Only",
                description="A project with a website preview image.",
                url="https://github.com/example/og-only",
                homepage="https://og-only.example.com",
                raw_json={"created_at": "2026-08-01T00:00:00+00:00"},
                github_created_at="2026-08-01T00:00:00+00:00",
                observed_at="2026-08-02T00:00:00+00:00",
            )
            og_product = connection.execute(
                "INSERT INTO product(name, category, summary_zh, first_seen_at, last_updated_at) VALUES ('OG Only', 'agent', ?, ?, ?)",
                ("这是一个使用官网图片回退的测试项目。", "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
            ).lastrowid
            connection.execute(
                "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
                (og_product, og_source),
            )
            update_source_enrichment(connection, source_item_id=og_source, readme_images=[], og_image="https://cdn.example.com/og-only.png")
            output = self.root / "detail-batch"
            build_static_site(connection, output, generated_at=datetime(2026, 8, 2, tzinfo=timezone.utc))
            readme_row = connection.execute("SELECT * FROM product WHERE id = ?", (readme_product,)).fetchone()
            og_row = connection.execute("SELECT * FROM product WHERE id = ?", (og_product,)).fetchone()
            readme_source_row = connection.execute("SELECT * FROM source_item WHERE id = ?", (readme_source,)).fetchone()
            og_source_row = connection.execute("SELECT * FROM source_item WHERE id = ?", (og_source,)).fetchone()

        readme_detail = (output / "products" / stable_slug(readme_row, [readme_source_row]) / "index.html").read_text(encoding="utf-8")
        og_detail = (output / "products" / stable_slug(og_row, [og_source_row]) / "index.html").read_text(encoding="utf-8")
        self.assertIn("项目分析", readme_detail)
        self.assertNotIn("<span>基于README</span>", readme_detail)
        self.assertIn("https://cdn.example.com/readme-shot.png", readme_detail)
        self.assertNotIn("http://cdn.example.com/insecure.png", readme_detail)
        self.assertNotIn("https://cdn.example.com/og-fallback.png", readme_detail)
        self.assertIn("为什么值得关注", readme_detail)
        self.assertNotIn("可追溯口径", readme_detail)
        self.assertIn("近 7 天 GitHub 增长明显：Stars +35，Forks +4。", readme_detail)
        self.assertIn("<dt>增长统计</dt>", readme_detail)
        self.assertIn("<dd>近 7 天</dd>", readme_detail)
        self.assertIn("新近收录：进入榜单 2 天，适合继续关注后续变化。", readme_detail)
        self.assertIn("GitHub 仓库", readme_detail)
        self.assertIn("项目官网", readme_detail)
        self.assertIn("https://cdn.example.com/og-only.png", og_detail)
        self.assertIn("图：项目官网", og_detail)

    def _product_with_source(self, connection):
        product_id = connection.execute(
            "INSERT INTO product(name, category, summary_zh, summary_status, heat_score, first_seen_at, last_updated_at) VALUES (?, ?, ?, 'ok', ?, ?, ?)",
            (
                "Stable Agent",
                "agent",
                "这是一个用于自动化日常工作流的 AI 智能体产品，帮助团队整理任务、跟踪进度并通过可追溯来源理解能力边界。",
                88.0,
                "2026-08-01T00:00:00+00:00",
                "2026-08-02T00:00:00+00:00",
            ),
        ).lastrowid
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id="stable-agent",
            name="Stable Agent",
            description="An AI agent for workflow automation.",
            url="https://github.com/example/stable-agent",
            homepage="https://stable.example.com",
            raw_json={"created_at": "2026-08-01T00:00:00+00:00"},
            github_created_at="2026-08-01T00:00:00+00:00",
            observed_at="2026-08-01T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
            (product_id, source_item_id),
        )
        return product_id, source_item_id


if __name__ == "__main__":
    unittest.main()
