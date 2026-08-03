"""Daily orchestration that publishes only after the critical GitHub path succeeds."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ai_info_web.curation import curate
from ai_info_web.db import connect, initialize_database, record_run_log
from ai_info_web.github import GitHubProvider
from ai_info_web.heat import calculate_heat_scores, record_rank_history
from ai_info_web.producthunt import ProductHuntProvider
from ai_info_web.settings import Settings
from ai_info_web.site import build_static_site, publish_site
from ai_info_web.state import persist_state, restore_state
from ai_info_web.summary import DeepSeekSummaryProvider


@dataclass(frozen=True)
class DailyRunResult:
    status: str
    published: bool
    restored_state: bool
    products_published: int
    provider_status: dict[str, str]


def run_daily(
    *,
    settings: Settings,
    database_path: Path,
    output_directory: Path,
    review_queue_path: Path,
    project_root: Path,
    state_directory: Path | None = None,
    run_date: date | None = None,
    github_provider=None,
    product_hunt_provider=None,
    summary_provider=None,
) -> DailyRunResult:
    """Run the pipeline and retain the previous public batch on critical failure."""
    restored = restore_state(state_directory, database_path) if state_directory else False
    initialize_database(database_path)
    today = run_date or datetime.now(timezone.utc).date()
    with connect(database_path) as connection:
        github = github_provider or GitHubProvider(
            token=os.environ.get("GITHUB_TOKEN"),
            queries=settings.github_queries,
            pages_per_query=settings.github_pages_per_query,
            recent_created_days=settings.github_recent_created_days,
            max_enrichment_items=settings.github_max_enrichment_items,
            max_stargazer_prefill_items=settings.github_max_stargazer_prefill_items,
            max_stargazer_pages=settings.github_max_stargazer_pages,
        )
        github_result = github.run(connection, snapshot_date=today)
        statuses = {"github": github_result.status}
        if github_result.status != "ok":
            statuses.update({"producthunt": "not_run", "summary": "not_run", "pipeline": "failed"})
            _record_pipeline_status(connection, today, statuses, github_result.error)
            if state_directory:
                persist_state(database_path, state_directory)
            return DailyRunResult("failed", False, restored, 0, statuses)

        product_hunt = product_hunt_provider or ProductHuntProvider(
            enabled=settings.enable_product_hunt,
            token=os.environ.get("PRODUCT_HUNT_DEVELOPER_TOKEN"),
            pages_per_run=settings.product_hunt_pages_per_run,
        )
        product_hunt_result = product_hunt.run(connection, snapshot_date=today)
        statuses["producthunt"] = product_hunt_result.status
        curation = curate(connection, rules_path=project_root / "config" / "category_rules.json", review_queue_path=review_queue_path)
        if hasattr(github, "enrich_curated_items"):
            enrichment = github.enrich_curated_items(connection)
            statuses["github_enrichment"] = enrichment.status
        else:
            statuses["github_enrichment"] = "not_run"
        if hasattr(github, "prefill_curated_star_deltas"):
            prefill = github.prefill_curated_star_deltas(connection, snapshot_date=today)
            statuses["github_prefill"] = prefill.status
        else:
            statuses["github_prefill"] = "not_run"
        calculate_heat_scores(connection, config_path=project_root / "config" / "heat_config.json", as_of_date=today)
        record_rank_history(connection, listed_at=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))
        summary = summary_provider or _summary_provider(settings, project_root)
        summary_result = summary.run(connection, run_date=today)
        statuses["summary"] = summary_result.status
        statuses["pipeline"] = "ok" if all(status == "ok" for status in statuses.values()) else "degraded"
        _record_pipeline_status(connection, today, statuses, None)
        secrets = tuple(
            value for value in (
                os.environ.get("GITHUB_TOKEN"),
                os.environ.get("PRODUCT_HUNT_DEVELOPER_TOKEN"),
                os.environ.get("DEEPSEEK_API_KEY"),
            ) if value
        )
        publication = publish_site(
            output_directory,
            lambda staging: _build_and_verify(connection, staging, secrets),
        )
    if state_directory:
        persist_state(database_path, state_directory)
    return DailyRunResult(statuses["pipeline"], True, restored, publication["products"], statuses)


def _summary_provider(settings: Settings, project_root: Path) -> DeepSeekSummaryProvider:
    config = json.loads((project_root / "config" / "summary_config.json").read_text(encoding="utf-8"))
    return DeepSeekSummaryProvider(
        enabled=settings.enable_summary,
        token=os.environ.get("DEEPSEEK_API_KEY"),
        monthly_budget_cny=settings.summary_monthly_budget_cny,
        config=config,
    )


def _build_and_verify(connection, staging: Path, secrets: tuple[str, ...]):
    from ai_info_web.site import verify_static_site

    result = build_static_site(connection, staging)
    verify_static_site(staging, forbidden_values=secrets)
    return result


def _record_pipeline_status(connection, run_date: date, statuses: dict[str, str], errors: str | None) -> None:
    with connection:
        record_run_log(
            connection,
            run_date=run_date,
            provider_status=statuses,
            items_seen=0,
            items_new=0,
            errors=errors,
        )
