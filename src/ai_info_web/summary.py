"""DeepSeek-backed Chinese product summaries with private cache and budget ledger."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ai_info_web.db import record_run_log, utc_now


MIN_SUMMARY_LENGTH = 150
MAX_SUMMARY_LENGTH = 300


@dataclass(frozen=True)
class SummaryResponse:
    status: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]


@dataclass(frozen=True)
class SummaryRunResult:
    status: str
    products_seen: int
    generated: int
    cache_hits: int
    skipped: int
    failed: int
    request_count: int


Transport = Callable[[Request, float], SummaryResponse]


class DeepSeekSummaryProvider:
    """Generate cached 150-300 character Chinese analyses without exposing credentials."""

    def __init__(
        self,
        *,
        enabled: bool,
        token: str | None,
        monthly_budget_cny: float,
        config: Mapping[str, Any],
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.enabled = enabled
        self.token = token
        self.monthly_budget_cny = monthly_budget_cny
        self.config = config
        self.transport = transport or _urlopen_transport
        self.sleep = sleep
        self.request_count = 0

    def run(
        self,
        connection,
        *,
        run_date: date | None = None,
        max_items: int | None = None,
    ) -> SummaryRunResult:
        """Generate summaries, optionally bounding expensive cache misses per run."""
        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be zero or greater")
        today = run_date or datetime.now(timezone.utc).date()
        # Prioritize products whose README was just enriched, then products
        # without a Chinese summary. This keeps weekly enrichment and summary
        # batches focused on the same newly collected projects.
        products = connection.execute(
            """
            SELECT product.*,
                   CASE WHEN product.summary_zh IS NULL OR trim(product.summary_zh) = '' THEN 0 ELSE 1 END AS has_summary,
                   CASE WHEN EXISTS (
                     SELECT 1
                     FROM product_source
                     JOIN source_item ON source_item.id = product_source.source_item_id
                     WHERE product_source.product_id = product.id
                       AND source_item.readme_text IS NOT NULL
                       AND trim(source_item.readme_text) <> ''
                   ) THEN 0 ELSE 1 END AS has_readme
            FROM product
            ORDER BY has_readme, has_summary, product.id
            """
        ).fetchall()
        if not self.enabled or not self.token:
            reason = "summary is disabled" if not self.enabled else "DEEPSEEK_API_KEY is not configured"
            with connection:
                connection.execute(
                    "UPDATE product SET summary_status = 'skipped' WHERE summary_status != 'ok'"
                )
                record_run_log(
                    connection,
                    run_date=today,
                    provider_status={"summary": "degraded"},
                    items_seen=len(products),
                    items_new=0,
                    errors=reason,
                )
            return SummaryRunResult("degraded", len(products), 0, 0, len(products), 0, 0)

        generated = cache_hits = skipped = failed = attempted = 0
        for product in products:
            content = _summary_input(connection, product)
            if not content["descriptions"] and not content["readme_excerpt"]:
                _set_product_summary(connection, product["id"], None, "skipped")
                skipped += 1
                continue
            content_hash = _content_hash(content)
            cached = connection.execute(
                "SELECT * FROM summary_cache WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if cached is not None:
                _set_product_summary(connection, product["id"], cached["summary_zh"], cached["status"])
                if cached["status"] == "ok":
                    cache_hits += 1
                elif cached["status"] == "failed":
                    failed += 1
                else:
                    skipped += 1
                continue

            if max_items is not None and attempted >= max_items:
                # Leave the existing summary status intact so a later batch can
                # process this product instead of presenting it as a failure.
                continue

            prompt = _prompt(content)
            attempted += 1
            reservation = self._reservation_cost(prompt)
            if _monthly_usage(connection, today) + reservation > self.monthly_budget_cny:
                _set_product_summary(connection, product["id"], None, "skipped")
                skipped += 1
                continue
            try:
                response = self._request(prompt)
                summary, input_tokens, output_tokens = _parse_completion(response)
            except SummaryError:
                _cache_failure(connection, content_hash)
                _set_product_summary(connection, product["id"], None, "failed")
                failed += 1
                continue

            estimated_cost = _usage_cost(input_tokens, output_tokens, self.config)
            with connection:
                connection.execute(
                    """
                    INSERT INTO summary_cache(
                      content_hash, summary_zh, status, input_tokens, output_tokens,
                      estimated_cost, created_at
                    ) VALUES (?, ?, 'ok', ?, ?, ?, ?)
                    """,
                    (content_hash, summary, input_tokens, output_tokens, estimated_cost, utc_now()),
                )
                _add_monthly_usage(connection, today, estimated_cost)
                connection.execute(
                    "UPDATE product SET summary_zh = ?, summary_status = 'ok' WHERE id = ?",
                    (summary, product["id"]),
                )
            generated += 1

        status = "ok" if failed == 0 and skipped == 0 else "degraded"
        with connection:
            record_run_log(
                connection,
                run_date=today,
                provider_status={"summary": status},
                items_seen=len(products),
                items_new=generated,
                errors=None if status == "ok" else "some summaries were skipped or failed",
            )
        return SummaryRunResult(status, len(products), generated, cache_hits, skipped, failed, self.request_count)

    def _request(self, prompt: str) -> Mapping[str, Any]:
        last_error = "DeepSeek request failed"
        for attempt in range(int(self.config["max_retries"]) + 1):
            request = Request(
                self.config["endpoint"],
                data=json.dumps(
                    {
                        "model": self.config["model"],
                        "messages": [
                            {"role": "system", "content": "You write concise, factual Chinese product summaries."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": self.config["max_output_tokens"],
                    }
                ).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                method="POST",
            )
            self.request_count += 1
            try:
                response = self.transport(request, float(self.config["request_timeout_seconds"]))
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                last_error = str(error)
                if attempt < int(self.config["max_retries"]):
                    self.sleep(float(attempt + 1))
                    continue
                break
            if response.status >= 500 or response.status == 429:
                last_error = f"DeepSeek API returned HTTP {response.status}"
                if attempt < int(self.config["max_retries"]):
                    self.sleep(float(attempt + 1))
                    continue
                break
            if response.status >= 400:
                raise SummaryError(f"DeepSeek API returned HTTP {response.status}")
            return response.body
        raise SummaryError(last_error)

    def _reservation_cost(self, prompt: str) -> float:
        return _usage_cost(len(prompt), int(self.config["max_output_tokens"]), self.config)


class SummaryError(RuntimeError):
    """A per-product completion failure that must not stop other products."""


def _summary_input(connection, product):
    sources = connection.execute(
        """
        SELECT source_item.name, source_item.description, source_item.url, source_item.homepage,
               source_item.readme_text
        FROM product_source
        JOIN source_item ON source_item.id = product_source.source_item_id
        WHERE product_source.product_id = ?
        ORDER BY product_source.is_primary DESC, source_item.id ASC
        """,
        (product["id"],),
    ).fetchall()
    descriptions = [source["description"].strip() for source in sources if source["description"] and source["description"].strip()]
    links = [link for source in sources for link in (source["url"], source["homepage"]) if link]
    readmes = [source["readme_text"].strip() for source in sources if source["readme_text"] and source["readme_text"].strip()]
    return {
        "name": product["name"],
        "descriptions": descriptions,
        "links": sorted(set(links)),
        "readme_excerpt": "\n\n".join(readmes)[:8000],
        "basis": "README" if readmes else "简介",
    }


def _content_hash(content: Mapping[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt(content: Mapping[str, Any]) -> str:
    descriptions = "\n".join(f"- {item}" for item in content["descriptions"])
    links = "\n".join(f"- {item}" for item in content["links"])
    return (
        "请用简体中文写一段严格为 150 到 300 字（含标点）的客观项目分析。只根据给出的资料，依次说清："
        "项目是什么、解决什么问题、怎样实现或使用、为什么可能受到关注。资料未说明的内容不要编造；避免营销口号、"
        "避免提及你自己。输出一个自然段，不要标题或项目符号。\n"
        f"产品名称：{content['name']}\n来源基础：{content['basis']}\n简介：\n{descriptions}\n"
        f"README 摘要：\n{content['readme_excerpt'] or '未提供 README'}\n来源链接：\n{links}"
    )


def _parse_completion(body: Mapping[str, Any]) -> tuple[str, int, int]:
    if not isinstance(body, Mapping):
        raise SummaryError("DeepSeek response is not a JSON object")
    choices = body.get("choices")
    usage = body.get("usage")
    if not isinstance(choices, list) or not choices or not isinstance(usage, Mapping):
        raise SummaryError("DeepSeek response is missing choices or usage")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    summary = message.get("content", "").strip() if isinstance(message, Mapping) else ""
    if not summary:
        raise SummaryError("DeepSeek response did not contain summary content")
    return (
        _normalize_summary(summary),
        _as_int(usage.get("prompt_tokens")),
        _as_int(usage.get("completion_tokens")),
    )


def _normalize_summary(summary: str) -> str:
    """Keep model output within the analysis page's fixed summary budget."""
    normalized = " ".join(summary.split())
    if len(normalized) < MIN_SUMMARY_LENGTH:
        raise SummaryError(
            f"DeepSeek response was shorter than {MIN_SUMMARY_LENGTH} characters"
        )
    if len(normalized) <= MAX_SUMMARY_LENGTH:
        return normalized

    sentence_end = max(
        (
            index + 1
            for index, character in enumerate(normalized[:MAX_SUMMARY_LENGTH])
            if character in "。！？；"
            and index + 1 >= MIN_SUMMARY_LENGTH
        ),
        default=0,
    )
    if sentence_end:
        return normalized[:sentence_end]
    return normalized[: MAX_SUMMARY_LENGTH - 1].rstrip() + "…"


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _usage_cost(input_tokens: int, output_tokens: int, config: Mapping[str, Any]) -> float:
    return (
        input_tokens * float(config["input_cost_cny_per_million_tokens"])
        + output_tokens * float(config["output_cost_cny_per_million_tokens"])
    ) / 1_000_000


def _monthly_usage(connection, today: date) -> float:
    row = connection.execute("SELECT estimated_cost FROM summary_usage WHERE month = ?", (today.strftime("%Y-%m"),)).fetchone()
    return float(row["estimated_cost"]) if row is not None else 0.0


def _add_monthly_usage(connection, today: date, cost: float) -> None:
    connection.execute(
        """
        INSERT INTO summary_usage(month, estimated_cost) VALUES (?, ?)
        ON CONFLICT(month) DO UPDATE SET estimated_cost = summary_usage.estimated_cost + excluded.estimated_cost
        """,
        (today.strftime("%Y-%m"), cost),
    )


def _set_product_summary(connection, product_id: int, summary: str | None, status: str) -> None:
    with connection:
        connection.execute(
            "UPDATE product SET summary_zh = ?, summary_status = ? WHERE id = ?",
            (summary, status, product_id),
        )


def _cache_failure(connection, content_hash: str) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO summary_cache(content_hash, status, created_at) VALUES (?, 'failed', ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (content_hash, utc_now()),
        )


def _urlopen_transport(request: Request, timeout: float) -> SummaryResponse:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed configured API endpoint
        return SummaryResponse(
            status=response.status,
            headers=dict(response.headers.items()),
            body=json.loads(response.read().decode("utf-8")),
        )
