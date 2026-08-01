"""Command-line entry points for local project operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ai_info_web.curation import curate
from ai_info_web.db import connect, initialize_database
from ai_info_web.github import GitHubProvider
from ai_info_web.producthunt import ProductHuntProvider
from ai_info_web.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Info Web maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create or migrate the SQLite database")
    init_parser.add_argument("--db", type=Path, help="private SQLite database path")
    github_parser = subparsers.add_parser(
        "fetch-github", help="collect GitHub repository snapshots into the private database"
    )
    github_parser.add_argument("--db", type=Path, help="private SQLite database path")
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
            ).run(connection)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        if result.status != "ok":
            raise SystemExit(2)
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


if __name__ == "__main__":
    main()
