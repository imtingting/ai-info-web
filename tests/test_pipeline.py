from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_info_web.db import connect, upsert_metric_snapshot, upsert_source_item
from ai_info_web.github import GitHubEnrichmentResult, GitHubPrefillResult, GitHubRunResult
from ai_info_web.pipeline import run_daily
from ai_info_web.producthunt import ProductHuntRunResult
from ai_info_web.settings import Settings
from ai_info_web.summary import SummaryRunResult


class DailyPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "working" / "state.sqlite3"
        self.output_directory = self.root / "public"
        self.state_directory = self.root / "state-repo"
        self.settings = Settings(
            database_path=self.database_path,
            enable_product_hunt=False,
            product_hunt_pages_per_run=1,
            enable_summary=True,
            summary_monthly_budget_cny=20.0,
            github_pages_per_query=1,
            github_queries=("topic:ai-agent",),
        )
        self.project_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_two_runs_restore_private_state_and_publish_static_batches(self) -> None:
        first = self._run(date(2026, 8, 1))
        second = self._run(date(2026, 8, 2))

        self.assertTrue(first.published)
        self.assertFalse(first.restored_state)
        self.assertTrue(second.published)
        self.assertTrue(second.restored_state)
        self.assertTrue((self.output_directory / "index.html").is_file())
        with connect(self.state_directory / "state.sqlite3") as connection:
            snapshots = connection.execute("SELECT COUNT(*) AS count FROM metric_snapshot").fetchone()["count"]
        self.assertEqual(2, snapshots)

    def test_critical_github_failure_preserves_previous_publication(self) -> None:
        self.output_directory.mkdir()
        (self.output_directory / "index.html").write_text("previous batch", encoding="utf-8")
        result = run_daily(
            settings=self.settings,
            database_path=self.database_path,
            output_directory=self.output_directory,
            review_queue_path=self.root / "review.json",
            project_root=self.project_root,
            github_provider=FailedGitHub(),
            run_date=date(2026, 8, 2),
        )

        self.assertFalse(result.published)
        self.assertEqual("failed", result.status)
        self.assertEqual("previous batch", (self.output_directory / "index.html").read_text(encoding="utf-8"))

    def test_optional_github_enrichment_failure_does_not_block_publication(self) -> None:
        result = run_daily(
            settings=self.settings,
            database_path=self.database_path,
            output_directory=self.output_directory,
            review_queue_path=self.root / "review.json",
            project_root=self.project_root,
            github_provider=EnrichmentDegradedGitHub(),
            product_hunt_provider=DegradedProductHunt(),
            summary_provider=DegradedSummary(),
            run_date=date(2026, 8, 2),
        )

        self.assertTrue(result.published)
        self.assertEqual("degraded", result.provider_status["github_enrichment"])
        self.assertTrue((self.output_directory / "index.html").is_file())

    def test_startup_prefill_failure_does_not_block_publication(self) -> None:
        result = run_daily(
            settings=self.settings,
            database_path=self.database_path,
            output_directory=self.output_directory,
            review_queue_path=self.root / "review.json",
            project_root=self.project_root,
            github_provider=PrefillDegradedGitHub(),
            product_hunt_provider=DegradedProductHunt(),
            summary_provider=DegradedSummary(),
            run_date=date(2026, 8, 2),
        )

        self.assertTrue(result.published)
        self.assertEqual("degraded", result.provider_status["github_prefill"])
        self.assertTrue((self.output_directory / "index.html").is_file())

    def _run(self, run_date: date):
        return run_daily(
            settings=self.settings,
            database_path=self.database_path,
            output_directory=self.output_directory,
            review_queue_path=self.root / "review.json",
            project_root=self.project_root,
            state_directory=self.state_directory,
            github_provider=SuccessfulGitHub(),
            product_hunt_provider=DegradedProductHunt(),
            summary_provider=DegradedSummary(),
            run_date=run_date,
        )


class SuccessfulGitHub:
    def run(self, connection, *, snapshot_date):
        source_item_id = upsert_source_item(
            connection,
            source="github",
            external_id="daily-agent",
            name="Daily Agent",
            description="An AI agent for reliable workflow automation.",
            url="https://github.com/example/daily-agent",
            homepage="https://daily-agent.example.com",
            topics=("ai-agent",),
            raw_json={"created_at": "2026-08-01T00:00:00+00:00", "stargazers_count": 100},
            observed_at="2026-08-01T00:00:00+00:00",
        )
        upsert_metric_snapshot(
            connection,
            source_item_id=source_item_id,
            snapshot_date=snapshot_date,
            stars=100 + snapshot_date.day,
            forks=5,
        )
        return GitHubRunResult("ok", 1, 1, 1)


class FailedGitHub:
    def run(self, _connection, *, snapshot_date):
        return GitHubRunResult("failed", 0, 0, 0, "offline")


class EnrichmentDegradedGitHub(SuccessfulGitHub):
    def enrich_curated_items(self, _connection):
        return GitHubEnrichmentResult("degraded", 0, 0, 0, ("README unavailable",))


class PrefillDegradedGitHub(SuccessfulGitHub):
    def prefill_curated_star_deltas(self, _connection, *, snapshot_date):
        return GitHubPrefillResult("degraded", 1, 0, ("stargazers unavailable",))


class DegradedProductHunt:
    def run(self, _connection, *, snapshot_date):
        return ProductHuntRunResult("degraded", 0, 0, 0, "disabled")


class DegradedSummary:
    def run(self, _connection, *, run_date):
        return SummaryRunResult("degraded", 1, 0, 0, 1, 0, 0)


if __name__ == "__main__":
    unittest.main()
