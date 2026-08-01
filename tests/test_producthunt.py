from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.error import URLError

from ai_info_web.db import connect, initialize_database
from ai_info_web.producthunt import ProductHuntProvider, ProductHuntResponse


def post(post_id: str, name: str, votes: int = 10):
    return {
        "id": post_id, "name": name, "tagline": f"{name} tagline", "description": f"{name} description",
        "createdAt": "2026-08-01T00:00:00Z", "dailyRank": 5, "votesCount": votes,
        "commentsCount": 3, "website": "https://example.com", "url": f"https://www.producthunt.com/posts/{name}",
        "topics": {"nodes": [{"name": "Artificial Intelligence", "slug": "artificial-intelligence"}]},
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request, _timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ProductHuntProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"
        initialize_database(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_disabled_provider_degrades_without_requests(self):
        transport = FakeTransport([])
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=False, token=None, transport=transport).run(connection, snapshot_date=date(2026, 8, 1))
            log = connection.execute("SELECT * FROM run_log").fetchone()
        self.assertEqual("degraded", result.status)
        self.assertEqual([], transport.requests)
        self.assertEqual({"producthunt": "degraded"}, json.loads(log["provider_status"]))

    def test_enabled_provider_without_credentials_degrades_without_requests(self):
        transport = FakeTransport([])
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=True, token=None, transport=transport).run(connection, snapshot_date=date(2026, 8, 1))
        self.assertEqual("degraded", result.status)
        self.assertIn("credentials", result.error or "")
        self.assertEqual([], transport.requests)

    def test_cursor_pages_persist_product_hunt_snapshots(self):
        transport = FakeTransport([
            ProductHuntResponse(200, {}, {"data": {"posts": {"edges": [{"node": post("one", "one")}], "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}}}}),
            ProductHuntResponse(200, {}, {"data": {"posts": {"edges": [{"node": post("two", "two", 20)}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
        ])
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=True, token="test-token", transport=transport, pages_per_run=2).run(connection, snapshot_date=date(2026, 8, 1))
            snapshots = connection.execute("SELECT votes_count, daily_rank FROM metric_snapshot ORDER BY votes_count").fetchall()
        self.assertEqual("ok", result.status)
        self.assertEqual((2, 2, 2), (result.items_seen, result.items_new, result.request_count))
        self.assertEqual([(10, 5), (20, 5)], [(row["votes_count"], row["daily_rank"]) for row in snapshots])
        first_request = json.loads(transport.requests[0].data.decode("utf-8"))
        second_request = json.loads(transport.requests[1].data.decode("utf-8"))
        self.assertIsNone(first_request["variables"]["after"])
        self.assertEqual("cursor-1", second_request["variables"]["after"])

    def test_rate_limit_retries_then_degrades_without_writing_posts(self):
        transport = FakeTransport([
            ProductHuntResponse(429, {"Retry-After": "2"}, {}),
            ProductHuntResponse(503, {}, {}),
            ProductHuntResponse(503, {}, {}),
        ])
        delays = []
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=True, token="test-token", transport=transport, sleep=delays.append).run(connection, snapshot_date=date(2026, 8, 1))
            count = connection.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()
        self.assertEqual("degraded", result.status)
        self.assertEqual([2.0, 1.0], delays)
        self.assertEqual((3, 0), (result.request_count, count["count"]))

    def test_network_failure_degrades_optional_source(self):
        transport = FakeTransport([URLError("offline"), URLError("offline"), URLError("offline")])
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=True, token="test-token", transport=transport, sleep=lambda _delay: None).run(connection, snapshot_date=date(2026, 8, 1))
        self.assertEqual("degraded", result.status)
        self.assertIn("offline", result.error or "")

    def test_graphql_errors_degrade_optional_source(self):
        transport = FakeTransport([ProductHuntResponse(200, {}, {"errors": [{"message": "invalid token"}]})])
        with connect(self.database_path) as connection:
            result = ProductHuntProvider(enabled=True, token="test-token", transport=transport).run(connection, snapshot_date=date(2026, 8, 1))
        self.assertEqual("degraded", result.status)
        self.assertIn("GraphQL", result.error or "")
