"""Command-line entry points for local project operations."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from ai_info_web.curation import curate
from ai_info_web.db import connect, initialize_database
from ai_info_web.github import GitHubProvider
from ai_info_web.heat import calculate_heat_scores
from ai_info_web.producthunt import ProductHuntProvider
from ai_info_web.pipeline import run_daily
from ai_info_web.settings import load_settings
from ai_info_web.state import persist_state, restore_state
from ai_info_web.site import build_static_site, publish_site, verify_static_site
from ai_info_web.summary import DeepSeekSummaryProvider
from ai_info_web.trending import GitHubTrendingObserver


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Info Web maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create or migrate the SQLite database")
    init_parser.add_argument("--db", type=Path, help="private SQLite database path")
    github_parser = subparsers.add_parser(
        "fetch-github", help="collect GitHub repository snapshots into the private database"
    )
    github_parser.add_argument("--db", type=Path, help="private SQLite database path")
    trending_parser = subparsers.add_parser(
        "observe-trending", help="capture the non-critical GitHub Trending weekly observation"
    )
    trending_parser.add_argument("--db", type=Path, help="private SQLite database path")
    product_hunt_parser = subparsers.add_parser(
        "fetch-product-hunt", help="collect optional Product Hunt post snapshots"
    )
    product_hunt_parser.add_argument("--db", type=Path, help="private SQLite database path")
    curate_parser = subparsers.add_parser(
        "curate", help="clean sources and rebuild conservatively merged products"
    )
    curate_parser.add_argument("--db", type=Path, help="private SQLite database path")
    curate_parser.add_argument(
        "--review-queue", type=Path, help="private weak-match review queue path"
    )
    heat_parser = subparsers.add_parser(
        "score-heat", help="calculate product heat scores"
    )
    heat_parser.add_argument("--db", type=Path, help="private SQLite database path")
    heat_parser.add_argument(
        "--date", type=date.fromisoformat, help="UTC scoring date (YYYY-MM-DD)"
    )
    summary_parser = subparsers.add_parser("summarize", help="generate cached Chinese product summaries")
    summary_parser.add_argument("--db", type=Path, help="private SQLite database path")
    readme_parser = subparsers.add_parser("enrich-readmes", help="backfill bounded GitHub README context")
    readme_parser.add_argument("--db", type=Path, help="private SQLite database path")
    readme_parser.add_argument("--state-dir", type=Path, help="private persisted state directory to restore and update")
    static_parser = subparsers.add_parser("build-static", help="build a verified static package from private state")
    static_parser.add_argument("--db", type=Path, help="private SQLite database path")
    static_parser.add_argument("--output", type=Path, default=Path("public"), help="static output directory")
    daily_parser = subparsers.add_parser("run-daily", help="run the publishable daily pipeline")
    daily_parser.add_argument("--db", type=Path, help="private SQLite database path")
    daily_parser.add_argument("--output", type=Path, default=Path("public"), help="static output directory")
    daily_parser.add_argument("--state-dir", type=Path, help="private persisted state directory")
    daily_parser.add_argument("--review-queue", type=Path, help="private weak-match review queue path")
    args = parser.parse_args()

    if args.command == "init":
        database_path = args.db or load_settings().database_path
        initialize_database(database_path.expanduser().resolve())
        print(f"Initialized database: {database_path.expanduser().resolve()}")
        return

    if args.command == "fetch-github":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        with connect(database_path) as connection:
            result = GitHubProvider(
                token=os.environ.get("GITHUB_TOKEN"),
                queries=settings.github_queries,
                pages_per_query=settings.github_pages_per_query,
                recent_created_days=settings.github_recent_created_days,
                max_enrichment_items=settings.github_max_enrichment_items,
                max_stargazer_prefill_items=settings.github_max_stargazer_prefill_items,
                max_stargazer_pages=settings.github_max_stargazer_pages,
            ).run(connection)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        if result.status != "ok":
            raise SystemExit(2)
        return

    if args.command == "observe-trending":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        with connect(database_path) as connection:
            github = GitHubProvider(
                token=os.environ.get("GITHUB_TOKEN"),
                queries=settings.github_queries,
                pages_per_query=settings.github_pages_per_query,
                recent_created_days=settings.github_recent_created_days,
                max_enrichment_items=settings.github_max_enrichment_items,
                max_stargazer_prefill_items=settings.github_max_stargazer_prefill_items,
                max_stargazer_pages=settings.github_max_stargazer_pages,
            )
            result = GitHubTrendingObserver(github=github).run(connection)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "fetch-product-hunt":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        with connect(database_path) as connection:
            result = ProductHuntProvider(
                enabled=settings.enable_product_hunt,
                token=os.environ.get("PRODUCT_HUNT_DEVELOPER_TOKEN"),
                pages_per_run=settings.product_hunt_pages_per_run,
            ).run(connection)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "curate":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        project_root = Path(__file__).resolve().parents[2]
        review_queue_path = (
            args.review_queue or database_path.with_name("weak_match_review.json")
        ).expanduser().resolve()
        with connect(database_path) as connection:
            result = curate(
                connection,
                rules_path=project_root / "config" / "category_rules.json",
                review_queue_path=review_queue_path,
            )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "score-heat":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        project_root = Path(__file__).resolve().parents[2]
        with connect(database_path) as connection:
            result = calculate_heat_scores(
                connection,
                config_path=project_root / "config" / "heat_config.json",
                as_of_date=args.date,
            )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "summarize":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        initialize_database(database_path)
        project_root = Path(__file__).resolve().parents[2]
        summary_config = json.loads(
            (project_root / "config" / "summary_config.json").read_text(encoding="utf-8")
        )
        with connect(database_path) as connection:
            result = DeepSeekSummaryProvider(
                enabled=settings.enable_summary,
                token=os.environ.get("DEEPSEEK_API_KEY"),
                monthly_budget_cny=settings.summary_monthly_budget_cny,
                config=summary_config,
            ).run(connection, max_items=settings.summary_max_items)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "enrich-readmes":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        state_directory = args.state_dir.expanduser().resolve() if args.state_dir else None
        if state_directory:
            restore_state(state_directory, database_path)
        initialize_database(database_path)
        with connect(database_path) as connection:
            result = GitHubProvider(
                token=os.environ.get("GITHUB_TOKEN"),
                queries=settings.github_queries,
                pages_per_query=settings.github_pages_per_query,
                recent_created_days=settings.github_recent_created_days,
                max_enrichment_items=settings.github_max_enrichment_items,
                max_stargazer_prefill_items=0,
                max_stargazer_pages=settings.github_max_stargazer_pages,
            ).enrich_curated_items(connection)
        if state_directory:
            persist_state(database_path, state_directory)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "build-static":
        database_path = (args.db or load_settings().database_path).expanduser().resolve()
        output_directory = args.output.expanduser().resolve()
        initialize_database(database_path)
        secrets = tuple(
            value
            for value in (os.environ.get("GITHUB_TOKEN"), os.environ.get("DEEPSEEK_API_KEY"))
            if value
        )
        with connect(database_path) as connection:
            publication = publish_site(
                output_directory,
                lambda staging: _build_and_verify(connection, staging, secrets),
            )
        print(json.dumps(publication, ensure_ascii=False, sort_keys=True))
        return

    if args.command == "run-daily":
        settings = load_settings()
        database_path = (args.db or settings.database_path).expanduser().resolve()
        output_directory = args.output.expanduser().resolve()
        review_queue_path = (
            args.review_queue or database_path.with_name("weak_match_review.json")
        ).expanduser().resolve()
        state_directory = args.state_dir.expanduser().resolve() if args.state_dir else None
        project_root = Path(__file__).resolve().parents[2]
        result = run_daily(
            settings=settings,
            database_path=database_path,
            output_directory=output_directory,
            review_queue_path=review_queue_path,
            project_root=project_root,
            state_directory=state_directory,
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        if not result.published:
            raise SystemExit(2)
        return


def _build_and_verify(connection, staging: Path, secrets: tuple[str, ...]):
    result = build_static_site(connection, staging)
    verify_static_site(staging, forbidden_values=secrets)
    return result


if __name__ == "__main__":
    main()
