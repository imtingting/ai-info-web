"""GitHub REST Search provider for daily repository snapshots."""

from __future__ import annotations

import json
import re
import time
from base64 import b64decode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from ai_info_web.db import (
    record_run_log,
    update_source_enrichment,
    upsert_metric_snapshot,
    upsert_source_item,
)


SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
API_VERSION = "2022-11-28"


class GitHubProviderError(RuntimeError):
    """A GitHub provider error that makes the critical source unavailable."""


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    headers: Mapping[str, str]
    body: Any


@dataclass(frozen=True)
class GitHubRunResult:
    status: str
    items_seen: int
    items_new: int
    request_count: int
    error: str | None = None


@dataclass(frozen=True)
class GitHubEnrichmentResult:
    status: str
    readmes_fetched: int
    homepage_probes: int
    images_found: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitHubPrefillResult:
    status: str
    items_considered: int
    items_prefilled: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class HomepageResponse:
    status: int
    url: str
    body: str


Transport = Callable[[Request, float], GitHubResponse]
HomepageTransport = Callable[[Request, float], HomepageResponse]
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
        recent_created_days: int = 7,
        request_timeout_seconds: float = 20.0,
        homepage_timeout_seconds: float = 10.0,
        max_enrichment_items: int = 20,
        max_stargazer_prefill_items: int = 30,
        max_retries: int = 2,
        enrich_details: bool = False,
        homepage_transport: HomepageTransport | None = None,
    ) -> None:
        self.token = token
        self.queries = tuple(queries)
        self.transport = transport or _urlopen_transport
        self.sleep = sleep
        self.clock = clock
        self.pages_per_query = pages_per_query
        self.recent_created_days = recent_created_days
        self.request_timeout_seconds = request_timeout_seconds
        self.homepage_timeout_seconds = homepage_timeout_seconds
        self.max_enrichment_items = max_enrichment_items
        self.max_stargazer_prefill_items = max_stargazer_prefill_items
        self.max_retries = max_retries
        self.enrich_details = enrich_details
        self.homepage_transport = homepage_transport or _urlopen_homepage_transport
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
            candidates = self.fetch_candidates(run_date=run_date)
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
                    github_created_at=_as_string(repository.get("created_at")),
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

    def fetch_candidates(self, *, run_date: date | None = None) -> list[dict[str, Any]]:
        """Return unique repositories from the configured Search API queries."""
        candidates: dict[int, dict[str, Any]] = {}
        for query in self._search_queries(run_date or datetime.now(timezone.utc).date()):
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

    def enrich_curated_items(self, connection: Any, *, observed_at: datetime | None = None) -> GitHubEnrichmentResult:
        """Collect optional README and og:image metadata for curated GitHub items.

        This happens only after curation, is cached per source item, and never
        raises into the critical search-and-publish path.
        """
        timestamp = (observed_at or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
        rows = connection.execute(
            """
            SELECT DISTINCT source_item.*
            FROM source_item
            JOIN product_source ON product_source.source_item_id = source_item.id
            WHERE source_item.source IN ('github', 'github_trending_observation')
              AND (
                source_item.readme_checked_at IS NULL
                OR (source_item.homepage IS NOT NULL AND source_item.og_image_checked_at IS NULL)
              )
            ORDER BY source_item.github_created_at DESC, source_item.id DESC
            LIMIT ?
            """,
            (self.max_enrichment_items,),
        ).fetchall()
        readmes_fetched = homepage_probes = images_found = 0
        errors: list[str] = []
        with connection:
            for row in rows:
                full_name = _repository_full_name(row)
                if not row["readme_checked_at"] and full_name:
                    try:
                        readme_text = self.fetch_repository_readme(full_name)
                        images = extract_readme_images(readme_text, full_name, _default_branch(row))
                        update_source_enrichment(
                            connection,
                            source_item_id=row["id"],
                            readme_text=readme_text,
                            readme_images=images,
                            readme_checked_at=timestamp,
                        )
                        readmes_fetched += 1
                        images_found += len(images)
                    except GitHubProviderError as error:
                        errors.append(f"README {full_name}: {error}")
                if not row["og_image_checked_at"] and row["homepage"]:
                    homepage_probes += 1
                    og_image = self.fetch_homepage_og_image(row["homepage"])
                    update_source_enrichment(
                        connection,
                        source_item_id=row["id"],
                        og_image=og_image,
                        og_image_checked_at=timestamp,
                    )
        return GitHubEnrichmentResult(
            status="degraded" if errors else "ok",
            readmes_fetched=readmes_fetched,
            homepage_probes=homepage_probes,
            images_found=images_found,
            errors=tuple(errors),
        )

    def fetch_repository_details(self, full_name: str) -> dict[str, Any]:
        """Fetch repository details for a future post-filter enrichment step."""
        return self._request_json(f"https://api.github.com/repos/{full_name}")

    def fetch_repository_readme(self, full_name: str) -> str | None:
        """Fetch and bound a README through GitHub's documented contents API."""
        try:
            payload = self._request_json(f"https://api.github.com/repos/{full_name}/readme")
        except GitHubProviderError as error:
            if "HTTP 404" in str(error):
                return None
            raise
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            return None
        try:
            text = b64decode(payload["content"], validate=False).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return None
        return text[:8000]

    def fetch_homepage_og_image(self, homepage: str) -> str | None:
        """Return a homepage's HTTPS og:image URL without downloading the image."""
        if not _is_https_url(homepage):
            return None
        try:
            response = self.homepage_transport(
                Request(homepage, headers={"Accept": "text/html", "User-Agent": "ai-info-web"}),
                self.homepage_timeout_seconds,
            )
        except (HTTPError, URLError, TimeoutError, OSError):
            return None
        if response.status >= 400 or not _is_https_url(response.url):
            return None
        return extract_og_image(response.body, response.url)

    def prefill_curated_star_deltas(
        self, connection: Any, *, snapshot_date: date | None = None
    ) -> GitHubPrefillResult:
        """Seed startup heat windows from GitHub's timestamped stargazer API.

        The prefill is deliberately bounded to high-signal curated repositories.
        It only supplies the star delta: GitHub does not expose historical fork
        timestamps, so the matching startup fork delta remains zero.
        """
        score_date = snapshot_date or datetime.now(timezone.utc).date()
        available_days = connection.execute(
            """
            SELECT COUNT(DISTINCT metric_snapshot.snapshot_date) AS count
            FROM metric_snapshot
            JOIN source_item ON source_item.id = metric_snapshot.source_item_id
            WHERE source_item.source = 'github'
            """
        ).fetchone()["count"]
        if available_days >= 7 or not self.token:
            return GitHubPrefillResult("ok", 0, 0)
        rows = connection.execute(
            """
            SELECT source_item.*, latest.stars, latest.forks
            FROM source_item
            JOIN product_source ON product_source.source_item_id = source_item.id
            JOIN metric_snapshot AS latest
              ON latest.source_item_id = source_item.id
             AND latest.snapshot_date = ?
            WHERE source_item.source = 'github'
              AND NOT EXISTS (
                SELECT 1 FROM metric_snapshot AS prior
                WHERE prior.source_item_id = source_item.id
                  AND prior.snapshot_date < ?
              )
              AND latest.stars_delta_prefill IS NULL
            ORDER BY latest.stars DESC, source_item.id ASC
            LIMIT ?
            """,
            (score_date.isoformat(), score_date.isoformat(), self.max_stargazer_prefill_items),
        ).fetchall()
        errors: list[str] = []
        prefilled = 0
        cutoff = datetime.combine(score_date - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
        with connection:
            for row in rows:
                full_name = _repository_full_name(row)
                if not full_name:
                    continue
                try:
                    stars_delta = self.count_stargazers_since(full_name, cutoff=cutoff)
                except GitHubProviderError as error:
                    errors.append(f"stargazers {full_name}: {error}")
                    continue
                upsert_metric_snapshot(
                    connection,
                    source_item_id=row["id"],
                    snapshot_date=score_date,
                    stars=_as_int(row["stars"]),
                    forks=_as_int(row["forks"]),
                    stars_delta_prefill=stars_delta,
                    forks_delta_prefill=0,
                    prefill_window_days=7,
                )
                prefilled += 1
        return GitHubPrefillResult(
            "degraded" if errors else "ok", len(rows), prefilled, tuple(errors)
        )

    def count_stargazers_since(self, full_name: str, *, cutoff: datetime) -> int:
        """Count recent timestamped stargazers, stopping once the weekly window ends."""
        url = f"https://api.github.com/repos/{full_name}/stargazers?per_page=100"
        count = 0
        while url:
            response = self._request(url, accept="application/vnd.github.star+json")
            if not isinstance(response.body, list):
                raise GitHubProviderError("GitHub stargazer response did not contain an array.")
            entries = [entry for entry in response.body if isinstance(entry, dict)]
            for entry in entries:
                starred_at = _parse_github_time(entry.get("starred_at"))
                if starred_at is not None and starred_at >= cutoff:
                    count += 1
            timestamps = [
                timestamp
                for entry in entries
                if (timestamp := _parse_github_time(entry.get("starred_at"))) is not None
            ]
            oldest = min(timestamps, default=None)
            if oldest is not None and oldest < cutoff:
                break
            url = _next_link(response.headers.get("Link") or response.headers.get("link"))
        return count

    def _search_queries(self, run_date: date) -> tuple[str, ...]:
        """Pair each topic query with a created-at query for the weekly-new feed."""
        if self.recent_created_days <= 0:
            return self.queries
        cutoff = (run_date - timedelta(days=self.recent_created_days)).isoformat()
        created_queries = tuple(f"{query} created:>={cutoff}" for query in self.queries)
        return self.queries + created_queries

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
        if not isinstance(response.body, Mapping):
            raise GitHubProviderError("GitHub API response did not contain an object.")
        return dict(response.body)

    def _request(self, url: str, *, accept: str = "application/vnd.github+json") -> GitHubResponse:
        last_error: str | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                url,
                headers={
                    "Accept": accept,
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
            body=payload if isinstance(payload, (dict, list)) else {},
        )


def _urlopen_homepage_transport(request: Request, timeout: float) -> HomepageResponse:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - homepage is validated HTTPS
        return HomepageResponse(
            status=response.status,
            url=response.geturl(),
            body=response.read(512_000).decode("utf-8", errors="replace"),
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


def _as_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_github_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _repository_full_name(row: Mapping[str, Any]) -> str | None:
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        raw = {}
    full_name = raw.get("full_name") if isinstance(raw, dict) else None
    if isinstance(full_name, str) and full_name.count("/") == 1:
        return full_name
    parsed = urlparse(row["url"] or "")
    parts = [part for part in parsed.path.split("/") if part]
    return "/".join(parts[:2]) if parsed.netloc == "github.com" and len(parts) >= 2 else None


def _default_branch(row: Mapping[str, Any]) -> str:
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        raw = {}
    branch = raw.get("default_branch") if isinstance(raw, dict) else None
    return branch if isinstance(branch, str) and branch else "main"


def extract_readme_images(readme_text: str | None, full_name: str, default_branch: str) -> list[str]:
    """Extract de-duplicated HTTPS image URLs from Markdown and HTML README markup."""
    if not readme_text:
        return []
    base = f"https://raw.githubusercontent.com/{full_name}/{default_branch}/"
    matches = []
    for pattern in (
        r"!\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)",
        r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
    ):
        matches.extend(re.findall(pattern, readme_text, flags=re.IGNORECASE))
    images: list[str] = []
    for value in matches:
        resolved = urljoin(base, value.strip("<>"))
        if _is_https_url(resolved) and resolved not in images:
            images.append(resolved)
    return images


def extract_og_image(document: str, base_url: str) -> str | None:
    """Read the first HTTPS Open Graph image reference from a homepage document."""
    for tag in re.findall(r"<meta\b[^>]*>", document, flags=re.IGNORECASE):
        property_match = re.search(r"\b(?:property|name)=[\"']og:image[\"']", tag, flags=re.IGNORECASE)
        content_match = re.search(r"\bcontent=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
        if property_match and content_match:
            candidate = urljoin(base_url, content_match.group(1))
            return candidate if _is_https_url(candidate) else None
    return None


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)
