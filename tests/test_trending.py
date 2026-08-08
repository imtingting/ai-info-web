from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_info_web.db import connect, initialize_database
from ai_info_web.github import GitHubProvider, GitHubResponse
from ai_info_web.trending import GitHubTrendingObserver, TrendingPageResponse


class TrendingObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_successful_weekly_observation_is_enriched_with_the_repository_api(self) -> None:
        html = """
        <article class="Box-row"><h2><a href="/team/demo"> team / demo </a></h2>
        <p class="color-fg-muted">A useful AI project.</p><span itemprop="programmingLanguage">Python</span></article>
        """
        github = GitHubProvider(
            token="test-token",
            queries=(),
            transport=lambda _request, _timeout: GitHubResponse(
                200,
                {},
                {
                    "id": 101,
                    "full_name": "team/demo",
                    "description": "A useful AI project.",
                    "html_url": "https://github.com/team/demo",
                    "homepage": "https://demo.example.com",
                    "topics": ["llm"],
                    "created_at": "2026-08-01T00:00:00Z",
                    "stargazers_count": 100,
                    "forks_count": 10,
                },
            ),
        )
        observer = GitHubTrendingObserver(
            github=github,
            transport=lambda _request, _timeout: TrendingPageResponse(200, html),
        )
        with connect(self.database_path) as connection:
            result = observer.run(connection, observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc))
            item = connection.execute("SELECT * FROM source_item").fetchone()
            snapshot = connection.execute("SELECT * FROM metric_snapshot").fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual(1, result.items_seen)
        self.assertEqual(1, result.items_persisted)
        self.assertEqual("github_trending_observation", item["source"])
        self.assertEqual("2026-08-01T00:00:00Z", item["github_created_at"])
        self.assertEqual(100, snapshot["stars"])

    def test_failed_page_is_recorded_as_degraded_without_raising(self) -> None:
        github = GitHubProvider(token="test-token", queries=())
        observer = GitHubTrendingObserver(
            github=github,
            transport=lambda _request, _timeout: TrendingPageResponse(503, "temporarily unavailable"),
        )
        with connect(self.database_path) as connection:
            result = observer.run(connection, observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc))
            log = connection.execute("SELECT * FROM run_log").fetchone()

        self.assertEqual("degraded", result.status)
        self.assertEqual(0, result.items_persisted)
        self.assertIn("HTTP 503", result.error or "")
        self.assertIn("github_trending_observation", log["provider_status"])


if __name__ == "__main__":
    unittest.main()
