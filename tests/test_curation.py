from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ai_info_web.curation import curate
from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item


class CurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_directory = Path(self.temporary_directory.name)
        self.database_path = self.state_directory / "state.sqlite3"
        self.review_queue_path = self.state_directory / "weak_match_review.json"
        self.rules_path = Path(__file__).resolve().parents[1] / "config" / "category_rules.json"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_strong_domain_match_merges_sources_and_prefers_github(self) -> None:
        with connect(self.database_path) as connection, connection:
            github_id = self._github(
                connection,
                external_id="github-1",
                name="Acme Agent",
                homepage="https://acme.example.com",
                topics=("ai-agent",),
                stars=70,
            )
            product_hunt_id = self._product_hunt(
                connection,
                external_id="ph-1",
                name="Acme",
                homepage="https://www.acme.example.com",
                votes=40,
            )
            result = self._curate(connection)
            product = connection.execute("SELECT * FROM product").fetchone()
            sources = connection.execute(
                "SELECT source_item_id, is_primary FROM product_source ORDER BY source_item_id"
            ).fetchall()

        self.assertEqual(1, result.products_created)
        self.assertEqual(2, result.source_items_accepted)
        self.assertEqual(0, result.weak_matches)
        self.assertEqual("Acme Agent", product["name"])
        self.assertEqual("agent", product["category"])
        self.assertEqual(
            [(github_id, 1), (product_hunt_id, 0)],
            [(source["source_item_id"], source["is_primary"]) for source in sources],
        )
        self.assertEqual([], self._review_queue())

    def test_name_only_match_stays_separate_and_enters_private_review_queue(self) -> None:
        with connect(self.database_path) as connection, connection:
            github_id = self._github(
                connection,
                external_id="github-1",
                name="Focus AI",
                homepage="https://focus.dev",
                stars=60,
            )
            product_hunt_id = self._product_hunt(
                connection,
                external_id="ph-1",
                name="Focus App",
                homepage="https://unrelated.example.com",
                votes=40,
            )
            result = self._curate(connection)
            product_count = connection.execute(
                "SELECT COUNT(*) AS count FROM product"
            ).fetchone()["count"]
            mapping_count = connection.execute(
                "SELECT COUNT(*) AS count FROM product_source"
            ).fetchone()["count"]

        self.assertEqual(2, result.products_created)
        self.assertEqual(1, result.weak_matches)
        self.assertEqual(2, product_count)
        self.assertEqual(2, mapping_count)
        self.assertEqual(
            [{
                "left_source_item_id": github_id,
                "right_source_item_id": product_hunt_id,
                "name": "Focus AI",
                "reason": "normalized_name_match",
            }],
            self._review_queue(),
        )

    def test_more_complete_product_hunt_item_can_be_primary(self) -> None:
        with connect(self.database_path) as connection, connection:
            github_id = upsert_source_item(
                connection,
                source="github",
                external_id="github-1",
                name="Acme",
                url="https://github.com/example/acme",
                homepage="https://acme.example.com",
                raw_json={"stargazers_count": 70, "created_at": "2026-08-01T00:00:00Z"},
                observed_at="2026-08-02T00:00:00+00:00",
            )
            upsert_metric_snapshot(
                connection,
                source_item_id=github_id,
                snapshot_date=date(2026, 8, 2),
                stars=70,
            )
            product_hunt_id = self._product_hunt(
                connection,
                external_id="ph-1",
                name="Acme Product",
                homepage="https://acme.example.com",
                votes=40,
                description="A full product description",
            )
            self._curate(connection)
            primary = connection.execute(
                "SELECT source_item_id FROM product_source WHERE is_primary = 1"
            ).fetchone()

        self.assertEqual(product_hunt_id, primary["source_item_id"])

    def test_filters_low_signal_and_blacklisted_records_and_accepts_product_hunt_rank(self) -> None:
        with connect(self.database_path) as connection, connection:
            self._github(
                connection,
                external_id="low-stars",
                name="Small Project",
                homepage="https://small.example.com",
                stars=19,
                created_at="2026-07-01T00:00:00Z",
            )
            self._github(
                connection,
                external_id="template",
                name="Agent Template",
                homepage="https://template.example.com",
                stars=200,
            )
            accepted_id = self._product_hunt(
                connection,
                external_id="ranked",
                name="Research Lens",
                homepage="https://lens.example.com",
                votes=0,
                rank=20,
                description="A research workspace",
            )
            result = self._curate(connection)
            source = connection.execute(
                "SELECT source_item_id FROM product_source"
            ).fetchone()
            product = connection.execute("SELECT category FROM product").fetchone()

        self.assertEqual(1, result.products_created)
        self.assertEqual(1, result.source_items_accepted)
        self.assertEqual(accepted_id, source["source_item_id"])
        self.assertEqual("research", product["category"])

    def test_repeated_runs_rebuild_without_duplicate_products_or_review_items(self) -> None:
        with connect(self.database_path) as connection, connection:
            self._github(
                connection,
                external_id="github-1",
                name="Focus AI",
                homepage="https://focus.dev",
                stars=60,
            )
            self._product_hunt(
                connection,
                external_id="ph-1",
                name="Focus App",
                homepage="https://unrelated.example.com",
                votes=40,
            )
            self._curate(connection)
            result = self._curate(connection)
            product_count = connection.execute(
                "SELECT COUNT(*) AS count FROM product"
            ).fetchone()["count"]
            mapping_count = connection.execute(
                "SELECT COUNT(*) AS count FROM product_source"
            ).fetchone()["count"]

        self.assertEqual(2, result.products_created)
        self.assertEqual(1, result.weak_matches)
        self.assertEqual(2, product_count)
        self.assertEqual(2, mapping_count)
        self.assertEqual(1, len(self._review_queue()))

    def _curate(self, connection):
        return curate(
            connection,
            rules_path=self.rules_path,
            review_queue_path=self.review_queue_path,
            now=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

    def _github(
        self,
        connection,
        *,
        external_id: str,
        name: str,
        homepage: str,
        stars: int,
        topics: tuple[str, ...] = (),
        created_at: str = "2026-08-01T00:00:00Z",
    ) -> int:
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id=external_id,
            name=name,
            description="Useful AI software",
            url=f"https://github.com/example/{external_id}",
            homepage=homepage,
            topics=topics,
            raw_json={"stargazers_count": stars, "created_at": created_at},
            observed_at="2026-08-02T00:00:00+00:00",
        )
        upsert_metric_snapshot(
            connection,
            source_item_id=source_item_id,
            snapshot_date=date(2026, 8, 2),
            stars=stars,
        )
        return source_item_id

    def _product_hunt(
        self,
        connection,
        *,
        external_id: str,
        name: str,
        homepage: str,
        votes: int,
        rank: int | None = None,
        description: str = "Useful AI product",
    ) -> int:
        source_item_id = upsert_source_item(
            connection,
            source="producthunt",
            external_id=external_id,
            name=name,
            description=description,
            url=f"https://producthunt.com/posts/{external_id}",
            homepage=homepage,
            raw_json={"votesCount": votes, "dailyRank": rank},
            observed_at="2026-08-02T00:00:00+00:00",
        )
        upsert_metric_snapshot(
            connection,
            source_item_id=source_item_id,
            snapshot_date=date(2026, 8, 2),
            votes_count=votes,
            daily_rank=rank,
        )
        return source_item_id

    def _review_queue(self):
        return json.loads(self.review_queue_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
