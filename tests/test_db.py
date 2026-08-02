from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_info_web.db import connect, initialize_database, upsert_metric_snapshot, upsert_source_item


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "state" / "ai-info.sqlite3"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initialization_creates_all_product_plan_tables(self) -> None:
        initialize_database(self.database_path)

        with connect(self.database_path) as connection:
            table_names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertTrue(
            {
                "source_item",
                "metric_snapshot",
                "product",
                "product_source",
                "run_log",
                "summary_cache",
                "summary_usage",
                "schema_migration",
            }.issubset(table_names)
        )

    def test_initialization_is_idempotent(self) -> None:
        initialize_database(self.database_path)
        initialize_database(self.database_path)

        with connect(self.database_path) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migration ORDER BY version"
            ).fetchall()

        self.assertEqual([1, 2], [row["version"] for row in versions])

    def test_source_item_and_daily_snapshot_upserts_do_not_duplicate(self) -> None:
        initialize_database(self.database_path)

        with connect(self.database_path) as connection, connection:
            first_id = upsert_source_item(
                connection,
                source="github",
                external_id="owner/repository",
                name="Repository",
                description="first description",
                topics=["llm"],
                observed_at="2026-08-01T00:00:00+00:00",
            )
            second_id = upsert_source_item(
                connection,
                source="github",
                external_id="owner/repository",
                name="Repository",
                description="updated description",
                topics=["llm", "rag"],
                observed_at="2026-08-02T00:00:00+00:00",
            )
            upsert_metric_snapshot(
                connection,
                source_item_id=first_id,
                snapshot_date=date(2026, 8, 1),
                stars=20,
            )
            upsert_metric_snapshot(
                connection,
                source_item_id=first_id,
                snapshot_date=date(2026, 8, 1),
                stars=25,
            )
            source_count = connection.execute("SELECT COUNT(*) AS count FROM source_item").fetchone()
            item = connection.execute("SELECT * FROM source_item").fetchone()
            snapshot_count = connection.execute(
                "SELECT COUNT(*) AS count FROM metric_snapshot"
            ).fetchone()
            snapshot = connection.execute("SELECT * FROM metric_snapshot").fetchone()

        self.assertEqual(first_id, second_id)
        self.assertEqual(1, source_count["count"])
        self.assertEqual("2026-08-01T00:00:00+00:00", item["first_seen_at"])
        self.assertEqual("2026-08-02T00:00:00+00:00", item["last_seen_at"])
        self.assertEqual("updated description", item["description"])
        self.assertEqual(1, snapshot_count["count"])
        self.assertEqual(25, snapshot["stars"])

    def test_source_item_records_structured_github_creation_time(self) -> None:
        initialize_database(self.database_path)
        with connect(self.database_path) as connection, connection:
            upsert_source_item(
                connection,
                source="github",
                external_id="owner/repository",
                name="Repository",
                github_created_at="2026-08-01T00:00:00Z",
            )
            item = connection.execute("SELECT github_created_at FROM source_item").fetchone()

        self.assertEqual("2026-08-01T00:00:00Z", item["github_created_at"])

    def test_metric_snapshot_requires_known_source_item(self) -> None:
        initialize_database(self.database_path)
        with connect(self.database_path) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                upsert_metric_snapshot(
                    connection,
                    source_item_id=999,
                    snapshot_date=date(2026, 8, 1),
                )
