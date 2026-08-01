"""Optional Product Hunt GraphQL provider, disabled by default."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ai_info_web.db import record_run_log, upsert_metric_snapshot, upsert_source_item
from ai_info_web.github import _retry_delay


GRAPHQL_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"
POSTS_QUERY = """
query Posts($after: String, $postedAfter: DateTime!) {
  posts(first: 50, after: $after, postedAfter: $postedAfter, order: NEWEST) {
    edges { cursor node { id name tagline description createdAt dailyRank votesCount commentsCount website url topics { nodes { name slug } } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ProductHuntProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductHuntResponse:
    status: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]


@dataclass(frozen=True)
class ProductHuntRunResult:
    status: str
    items_seen: int
    items_new: int
    request_count: int
    error: str | None = None


Transport = Callable[[Request, float], ProductHuntResponse]


class ProductHuntProvider:
    """Collect recent posts while degrading safely when this optional source fails."""

    def __init__(
        self,
        *,
        enabled: bool,
        token: str | None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        pages_per_run: int = 2,
        request_timeout_seconds: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        self.enabled = enabled
        self.token = token
        self.transport = transport or _urlopen_transport
        self.sleep = sleep
        self.clock = clock
        self.pages_per_run = pages_per_run
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.request_count = 0

    def run(self, connection: Any, *, snapshot_date: date | None = None) -> ProductHuntRunResult:
        run_date = snapshot_date or datetime.now(timezone.utc).date()
        if not self.enabled:
            return self._degraded(connection, run_date, "Product Hunt provider is disabled by feature flag.")
        if not self.token:
            return self._degraded(connection, run_date, "Product Hunt credentials are not configured.")
        try:
            posts = self.fetch_posts(run_date)
        except ProductHuntProviderError as error:
            return self._degraded(connection, run_date, str(error))

        items_new = 0
        with connection:
            for post in posts:
                existing = connection.execute(
                    "SELECT 1 FROM source_item WHERE source = ? AND external_id = ?",
                    ("producthunt", post["id"]),
                ).fetchone()
                items_new += existing is None
                source_item_id = upsert_source_item(
                    connection,
                    source="producthunt",
                    external_id=post["id"],
                    name=post["name"],
                    description=post.get("description") or post.get("tagline"),
                    url=post.get("url"),
                    homepage=post.get("website") or None,
                    topics=_topic_names(post.get("topics")),
                    raw_json=post,
                )
                upsert_metric_snapshot(
                    connection,
                    source_item_id=source_item_id,
                    snapshot_date=run_date,
                    votes_count=_as_int(post.get("votesCount")),
                    comments_count=_as_int(post.get("commentsCount")),
                    daily_rank=_as_int(post.get("dailyRank")),
                )
            record_run_log(
                connection,
                run_date=run_date,
                provider_status={"producthunt": "ok"},
                items_seen=len(posts),
                items_new=items_new,
            )
        return ProductHuntRunResult("ok", len(posts), items_new, self.request_count)

    def fetch_posts(self, run_date: date) -> list[dict[str, Any]]:
        cursor: str | None = None
        posts: dict[str, dict[str, Any]] = {}
        posted_after = datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
        posted_after -= timedelta(days=2)
        for _page in range(self.pages_per_run):
            response = self._request(
                {"query": POSTS_QUERY, "variables": {"after": cursor, "postedAfter": posted_after.isoformat()}}
            )
            data = response.get("data")
            posts_data = data.get("posts") if isinstance(data, dict) else None
            edges = posts_data.get("edges") if isinstance(posts_data, dict) else None
            if not isinstance(edges, list):
                raise ProductHuntProviderError("Product Hunt GraphQL response did not contain post edges.")
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict) and isinstance(node.get("id"), str) and node.get("name"):
                    posts[node["id"]] = node
            page_info = posts_data.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise ProductHuntProviderError("Product Hunt pagination cursor was missing.")
        return list(posts.values())

    def _request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                GRAPHQL_ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "ai-info-web",
                },
                method="POST",
            )
            self.request_count += 1
            try:
                response = self.transport(request, self.request_timeout_seconds)
            except HTTPError as error:
                response = ProductHuntResponse(error.code, dict(error.headers.items()) if error.headers else {}, _error_body(error))
            except (URLError, TimeoutError) as error:
                last_error = str(error)
                if attempt == self.max_retries:
                    break
                self.sleep(_retry_delay({}, self.clock()))
                continue
            if response.status == 429 or response.status >= 500:
                last_error = f"Product Hunt API returned HTTP {response.status}."
                if attempt == self.max_retries:
                    break
                self.sleep(_retry_delay(response.headers, self.clock()))
                continue
            if response.status >= 400:
                raise ProductHuntProviderError(f"Product Hunt API returned HTTP {response.status}.")
            if response.body.get("errors"):
                raise ProductHuntProviderError("Product Hunt GraphQL returned errors.")
            return response.body
        raise ProductHuntProviderError(last_error or "Product Hunt API request failed.")

    def _degraded(self, connection: Any, run_date: date, error: str) -> ProductHuntRunResult:
        result = ProductHuntRunResult("degraded", 0, 0, self.request_count, error)
        with connection:
            record_run_log(
                connection,
                run_date=run_date,
                provider_status={"producthunt": "degraded"},
                items_seen=0,
                items_new=0,
                errors=error,
            )
        return result


def _urlopen_transport(request: Request, timeout: float) -> ProductHuntResponse:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed endpoint
        return ProductHuntResponse(response.status, dict(response.headers.items()), json.loads(response.read().decode("utf-8")))


def _error_body(error: HTTPError) -> dict[str, Any]:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _topic_names(topics: Any) -> tuple[str, ...]:
    if not isinstance(topics, dict) or not isinstance(topics.get("nodes"), list):
        return ()
    return tuple(
        topic.get("slug") or topic.get("name")
        for topic in topics["nodes"]
        if isinstance(topic, dict) and isinstance(topic.get("slug") or topic.get("name"), str)
    )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
