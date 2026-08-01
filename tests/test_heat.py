from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item
from ai_info_web.heat import calculate_heat_scores, rank_hot_products, rank_new_products


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

    def test_dual_source_uses_product_hunt_weights_and_preserves_missing_component(self) -> None:
        with connect(self.database_path) as connection, connection:
            dual = self._product(connection, "Dual", "2026-08-08T00:00:00+00:00")
            github_only = self._product(connection, "GitHub Only", "2026-08-08T00:00:00+00:00")
            self._github_source(connection, dual, "dual", [("2026-08-01", 10, 1), ("2026-08-08", 110, 21)])
            self._product_hunt_source(connection, dual, "dual-ph", votes=80, rank=1)
            self._github_source(connection, github_only, "github-only", [("2026-08-01", 10, 1), ("2026-08-08", 10, 1)])

            result = calculate_heat_scores(
                connection, config_path=self.config_path, as_of_date=date(2026, 8, 8)
            )
            dual_row = connection.execute("SELECT * FROM product WHERE id = ?", (dual,)).fetchone()
            github_only_row = connection.execute(
                "SELECT * FROM product WHERE id = ?", (github_only,)
            ).fetchone()

        dual_breakdown = json.loads(dual_row["score_breakdown"])
        github_only_breakdown = json.loads(github_only_row["score_breakdown"])
        self.assertTrue(result.product_hunt_available)
        self.assertEqual("dual_source", dual_breakdown["mode"])
        self.assertEqual(["github", "producthunt"], dual_breakdown["scoring_sources"])
        self.assertEqual(80, dual_breakdown["producthunt"]["raw"]["votes_count"])
        self.assertEqual(1.0, dual_breakdown["producthunt"]["raw"]["rank_inverse"])
        self.assertEqual(100.0, dual_breakdown["producthunt"]["normalised"]["votes_count"])
        self.assertIsNone(github_only_breakdown["producthunt"])
        self.assertGreater(dual_row["heat_score"], github_only_row["heat_score"])

    def test_new_tab_filters_by_window_and_uses_source_signal_as_tiebreaker(self) -> None:
        with connect(self.database_path) as connection, connection:
            newest_high_signal = self._product(connection, "Newest High", "2026-08-08T10:00:00+00:00")
            newest_low_signal = self._product(connection, "Newest Low", "2026-08-08T10:00:00+00:00")
            recent = self._product(connection, "Recent", "2026-08-07T11:00:00+00:00")
            old = self._product(connection, "Old", "2026-08-06T09:59:00+00:00")
            self._github_source(connection, newest_high_signal, "high", [("2026-08-08", 100, 0)])
            self._github_source(connection, newest_low_signal, "low", [("2026-08-08", 10, 0)])
            self._github_source(connection, recent, "recent", [("2026-08-08", 999, 0)])
            self._github_source(connection, old, "old", [("2026-08-08", 9999, 0)])

            products = rank_new_products(
                connection,
                now=datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc),
                window_hours=48,
            )

        self.assertEqual(
            [newest_high_signal, newest_low_signal, recent],
            [product["id"] for product in products],
        )

    def _product(self, connection, name: str, first_seen_at: str) -> int:
        return connection.execute(
            "INSERT INTO product(name, category, first_seen_at, last_updated_at) VALUES (?, 'other', ?, ?)",
            (name, first_seen_at, first_seen_at),
        ).lastrowid

    def _github_source(self, connection, product_id: int, external_id: str, snapshots) -> None:
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id=external_id,
            name=external_id,
            description="AI repository",
            raw_json={},
            observed_at="2026-08-08T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
            (product_id, source_item_id),
        )
        for snapshot_date, stars, forks in snapshots:
            upsert_metric_snapshot(
                connection,
                source_item_id=source_item_id,
                snapshot_date=date.fromisoformat(snapshot_date),
                stars=stars,
                forks=forks,
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
