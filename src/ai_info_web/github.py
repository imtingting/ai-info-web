"""GitHub REST Search provider for daily repository snapshots."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ai_info_web.db import record_run_log, upsert_metric_snapshot, upsert_source_item


SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
API_VERSION = "2022-11-28"


class GitHubProviderError(RuntimeError):
    """A GitHub provider error that makes the critical source unavailable."""


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]


@dataclass(frozen=True)
class GitHubRunResult:
    status: str
    items_seen: int
    items_new: int
    request_count: int
    error: str | None = None


Transport = Callable[[Request, float], GitHubResponse]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


class GitHubProvider:
    """Collect public repository metadata and write one UTC snapshot per day."""

    def __init__(
        self,
        *,
        token: str | None,
        queries: Sequence[str],
        transport: Transport | None = None,
        sleep: Sleep = time.sleep,
        clock: Clock = time.time,
        pages_per_query: int = 1,
        request_timeout_seconds: float = 20.0,
        max_retries: int = 2,
        enrich_details: bool = False,
    ) -> None:
        self.token = token
        self.queries = tuple(queries)
        self.transport = transport or _urlopen_transport
        self.sleep = sleep
        self.clock = clock
        self.pages_per_query = pages_per_query
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries
        self.enrich_details = enrich_details
        self.request_count = 0

    def run(self, connection: Any, *, snapshot_date: date | None = None) -> GitHubRunResult:
        """Fetch all configured queries and atomically persist a daily snapshot."""
        run_date = snapshot_date or datetime.now(timezone.utc).date()
        if not self.token:
            result = GitHubRunResult(
                status="failed",
                items_seen=0,
                items_new=0,
                request_count=0,
                error="GITHUB_TOKEN is required because GitHub is a critical source.",
            )
            with connection:
                record_run_log(
                    connection,
                    run_date=run_date,
                    provider_status={"github": result.status},
                    items_seen=0,
                    items_new=0,
                    errors=result.error,
                )
            return result

        try:
            candidates = self.fetch_candidates()
        except GitHubProviderError as error:
            result = GitHubRunResult(
                status="failed",
                items_seen=0,
                items_new=0,
                request_count=self.request_count,
                error=str(error),
            )
            with connection:
                record_run_log(
                    connection,
                    run_date=run_date,
                    provider_status={"github": result.status},
                    items_seen=0,
                    items_new=0,
                    errors=result.error,
                )
            return result

        items_new = 0
        with connection:
            for repository in candidates:
                external_id = str(repository["id"])
                exists = connection.execute(
                    "SELECT 1 FROM source_item WHERE source = ? AND external_id = ?",
                    ("github", external_id),
                ).fetchone()
                if exists is None:
                    items_new += 1
                source_item_id = upsert_source_item(
                    connection,
                    source="github",
                    external_id=external_id,
                    name=repository.get("full_name") or repository["name"],
                    description=repository.get("description"),
                    url=repository.get("html_url"),
                    homepage=repository.get("homepage") or None,
                    topics=repository.get("topics") or (),
                    raw_json=repository,
                )
                upsert_metric_snapshot(
                    connection,
                    source_item_id=source_item_id,
                    snapshot_date=run_date,
                    stars=_as_int(repository.get("stargazers_count")),
                    forks=_as_int(repository.get("forks_count")),
                )
            record_run_log(
                connection,
                run_date=run_date,
                provider_status={"github": "ok"},
                items_seen=len(candidates),
                items_new=items_new,
            )
        return GitHubRunResult(
            status="ok",
            items_seen=len(candidates),
            items_new=items_new,
            request_count=self.request_count,
        )

    def fetch_candidates(self) -> list[dict[str, Any]]:
        """Return unique repositories from the configured Search API queries."""
        candidates: dict[int, dict[str, Any]] = {}
        for query in self.queries:
            for repository in self._fetch_query(query):
                repository_id = repository.get("id")
                if not isinstance(repository_id, int) or not repository.get("name"):
                    continue
                if self.enrich_details and isinstance(repository.get("full_name"), str):
                    repository = {
                        **repository,
                        **self.fetch_repository_details(repository["full_name"]),
                    }
                candidates[repository_id] = repository
        return list(candidates.values())

    def fetch_repository_details(self, full_name: str) -> dict[str, Any]:
        """Fetch repository details for a future post-filter enrichment step."""
        return self._request_json(f"https://api.github.com/repos/{full_name}")

    def _fetch_query(self, query: str) -> list[dict[str, Any]]:
        url = f"{SEARCH_ENDPOINT}?{urlencode({'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': 100})}"
        results: list[dict[str, Any]] = []
        page_number = 0
        while url and page_number < self.pages_per_query:
            response = self._request(url)
            items = response.body.get("items")
            if not isinstance(items, list):
                raise GitHubProviderError("GitHub search response did not contain an items array.")
            results.extend(item for item in items if isinstance(item, dict))
            page_number += 1
            url = _next_link(response.headers.get("Link") or response.headers.get("link"))
        return results

    def _request_json(self, url: str) -> dict[str, Any]:
        response = self._request(url)
        return dict(response.body)

    def _request(self, url: str) -> GitHubResponse:
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": "ai-info-web",
                },
            )
            self.request_count += 1
            try:
                response = self.transport(request, self.request_timeout_seconds)
            except HTTPError as error:
                response = GitHubResponse(
                    status=error.code,
                    headers=dict(error.headers.items()) if error.headers else {},
                    body=_error_body(error),
                )
            except (URLError, TimeoutError) as error:
                last_error = str(error)
                if attempt == self.max_retries:
                    break
                self.sleep(_retry_delay({}, self.clock()))
                continue

            if response.status in {403, 429}:
                last_error = f"GitHub API returned HTTP {response.status}."
                if attempt == self.max_retries:
                    break
                self.sleep(_retry_delay(response.headers, self.clock()))
                continue
            if response.status >= 400:
                raise GitHubProviderError(f"GitHub API returned HTTP {response.status}.")
            return response
        raise GitHubProviderError(last_error or "GitHub API request failed.")


def _urlopen_transport(request: Request, timeout: float) -> GitHubResponse:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed GitHub API URLs
        payload = json.loads(response.read().decode("utf-8"))
        return GitHubResponse(
            status=response.status,
            headers=dict(response.headers.items()),
            body=payload,
        )


def _error_body(error: HTTPError) -> dict[str, Any]:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start != -1 and end > start:
            return part[start + 1 : end]
    return None


def _retry_delay(headers: Mapping[str, str], now: float) -> float:
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(retry_after).timestamp() - now)
            except (TypeError, ValueError):
                pass
    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(1.0, float(reset) - now)
        except ValueError:
            pass
    return 1.0


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
