"""Heat scoring and independent ranking helpers for curated products."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_info_web.db import upsert_rank_history


@dataclass(frozen=True)
class HeatRunResult:
    products_scored: int
    product_hunt_available: bool


@dataclass(frozen=True)
class RankHistoryPage:
    items: tuple
    total_items: int
    page: int
    page_size: int


def calculate_heat_scores(
    connection,
    *,
    config_path: Path,
    as_of_date: date | None = None,
) -> HeatRunResult:
    """Persist heat scores using only metrics attached to the curated product set."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    score_date = as_of_date or datetime.now(timezone.utc).date()
    products = connection.execute("SELECT * FROM product ORDER BY id").fetchall()
    metrics = [_product_metrics(connection, product, score_date) for product in products]
    product_hunt_available = any(metric["producthunt"] is not None for metric in metrics)
    _apply_normalisation(metrics)

    with connection:
        for metric in metrics:
            breakdown, score = _score_product(
                metric,
                config=config,
                score_date=score_date,
            )
            connection.execute(
                "UPDATE product SET heat_score = ?, score_breakdown = ? WHERE id = ?",
                (score, json.dumps(breakdown, sort_keys=True), metric["product"]["id"]),
            )
    return HeatRunResult(len(metrics), product_hunt_available)


def rank_hot_products(connection, *, limit: int = 20):
    """Return products with a real snapshot window or a startup prefill only."""
    ranked = []
    for product in connection.execute(
        "SELECT * FROM product ORDER BY heat_score DESC, last_updated_at DESC, id DESC"
    ):
        breakdown = _json_object(product["score_breakdown"])
        github = breakdown.get("github")
        if not isinstance(github, dict) or int(github.get("window_days") or 0) <= 0:
            continue
        ranked.append(product)
        if len(ranked) == limit:
            break
    return ranked


def rank_new_products(
    connection,
    *,
    now: datetime | None = None,
    window_days: int = 7,
    limit: int = 20,
):
    """Return weekly-new products using GitHub's creation time, ranked by stars."""
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=window_days)
    candidates: dict[int, tuple] = {}
    rows = connection.execute(
        """
        SELECT product.*, source_item.id AS source_item_id, source_item.source,
               source_item.github_created_at, source_item.raw_json
        FROM product
        JOIN product_source ON product_source.product_id = product.id
        JOIN source_item ON source_item.id = product_source.source_item_id
        WHERE source_item.source LIKE 'github%'
        """
    ).fetchall()
    for row in rows:
        created_at = _source_created_at(row)
        if created_at is None or created_at < cutoff:
            continue
        stars = _latest_github_stars(connection, row["source_item_id"])
        current = candidates.get(row["id"])
        candidate = (row, stars, created_at)
        if current is None or (stars, created_at, row["source_item_id"]) > (
            current[1],
            current[2],
            current[0]["source_item_id"],
        ):
            candidates[row["id"]] = candidate
    ranked = sorted(
        candidates.values(),
        key=lambda item: (item[1], item[2], item[0]["id"]),
        reverse=True,
    )
    return [item[0] for item in ranked[:limit]]


def record_rank_history(connection, *, listed_at: datetime | None = None) -> None:
    """Persist the public weekly-new and hot membership union for later paging."""
    timestamp = (listed_at or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    rankings = {
        "weekly_new": rank_new_products(connection, now=listed_at),
        "hot": rank_hot_products(connection),
    }
    with connection:
        for list_name, products in rankings.items():
            for rank, product in enumerate(products, start=1):
                source_item_id = _history_source_item_id(connection, product["id"])
                if source_item_id is not None:
                    upsert_rank_history(
                        connection,
                        source_item_id=source_item_id,
                        list_name=list_name,
                        listed_at=timestamp,
                        rank=rank,
                    )


def rank_history_page(connection, *, page: int = 1, page_size: int = 30) -> RankHistoryPage:
    """Return the de-duplicated historical union of weekly-new and hot entries."""
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive.")
    total = connection.execute(
        "SELECT COUNT(DISTINCT source_item_id) AS count FROM rank_history"
    ).fetchone()["count"]
    rows = connection.execute(
        """
        SELECT source_item.*, MAX(rank_history.last_listed_at) AS rank_last_listed_at,
               GROUP_CONCAT(rank_history.list_name) AS rank_sources
        FROM rank_history
        JOIN source_item ON source_item.id = rank_history.source_item_id
        GROUP BY source_item.id
        ORDER BY rank_last_listed_at DESC, source_item.id DESC
        LIMIT ? OFFSET ?
        """,
        (page_size, (page - 1) * page_size),
    ).fetchall()
    return RankHistoryPage(tuple(rows), int(total), page, page_size)


def _product_metrics(connection, product, score_date):
    source_rows = connection.execute(
        """
        SELECT source_item.id, source_item.source
        FROM product_source
        JOIN source_item ON source_item.id = product_source.source_item_id
        WHERE product_source.product_id = ?
        """,
        (product["id"],),
    ).fetchall()
    source_ids = {row["source"]: row["id"] for row in source_rows}
    github = _github_metrics(connection, source_ids.get("github"), score_date)
    product_hunt = _product_hunt_metrics(connection, source_ids.get("producthunt"), score_date)
    return {
        "product": product,
        "github": github,
        "producthunt": product_hunt,
    }


def _github_metrics(connection, source_item_id, score_date):
    if source_item_id is None:
        return None
    snapshots = _window_snapshots(connection, source_item_id, score_date)
    if not snapshots:
        return None
    first, last = snapshots[0], snapshots[-1]
    if len(snapshots) == 1 and last["stars_delta_prefill"] is not None:
        return {
            "source_item_id": source_item_id,
            "stars_delta": last["stars_delta_prefill"] or 0,
            "forks_delta": last["forks_delta_prefill"] or 0,
            "window_days": last["prefill_window_days"] or 7,
            "used_prefill": True,
        }
    return {
        "source_item_id": source_item_id,
        "stars_delta": _difference(last["stars"], first["stars"]),
        "forks_delta": _difference(last["forks"], first["forks"]),
        "window_days": _window_days(first["snapshot_date"], last["snapshot_date"]),
        "used_prefill": False,
    }


def _product_hunt_metrics(connection, source_item_id, score_date):
    if source_item_id is None:
        return None
    snapshot = connection.execute(
        """
        SELECT votes_count, daily_rank, snapshot_date
        FROM metric_snapshot
        WHERE source_item_id = ? AND snapshot_date <= ?
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (source_item_id, score_date.isoformat()),
    ).fetchone()
    if snapshot is None:
        return None
    rank = snapshot["daily_rank"]
    return {
        "source_item_id": source_item_id,
        "votes_count": snapshot["votes_count"] or 0,
        "daily_rank": rank,
        "rank_inverse": 1 / rank if rank and rank > 0 else 0.0,
        "window_days": 0,
    }


def _window_snapshots(connection, source_item_id, score_date):
    return connection.execute(
        """
        SELECT stars, forks, snapshot_date, stars_delta_prefill,
               forks_delta_prefill, prefill_window_days
        FROM metric_snapshot
        WHERE source_item_id = ?
          AND snapshot_date <= ?
          AND snapshot_date >= ?
        ORDER BY snapshot_date ASC
        """,
        (
            source_item_id,
            score_date.isoformat(),
            (score_date - timedelta(days=7)).isoformat(),
        ),
    ).fetchall()


def _apply_normalisation(metrics):
    _normalise_component(metrics, "github", "stars_delta")
    _normalise_component(metrics, "github", "forks_delta")
    product_hunt_metrics = [metric for metric in metrics if metric["producthunt"] is not None]
    _normalise_component(product_hunt_metrics, "producthunt", "votes_count")
    _normalise_component(product_hunt_metrics, "producthunt", "rank_inverse")


def _normalise_component(metrics, source, field):
    values = [metric[source][field] if metric[source] is not None else 0 for metric in metrics]
    minimum, maximum = min(values, default=0), max(values, default=0)
    for metric, value in zip(metrics, values):
        component = metric[source]
        if component is not None:
            if maximum == minimum:
                component[f"norm_{field}"] = 100.0 if maximum > 0 else 0.0
            else:
                component[f"norm_{field}"] = 100 * (value - minimum) / (maximum - minimum)


def _score_product(metric, *, config, score_date):
    github = metric["github"]
    product_hunt = metric["producthunt"]
    github_score = _github_score(github, config)
    product_hunt_score = _product_hunt_score(product_hunt, config)
    if product_hunt is not None:
        score_before_decay = (
            config["source_weights"]["github"] * github_score
            + config["source_weights"]["producthunt"] * product_hunt_score
        )
        sources = ["github", "producthunt"]
        mode = "dual_source"
    else:
        score_before_decay = github_score
        sources = ["github"]
        mode = "github_only"
    age_days = _age_days(metric["product"]["first_seen_at"], score_date)
    decay = (
        math.exp(-age_days / config["freshness_decay_days"])
        if config["enable_freshness_decay"]
        else 1.0
    )
    final_score = score_before_decay * decay
    return (
        {
            "as_of_date": score_date.isoformat(),
            "mode": mode,
            "scoring_sources": sources,
            "github": _github_breakdown(github, config),
            "producthunt": _product_hunt_breakdown(product_hunt, config),
            "source_weights": config["source_weights"] if product_hunt is not None else {"github": 1.0},
            "freshness": {
                "enabled": config["enable_freshness_decay"],
                "age_days": age_days,
                "decay_days": config["freshness_decay_days"],
                "multiplier": decay,
            },
            "score_before_decay": score_before_decay,
            "heat_score": final_score,
        },
        final_score,
    )


def _github_score(component, config):
    if component is None:
        return 0.0
    return (
        config["github"]["stars_delta_weight"] * component["norm_stars_delta"]
        + config["github"]["forks_delta_weight"] * component["norm_forks_delta"]
    )


def _product_hunt_score(component, config):
    if component is None:
        return 0.0
    return (
        config["producthunt"]["votes_weight"] * component["norm_votes_count"]
        + config["producthunt"]["rank_inverse_weight"] * component["norm_rank_inverse"]
    )


def _github_breakdown(component, config):
    if component is None:
        return None
    return {
        "source_item_id": component["source_item_id"],
        "raw": {"stars_delta": component["stars_delta"], "forks_delta": component["forks_delta"]},
        "normalised": {
            "stars_delta": component["norm_stars_delta"],
            "forks_delta": component["norm_forks_delta"],
        },
        "weights": config["github"],
        "window_days": component["window_days"],
        "used_prefill": component["used_prefill"],
        "score": _github_score(component, config),
    }


def _product_hunt_breakdown(component, config):
    if component is None:
        return None
    return {
        "source_item_id": component["source_item_id"],
        "raw": {
            "votes_count": component["votes_count"],
            "daily_rank": component["daily_rank"],
            "rank_inverse": component["rank_inverse"],
        },
        "normalised": {
            "votes_count": component.get("norm_votes_count", 0.0),
            "rank_inverse": component.get("norm_rank_inverse", 0.0),
        },
        "weights": config["producthunt"],
        "window_days": component["window_days"],
        "score": _product_hunt_score(component, config),
    }


def _latest_github_stars(connection, source_item_id: int) -> int:
    row = connection.execute(
        """
        SELECT stars FROM metric_snapshot
        WHERE source_item_id = ?
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (source_item_id,),
    ).fetchone()
    return int(row["stars"] or 0) if row is not None else 0


def _history_source_item_id(connection, product_id: int) -> int | None:
    row = connection.execute(
        """
        SELECT product_source.source_item_id
        FROM product_source
        JOIN source_item ON source_item.id = product_source.source_item_id
        WHERE product_source.product_id = ?
        ORDER BY
          CASE
            WHEN source_item.source = 'github' THEN 0
            WHEN source_item.source LIKE 'github%' THEN 1
            ELSE 2
          END,
          product_source.is_primary DESC,
          product_source.source_item_id ASC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()
    return int(row["source_item_id"]) if row is not None else None


def _source_created_at(row) -> datetime | None:
    structured = _parse_datetime(row["github_created_at"])
    if structured is not None:
        return structured
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    return _parse_datetime(raw.get("created_at") if isinstance(raw, dict) else None)


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _difference(last, first):
    return (last or 0) - (first or 0)


def _window_days(first, last):
    return (date.fromisoformat(last) - date.fromisoformat(first)).days


def _age_days(first_seen_at, score_date):
    timestamp = _parse_datetime(first_seen_at)
    return max(0, (score_date - timestamp.date()).days) if timestamp is not None else 0


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
