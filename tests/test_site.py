from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item
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
        self.assertIn("data-category=\"agent\"", (output / "index.html").read_text(encoding="utf-8"))
        verify_static_site(output, forbidden_values=("not-in-output",))

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
            observed_at="2026-08-01T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
            (product_id, source_item_id),
        )
        return product_id, source_item_id


if __name__ == "__main__":
    unittest.main()
