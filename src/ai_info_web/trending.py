"""Non-critical weekly observation of GitHub's public Trending web page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ai_info_web.db import record_run_log, upsert_metric_snapshot, upsert_source_item
from ai_info_web.github import GitHubProvider, GitHubProviderError


TRENDING_URL = "https://github.com/trending?since=weekly"


@dataclass(frozen=True)
class TrendingPageResponse:
    status: int
    body: str


@dataclass(frozen=True)
class TrendingObservationResult:
    status: str
    items_seen: int
    items_persisted: int
    error: str | None = None


class GitHubTrendingObserver:
    """Capture a bounded weekly observation without making it a critical source."""

    def __init__(self, *, github: GitHubProvider, transport=None, timeout_seconds: float = 20.0) -> None:
        self.github = github
        self.transport = transport or _urlopen_transport
        self.timeout_seconds = timeout_seconds

    def run(self, connection: Any, *, observed_at: datetime | None = None) -> TrendingObservationResult:
        timestamp = observed_at or datetime.now(timezone.utc)
        try:
            response = self.transport(
                Request(TRENDING_URL, headers={"Accept": "text/html", "User-Agent": "ai-info-web"}),
                self.timeout_seconds,
            )
            if response.status >= 400:
                raise RuntimeError(f"Trending page returned HTTP {response.status}.")
            entries = _parse_entries(response.body)[:20]
            if not entries:
                raise RuntimeError("Trending page did not contain repository entries.")
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as error:
            result = TrendingObservationResult("degraded", 0, 0, str(error))
            with connection:
                record_run_log(
                    connection,
                    run_date=timestamp.date(),
                    provider_status={"github_trending_observation": result.status},
                    items_seen=0,
                    items_new=0,
                    errors=result.error,
                )
            return result

        persisted = 0
        errors: list[str] = []
        with connection:
            for entry in entries:
                try:
                    repository = self.github.fetch_repository_details(entry["full_name"])
                except GitHubProviderError as error:
                    errors.append(f"{entry['full_name']}: {error}")
                    continue
                repository = {
                    **repository,
                    "trending_observation": {
                        "observed_at": timestamp.replace(microsecond=0).isoformat(),
                        "period": "weekly",
                        "language": entry.get("language"),
                        "stars_text": entry.get("stars_text"),
                        "source_url": TRENDING_URL,
                    },
                }
                source_item_id = upsert_source_item(
                    connection,
                    source="github_trending_observation",
                    external_id=entry["full_name"],
                    name=repository.get("full_name") or entry["full_name"],
                    description=repository.get("description") or entry.get("description"),
                    url=repository.get("html_url") or f"https://github.com/{entry['full_name']}",
                    homepage=repository.get("homepage") or None,
                    topics=repository.get("topics") or (),
                    raw_json=repository,
                    github_created_at=repository.get("created_at") if isinstance(repository.get("created_at"), str) else None,
                    observed_at=timestamp.replace(microsecond=0).isoformat(),
                )
                upsert_metric_snapshot(
                    connection,
                    source_item_id=source_item_id,
                    snapshot_date=timestamp.date(),
                    stars=repository.get("stargazers_count") if isinstance(repository.get("stargazers_count"), int) else None,
                    forks=repository.get("forks_count") if isinstance(repository.get("forks_count"), int) else None,
                )
                persisted += 1
            status = "degraded" if errors else "ok"
            result = TrendingObservationResult(status, len(entries), persisted, "; ".join(errors) or None)
            record_run_log(
                connection,
                run_date=timestamp.date(),
                provider_status={"github_trending_observation": result.status},
                items_seen=result.items_seen,
                items_new=0,
                errors=result.error,
            )
        return result


def _urlopen_transport(request: Request, timeout: float) -> TrendingPageResponse:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed GitHub Trending URL
        return TrendingPageResponse(response.status, response.read(512_000).decode("utf-8", errors="replace"))


def _parse_entries(document: str) -> list[dict[str, str]]:
    parser = _TrendingParser()
    parser.feed(document)
    parser.close()
    return parser.entries


class _TrendingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict[str, str]] = []
        self._entry: dict[str, str] | None = None
        self._in_description = False
        self._in_language = False
        self._in_stars = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = attributes.get("class") or ""
        if tag == "article" and "Box-row" in classes:
            self._entry = {}
        if self._entry is None:
            return
        if tag == "a":
            href = attributes.get("href") or ""
            parts = [part for part in href.split("/") if part]
            if len(parts) == 2 and "full_name" not in self._entry:
                self._entry["full_name"] = "/".join(parts)
        if tag == "p":
            self._in_description = True
            self._text = []
        if attributes.get("itemprop") == "programmingLanguage":
            self._in_language = True
            self._text = []
        if tag == "span" and ("stars" in classes.lower() or "float-sm-right" in classes.lower()):
            self._in_stars = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._entry is not None and (self._in_description or self._in_language or self._in_stars):
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._entry is None:
            return
        if tag == "p" and self._in_description:
            self._entry["description"] = " ".join("".join(self._text).split())
            self._in_description = False
        if tag == "span" and self._in_language:
            self._entry["language"] = " ".join("".join(self._text).split())
            self._in_language = False
        if tag == "span" and self._in_stars:
            self._entry["stars_text"] = " ".join("".join(self._text).split())
            self._in_stars = False
        if tag == "article":
            if self._entry.get("full_name"):
                self.entries.append(self._entry)
            self._entry = None
