"""Command-line entry points for local project operations."""

from __future__ import annotations

import argparse
from pathlib import Path

from ai_info_web.db import initialize_database
from ai_info_web.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Info Web maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create or migrate the SQLite database")
    init_parser.add_argument("--db", type=Path, help="private SQLite database path")
    args = parser.parse_args()

    if args.command == "init":
        database_path = args.db or load_settings().database_path
        initialize_database(database_path.expanduser().resolve())
        print(f"Initialized database: {database_path.expanduser().resolve()}")


if __name__ == "__main__":
    main()
