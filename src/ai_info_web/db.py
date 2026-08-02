"""SQLite schema, migrations, and idempotent persistence helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE source_item (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL,
          external_id TEXT NOT NULL,
          raw_json TEXT,
          name TEXT NOT NULL,
          description TEXT,
          url TEXT,
          homepage TEXT,
          topics TEXT,
          content_hash TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(source, external_id)
        );

        CREATE TABLE metric_snapshot (
          id INTEGER PRIMARY KEY,
          source_item_id INTEGER NOT NULL REFERENCES source_item(id),
          snapshot_date TEXT NOT NULL,
          stars INTEGER,
          forks INTEGER,
          votes_count INTEGER,
          comments_count INTEGER,
          daily_rank INTEGER,
          UNIQUE(source_item_id, snapshot_date)
        );

        CREATE TABLE product (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          summary_zh TEXT,
          summary_status TEXT DEFAULT 'pending',
          category TEXT DEFAULT 'other',
          heat_score REAL,
          score_breakdown TEXT,
          first_seen_at TEXT,
          last_updated_at TEXT
        );

        CREATE TABLE product_source (
          product_id INTEGER NOT NULL REFERENCES product(id),
          source_item_id INTEGER NOT NULL REFERENCES source_item(id),
          source TEXT NOT NULL,
          is_primary INTEGER DEFAULT 0,
          UNIQUE(product_id, source_item_id)
        );

        CREATE TABLE run_log (
          id INTEGER PRIMARY KEY,
          run_date TEXT NOT NULL,
          provider_status TEXT,
          items_seen INTEGER,
          items_new INTEGER,
          errors TEXT
        );

        CREATE TABLE summary_cache (
          content_hash TEXT PRIMARY KEY,
          summary_zh TEXT,
          status TEXT DEFAULT 'ok',
          input_tokens INTEGER,
          output_tokens INTEGER,
          estimated_cost REAL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE summary_usage (
          month TEXT PRIMARY KEY,
          estimated_cost REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX idx_metric_snapshot_item_date
          ON metric_snapshot(source_item_id, snapshot_date);
        CREATE INDEX idx_product_source_source_item
          ON product_source(source_item_id);
        """,
    ),
    (
        2,
        """
        ALTER TABLE source_item ADD COLUMN github_created_at TEXT;
        ALTER TABLE source_item ADD COLUMN readme_text TEXT;
        ALTER TABLE source_item ADD COLUMN readme_images TEXT;
        ALTER TABLE source_item ADD COLUMN readme_checked_at TEXT;
        ALTER TABLE source_item ADD COLUMN og_image TEXT;
        ALTER TABLE source_item ADD COLUMN og_image_checked_at TEXT;
        """,
    ),
)


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a SQLite database with foreign key enforcement enabled."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: Path) -> None:
    """Create or upgrade the database; repeated calls are safe."""
    with closing(connect(database_path)) as connection:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied_versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migration")
            }
            for version, statements in MIGRATIONS:
                if version in applied_versions:
                    continue
                connection.executescript(statements)
                connection.execute(
                    "INSERT INTO schema_migration(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )


def upsert_source_item(
    connection: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    name: str,
    description: str | None = None,
    url: str | None = None,
    homepage: str | None = None,
    topics: Sequence[str] | None = None,
    raw_json: Mapping[str, Any] | None = None,
    content_hash: str | None = None,
    github_created_at: str | None = None,
    observed_at: str | None = None,
) -> int:
    """Insert or update a source item and return its stable database id."""
    timestamp = observed_at or utc_now()
    connection.execute(
        """
        INSERT INTO source_item(
          source, external_id, raw_json, name, description, url, homepage,
          topics, content_hash, github_created_at, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, external_id) DO UPDATE SET
          raw_json = excluded.raw_json,
          name = excluded.name,
          description = excluded.description,
          url = excluded.url,
          homepage = excluded.homepage,
          topics = excluded.topics,
          content_hash = excluded.content_hash,
          github_created_at = COALESCE(excluded.github_created_at, source_item.github_created_at),
          last_seen_at = excluded.last_seen_at
        """,
        (
            source,
            external_id,
            json.dumps(raw_json, sort_keys=True) if raw_json is not None else None,
            name,
            description,
            url,
            homepage,
            json.dumps(list(topics)) if topics is not None else None,
            content_hash,
            github_created_at,
            timestamp,
            timestamp,
        ),
    )
    row = connection.execute(
        "SELECT id FROM source_item WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def update_source_enrichment(
    connection: sqlite3.Connection,
    *,
    source_item_id: int,
    readme_text: str | None | object = ...,
    readme_images: Sequence[str] | None | object = ...,
    readme_checked_at: str | None | object = ...,
    og_image: str | None | object = ...,
    og_image_checked_at: str | None | object = ...,
) -> None:
    """Persist non-critical README and homepage enrichment fields.

    The sentinel defaults distinguish an unavailable value from a field that
    should be deliberately cleared after a checked request.
    """
    values: dict[str, Any] = {}
    if readme_text is not ...:
        values["readme_text"] = readme_text
    if readme_images is not ...:
        values["readme_images"] = json.dumps(list(readme_images or ()), sort_keys=True)
    if readme_checked_at is not ...:
        values["readme_checked_at"] = readme_checked_at
    if og_image is not ...:
        values["og_image"] = og_image
    if og_image_checked_at is not ...:
        values["og_image_checked_at"] = og_image_checked_at
    if not values:
        return
    assignments = ", ".join(f"{column} = ?" for column in values)
    connection.execute(
        f"UPDATE source_item SET {assignments} WHERE id = ?",  # nosec B608 - columns are fixed above
        (*values.values(), source_item_id),
    )


def upsert_metric_snapshot(
    connection: sqlite3.Connection,
    *,
    source_item_id: int,
    snapshot_date: date,
    stars: int | None = None,
    forks: int | None = None,
    votes_count: int | None = None,
    comments_count: int | None = None,
    daily_rank: int | None = None,
) -> None:
    """Store one mutable daily metrics snapshot for a source item."""
    connection.execute(
        """
        INSERT INTO metric_snapshot(
          source_item_id, snapshot_date, stars, forks, votes_count,
          comments_count, daily_rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_item_id, snapshot_date) DO UPDATE SET
          stars = excluded.stars,
          forks = excluded.forks,
          votes_count = excluded.votes_count,
          comments_count = excluded.comments_count,
          daily_rank = excluded.daily_rank
        """,
        (
            source_item_id,
            snapshot_date.isoformat(),
            stars,
            forks,
            votes_count,
            comments_count,
            daily_rank,
        ),
    )


def record_run_log(
    connection: sqlite3.Connection,
    *,
    run_date: date,
    provider_status: Mapping[str, str],
    items_seen: int,
    items_new: int,
    errors: str | None = None,
) -> None:
    """Persist the status of a pipeline run without exposing any credentials."""
    connection.execute(
        """
        INSERT INTO run_log(run_date, provider_status, items_seen, items_new, errors)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            run_date.isoformat(),
            json.dumps(dict(provider_status), sort_keys=True),
            items_seen,
            items_new,
            errors,
        ),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
