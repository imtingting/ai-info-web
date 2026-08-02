from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item
from ai_info_web.github import GitHubProvider, GitHubResponse, HomepageResponse


def repository(repository_id: int, name: str, stars: int = 10) -> dict[str, object]:
    return {
        "id": repository_id,
        "name": name,
        "full_name": f"team/{name}",
        "description": f"{name} description",
        "html_url": f"https://github.com/team/{name}",
        "homepage": "https://example.com",
        "topics": ["llm"],
        "stargazers_count": stars,
        "forks_count": 2,
    }


class FakeTransport:
    def __init__(self, responses: list[GitHubResponse | Exception]) -> None:
        self.responses = responses
        self.requests = []

    def __call__(self, request, _timeout: float) -> GitHubResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GitHubProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_missing_token_marks_critical_source_as_failed_without_requests(self) -> None:
        transport = FakeTransport([])
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token=None, queries=("topic:llm",), transport=transport
            ).run(connection, snapshot_date=date(2026, 8, 1))
            log = connection.execute("SELECT * FROM run_log").fetchone()

        self.assertEqual("failed", result.status)
        self.assertIn("GITHUB_TOKEN", result.error or "")
        self.assertEqual([], transport.requests)
        self.assertEqual({"github": "failed"}, json.loads(log["provider_status"]))

    def test_search_pages_are_deduplicated_and_persisted_as_daily_snapshots(self) -> None:
        next_page = "https://api.github.com/search/repositories?page=2"
        transport = FakeTransport(
            [
                GitHubResponse(
                    status=200,
                    headers={"Link": f'<{next_page}>; rel="next"'},
                    body={"items": [repository(1, "one", 15), repository(2, "two", 20)]},
                ),
                GitHubResponse(
                    status=200,
                    headers={},
                    body={"items": [repository(2, "two", 21), repository(3, "three", 30)]},
                ),
            ]
        )
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token="test-token",
                queries=("topic:llm",),
                transport=transport,
                pages_per_query=2,
                recent_created_days=0,
            ).run(connection, snapshot_date=date(2026, 8, 1))
            items = connection.execute("SELECT * FROM source_item ORDER BY external_id").fetchall()
            snapshots = connection.execute(
                "SELECT * FROM metric_snapshot ORDER BY source_item_id"
            ).fetchall()

        self.assertEqual("ok", result.status)
        self.assertEqual(3, result.items_seen)
        self.assertEqual(3, result.items_new)
        self.assertEqual(2, result.request_count)
        self.assertEqual(["1", "2", "3"], [item["external_id"] for item in items])
        self.assertEqual([15, 21, 30], [snapshot["stars"] for snapshot in snapshots])
        first_query = parse_qs(urlparse(transport.requests[0].full_url).query)
        self.assertEqual(["topic:llm"], first_query["q"])
        self.assertEqual(["100"], first_query["per_page"])
        self.assertEqual("Bearer test-token", transport.requests[0].headers["Authorization"])

    def test_retries_rate_limited_request_using_retry_after_header(self) -> None:
        transport = FakeTransport(
            [
                GitHubResponse(status=429, headers={"Retry-After": "2"}, body={}),
                GitHubResponse(status=200, headers={}, body={"items": []}),
            ]
        )
        delays: list[float] = []
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token="test-token",
                queries=("topic:llm",),
                transport=transport,
                sleep=delays.append,
                recent_created_days=0,
            ).run(connection, snapshot_date=date(2026, 8, 1))

        self.assertEqual("ok", result.status)
        self.assertEqual([2.0], delays)
        self.assertEqual(2, result.request_count)

    def test_three_daily_runs_keep_one_source_item_and_three_snapshots(self) -> None:
        transport = FakeTransport(
            [
                GitHubResponse(status=200, headers={}, body={"items": [repository(1, "one", 10)]}),
                GitHubResponse(status=200, headers={}, body={"items": [repository(1, "one", 12)]}),
                GitHubResponse(status=200, headers={}, body={"items": [repository(1, "one", 15)]}),
            ]
        )
        with connect(self.database_path) as connection:
            provider = GitHubProvider(
                token="test-token", queries=("topic:llm",), transport=transport, recent_created_days=0
            )
            results = [
                provider.run(connection, snapshot_date=date(2026, 8, day))
                for day in (1, 2, 3)
            ]
            source_count = connection.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()
            snapshots = connection.execute(
                "SELECT snapshot_date, stars FROM metric_snapshot ORDER BY snapshot_date"
            ).fetchall()

        self.assertEqual([1, 0, 0], [result.items_new for result in results])
        self.assertEqual(1, source_count["count"])
        self.assertEqual(
            [("2026-08-01", 10), ("2026-08-02", 12), ("2026-08-03", 15)],
            [(snapshot["snapshot_date"], snapshot["stars"]) for snapshot in snapshots],
        )

    def test_optional_detail_enrichment_uses_repository_endpoint(self) -> None:
        transport = FakeTransport(
            [
                GitHubResponse(status=200, headers={}, body={"items": [repository(1, "one")]}),
                GitHubResponse(
                    status=200,
                    headers={},
                    body={**repository(1, "one", 40), "homepage": "https://details.example.com"},
                ),
            ]
        )
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token="test-token",
                queries=("topic:llm",),
                transport=transport,
                enrich_details=True,
                recent_created_days=0,
            ).run(connection, snapshot_date=date(2026, 8, 1))
            item = connection.execute("SELECT homepage FROM source_item").fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual(2, result.request_count)
        self.assertEqual("https://api.github.com/repos/team/one", transport.requests[1].full_url)
        self.assertEqual("https://details.example.com", item["homepage"])

    def test_each_topic_adds_a_created_at_query_for_weekly_new_candidates(self) -> None:
        transport = FakeTransport(
            [
                GitHubResponse(status=200, headers={}, body={"items": [repository(1, "one")]}),
                GitHubResponse(status=200, headers={}, body={"items": [repository(2, "two")]}),
            ]
        )
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token="test-token", queries=("topic:llm",), transport=transport
            ).run(connection, snapshot_date=date(2026, 8, 8))

        self.assertEqual("ok", result.status)
        queries = [parse_qs(urlparse(request.full_url).query)["q"][0] for request in transport.requests]
        self.assertEqual(["topic:llm", "topic:llm created:>=2026-08-01"], queries)

    def test_enrichment_caches_readme_images_and_homepage_og_image(self) -> None:
        readme = "# Demo\n![Screen](docs/screen.png)\n<img src=\"https://cdn.example.com/diagram.png\">"
        transport = FakeTransport(
            [
                GitHubResponse(
                    status=200,
                    headers={},
                    body={"encoding": "base64", "content": __import__("base64").b64encode(readme.encode()).decode()},
                )
            ]
        )
        homepage_requests = []

        def homepage_transport(request, _timeout):
            homepage_requests.append(request)
            return HomepageResponse(
                200,
                "https://example.com/",
                '<meta property="og:image" content="/share.png">',
            )

        with connect(self.database_path) as connection, connection:
            source_item_id = upsert_source_item(
                connection,
                source="github",
                external_id="team/demo",
                name="team/demo",
                description="demo",
                url="https://github.com/team/demo",
                homepage="https://example.com",
                raw_json={"full_name": "team/demo", "default_branch": "main"},
            )
            product_id = connection.execute(
                "INSERT INTO product(name, first_seen_at, last_updated_at) VALUES ('Demo', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            ).lastrowid
            connection.execute(
                "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
                (product_id, source_item_id),
            )
            provider = GitHubProvider(
                token="test-token",
                queries=(),
                transport=transport,
                homepage_transport=homepage_transport,
            )
            result = provider.enrich_curated_items(connection, observed_at=datetime(2026, 8, 8, tzinfo=timezone.utc))
            item = connection.execute("SELECT * FROM source_item WHERE id = ?", (source_item_id,)).fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual(1, result.readmes_fetched)
        self.assertEqual(1, result.homepage_probes)
        self.assertEqual(2, result.images_found)
        self.assertEqual(1, len(homepage_requests))
        self.assertEqual(readme, item["readme_text"])
        self.assertEqual(
            [
                "https://raw.githubusercontent.com/team/demo/main/docs/screen.png",
                "https://cdn.example.com/diagram.png",
            ],
            json.loads(item["readme_images"]),
        )
        self.assertEqual("https://example.com/share.png", item["og_image"])

    def test_startup_prefill_writes_a_timestamped_stargazer_delta(self) -> None:
        transport = FakeTransport(
            [
                GitHubResponse(
                    status=200,
                    headers={},
                    body=[
                        {"starred_at": "2026-08-07T12:00:00Z", "user": {}},
                        {"starred_at": "2026-07-01T12:00:00Z", "user": {}},
                    ],
                )
            ]
        )
        with connect(self.database_path) as connection, connection:
            source_item_id = upsert_source_item(
                connection,
                source="github",
                external_id="team/demo",
                name="team/demo",
                description="demo",
                url="https://github.com/team/demo",
                raw_json={"full_name": "team/demo"},
            )
            product_id = connection.execute(
                "INSERT INTO product(name, first_seen_at, last_updated_at) VALUES ('Demo', '2026-08-08T00:00:00+00:00', '2026-08-08T00:00:00+00:00')"
            ).lastrowid
            connection.execute(
                "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, 'github', 1)",
                (product_id, source_item_id),
            )
            upsert_metric_snapshot(
                connection,
                source_item_id=source_item_id,
                snapshot_date=date(2026, 8, 8),
                stars=100,
                forks=5,
            )
            provider = GitHubProvider(token="test-token", queries=(), transport=transport)
            result = provider.prefill_curated_star_deltas(connection, snapshot_date=date(2026, 8, 8))
            snapshot = connection.execute("SELECT * FROM metric_snapshot").fetchone()

        self.assertEqual("ok", result.status)
        self.assertEqual(1, result.items_prefilled)
        self.assertEqual(1, snapshot["stars_delta_prefill"])
        self.assertEqual(0, snapshot["forks_delta_prefill"])
        self.assertEqual(7, snapshot["prefill_window_days"])
        self.assertEqual("application/vnd.github.star+json", transport.requests[0].headers["Accept"])

    def test_network_failure_after_retries_is_recorded_as_failed(self) -> None:
        transport = FakeTransport([URLError("offline"), URLError("offline"), URLError("offline")])
        with connect(self.database_path) as connection:
            result = GitHubProvider(
                token="test-token",
                queries=("topic:llm",),
                transport=transport,
                sleep=lambda _delay: None,
            ).run(connection, snapshot_date=date(2026, 8, 1))
            log = connection.execute("SELECT * FROM run_log").fetchone()

        self.assertEqual("failed", result.status)
        self.assertEqual(3, result.request_count)
        self.assertIn("offline", result.error or "")
        self.assertEqual({"github": "failed"}, json.loads(log["provider_status"]))
