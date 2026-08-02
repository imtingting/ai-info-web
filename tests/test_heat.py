from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item
from ai_info_web.heat import (
    calculate_heat_scores,
    rank_history_page,
    rank_hot_products,
    rank_new_products,
    record_rank_history,
)


class HeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"
        self.config_path = Path(__file__).resolve().parents[1] / "config" / "heat_config.json"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_github_only_scores_use_product_set_normalisation_and_actual_window(self) -> None:
        with connect(self.database_path) as connection, connection:
            leading = self._product(connection, "Leading", "2026-08-05T00:00:00+00:00")
            trailing = self._product(connection, "Trailing", "2026-08-08T00:00:00+00:00")
            self._github_source(connection, leading, "leading", [("2026-08-01", 100, 10), ("2026-08-08", 170, 30)])
            self._github_source(connection, trailing, "trailing", [("2026-08-01", 80, 20), ("2026-08-08", 100, 20)])

            result = calculate_heat_scores(
                connection, config_path=self.config_path, as_of_date=date(2026, 8, 8)
            )
            products = rank_hot_products(connection)
            leading_row = connection.execute("SELECT * FROM product WHERE id = ?", (leading,)).fetchone()
            trailing_row = connection.execute("SELECT * FROM product WHERE id = ?", (trailing,)).fetchone()

        leading_breakdown = json.loads(leading_row["score_breakdown"])
        trailing_breakdown = json.loads(trailing_row["score_breakdown"])
        self.assertEqual(2, result.products_scored)
        self.assertFalse(result.product_hunt_available)
        self.assertEqual([leading, trailing], [row["id"] for row in products])
        self.assertGreater(leading_row["heat_score"], trailing_row["heat_score"])
        self.assertEqual("github_only", leading_breakdown["mode"])
        self.assertEqual(["github"], leading_breakdown["scoring_sources"])
        self.assertEqual(7, leading_breakdown["github"]["window_days"])
        self.assertEqual(70, leading_breakdown["github"]["raw"]["stars_delta"])
        self.assertEqual(100.0, leading_breakdown["github"]["normalised"]["stars_delta"])
        self.assertEqual(0.0, trailing_breakdown["github"]["normalised"]["stars_delta"])
        self.assertEqual({"github": 1.0}, leading_breakdown["source_weights"])

    def test_single_snapshot_and_equal_values_do_not_divide_by_zero(self) -> None:
        with connect(self.database_path) as connection, connection:
            first = self._product(connection, "First", "2026-08-08T00:00:00+00:00")
            second = self._product(connection, "Second", "2026-08-08T00:00:00+00:00")
            self._github_source(connection, first, "first", [("2026-08-08", 10, 1)])
            self._github_source(connection, second, "second", [("2026-08-08", 10, 1)])

            calculate_heat_scores(connection, config_path=self.config_path, as_of_date=date(2026, 8, 8))
            rows = connection.execute("SELECT heat_score, score_breakdown FROM product ORDER BY id").fetchall()

        for row in rows:
            breakdown = json.loads(row["score_breakdown"])
            self.assertEqual(0.0, row["heat_score"])
            self.assertEqual(0, breakdown["github"]["window_days"])
            self.assertEqual(0.0, breakdown["github"]["normalised"]["stars_delta"])
            self.assertEqual(0.0, breakdown["github"]["normalised"]["forks_delta"])

    def test_equal_positive_deltas_keep_a_nonzero_score(self) -> None:
        with connect(self.database_path) as connection, connection:
            first = self._product(connection, "First", "2026-08-08T00:00:00+00:00")
            second = self._product(connection, "Second", "2026-08-08T00:00:00+00:00")
            self._github_source(connection, first, "first", [("2026-08-01", 10, 1), ("2026-08-08", 20, 1)])
            self._github_source(connection, second, "second", [("2026-08-01", 20, 1), ("2026-08-08", 30, 1)])

            calculate_heat_scores(connection, config_path=self.config_path, as_of_date=date(2026, 8, 8))
            rows = connection.execute("SELECT heat_score, score_breakdown FROM product ORDER BY id").fetchall()

        for row in rows:
            breakdown = json.loads(row["score_breakdown"])
            self.assertEqual(100.0, breakdown["github"]["normalised"]["stars_delta"])
            self.assertGreater(row["heat_score"], 0.0)

    def test_mixed_sources_score_github_only_products_without_ph_penalty(self) -> None:
        with connect(self.database_path) as connection, connection:
            dual_strong = self._product(connection, "Dual Strong", "2026-08-08T00:00:00+00:00")
            dual_weak = self._product(connection, "Dual Weak", "2026-08-08T00:00:00+00:00")
            github_only = self._product(connection, "GitHub Only", "2026-08-08T00:00:00+00:00")
            snapshots = [("2026-08-01", 10, 1), ("2026-08-08", 110, 21)]
            self._github_source(connection, dual_strong, "dual-strong", snapshots)
            self._github_source(connection, dual_weak, "dual-weak", snapshots)
            self._github_source(connection, github_only, "github-only", snapshots)
            self._product_hunt_source(connection, dual_strong, "strong-ph", votes=80, rank=1)
            self._product_hunt_source(connection, dual_weak, "weak-ph", votes=20, rank=10)

            result = calculate_heat_scores(
                connection, config_path=self.config_path, as_of_date=date(2026, 8, 8)
            )
            dual_strong_row = connection.execute(
                "SELECT * FROM product WHERE id = ?", (dual_strong,)
            ).fetchone()
            dual_weak_row = connection.execute(
                "SELECT * FROM product WHERE id = ?", (dual_weak,)
            ).fetchone()
            github_only_row = connection.execute(
                "SELECT * FROM product WHERE id = ?", (github_only,)
            ).fetchone()

        dual_strong_breakdown = json.loads(dual_strong_row["score_breakdown"])
        dual_weak_breakdown = json.loads(dual_weak_row["score_breakdown"])
        github_only_breakdown = json.loads(github_only_row["score_breakdown"])
        self.assertTrue(result.product_hunt_available)
        self.assertEqual("dual_source", dual_strong_breakdown["mode"])
        self.assertEqual(["github", "producthunt"], dual_strong_breakdown["scoring_sources"])
        self.assertEqual(80, dual_strong_breakdown["producthunt"]["raw"]["votes_count"])
        self.assertEqual(1.0, dual_strong_breakdown["producthunt"]["raw"]["rank_inverse"])
        self.assertEqual(100.0, dual_strong_breakdown["producthunt"]["normalised"]["votes_count"])
        self.assertEqual(0.0, dual_weak_breakdown["producthunt"]["normalised"]["votes_count"])
        self.assertEqual("github_only", github_only_breakdown["mode"])
        self.assertEqual(["github"], github_only_breakdown["scoring_sources"])
        self.assertEqual({"github": 1.0}, github_only_breakdown["source_weights"])
        self.assertIsNone(github_only_breakdown["producthunt"])
        self.assertEqual(100.0, github_only_row["heat_score"])
        self.assertGreater(github_only_row["heat_score"], dual_weak_row["heat_score"])

    def test_weekly_new_uses_github_creation_time_and_stars(self) -> None:
        with connect(self.database_path) as connection, connection:
            high_stars = self._product(connection, "High Stars", "2026-07-01T00:00:00+00:00")
            low_stars = self._product(connection, "Low Stars", "2026-08-08T10:00:00+00:00")
            old = self._product(connection, "Old", "2026-08-08T10:00:00+00:00")
            self._github_source(connection, high_stars, "high", [("2026-08-08", 100, 0)], created_at="2026-08-02T00:00:00Z")
            self._github_source(connection, low_stars, "low", [("2026-08-08", 10, 0)], created_at="2026-08-08T00:00:00Z")
            self._github_source(connection, old, "old", [("2026-08-08", 9999, 0)], created_at="2026-07-31T00:00:00Z")

            products = rank_new_products(
                connection,
                now=datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc),
            )

        self.assertEqual([high_stars, low_stars], [product["id"] for product in products])

    def test_prefilled_single_snapshot_is_eligible_for_hot_ranking(self) -> None:
        with connect(self.database_path) as connection, connection:
            prefilled = self._product(connection, "Prefilled", "2026-08-08T00:00:00+00:00")
            unready = self._product(connection, "Unready", "2026-08-08T00:00:00+00:00")
            self._github_source(
                connection,
                prefilled,
                "prefilled",
                [("2026-08-08", 100, 5, 30, 0, 7)],
            )
            self._github_source(connection, unready, "unready", [("2026-08-08", 200, 5)])
            calculate_heat_scores(connection, config_path=self.config_path, as_of_date=date(2026, 8, 8))
            products = rank_hot_products(connection)
            breakdown = json.loads(
                connection.execute("SELECT score_breakdown FROM product WHERE id = ?", (prefilled,)).fetchone()["score_breakdown"]
            )

        self.assertEqual([prefilled], [product["id"] for product in products])
        self.assertTrue(breakdown["github"]["used_prefill"])
        self.assertEqual(30, breakdown["github"]["raw"]["stars_delta"])

    def test_rank_history_returns_de_duplicated_weekly_new_and_hot_union(self) -> None:
        with connect(self.database_path) as connection, connection:
            product_id = self._product(connection, "History", "2026-08-08T00:00:00+00:00")
            self._github_source(
                connection,
                product_id,
                "history",
                [("2026-08-01", 10, 0), ("2026-08-08", 30, 0)],
                created_at="2026-08-06T00:00:00Z",
            )
            calculate_heat_scores(connection, config_path=self.config_path, as_of_date=date(2026, 8, 8))
            record_rank_history(connection, listed_at=datetime(2026, 8, 8, tzinfo=timezone.utc))
            page = rank_history_page(connection, page=1, page_size=30)

        self.assertEqual(1, page.total_items)
        self.assertEqual(["history"], [item["external_id"] for item in page.items])
        self.assertIn("hot", page.items[0]["rank_sources"])
        self.assertIn("weekly_new", page.items[0]["rank_sources"])

    def _product(self, connection, name: str, first_seen_at: str) -> int:
        return connection.execute(
            "INSERT INTO product(name, category, first_seen_at, last_updated_at) VALUES (?, 'other', ?, ?)",
            (name, first_seen_at, first_seen_at),
        ).lastrowid

    def _github_source(
        self,
        connection,
        product_id: int,
        external_id: str,
        snapshots,
        *,
        created_at: str = "2026-08-08T00:00:00Z",
    ) -> None:
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id=external_id,
            name=external_id,
            description="AI repository",
            raw_json={"created_at": created_at},
            github_created_at=created_at,
            observed_at="2026-08-08T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
            (product_id, source_item_id),
        )
        for snapshot in snapshots:
            snapshot_date, stars, forks, *prefill = snapshot
            upsert_metric_snapshot(
                connection,
                source_item_id=source_item_id,
                snapshot_date=date.fromisoformat(snapshot_date),
                stars=stars,
                forks=forks,
                stars_delta_prefill=prefill[0] if prefill else None,
                forks_delta_prefill=prefill[1] if len(prefill) > 1 else None,
                prefill_window_days=prefill[2] if len(prefill) > 2 else None,
            )

    def _product_hunt_source(self, connection, product_id: int, external_id: str, *, votes: int, rank: int) -> None:
        source_item_id = upsert_source_item(
            connection,
            source="producthunt",
            external_id=external_id,
            name=external_id,
            description="AI product",
            raw_json={},
            observed_at="2026-08-08T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'producthunt', 0)",
            (product_id, source_item_id),
        )
        upsert_metric_snapshot(
            connection,
            source_item_id=source_item_id,
            snapshot_date=date(2026, 8, 8),
            votes_count=votes,
            daily_rank=rank,
        )


if __name__ == "__main__":
    unittest.main()
