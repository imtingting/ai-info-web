"""Heat scoring and independent ranking helpers for curated products."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class HeatRunResult:
    products_scored: int
    product_hunt_available: bool


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
    _apply_normalisation(metrics, product_hunt_available)

    with connection:
        for metric in metrics:
            breakdown, score = _score_product(
                metric,
                config=config,
                score_date=score_date,
                product_hunt_available=product_hunt_available,
            )
            connection.execute(
                "UPDATE product SET heat_score = ?, score_breakdown = ? WHERE id = ?",
                (score, json.dumps(breakdown, sort_keys=True), metric["product"]["id"]),
            )
    return HeatRunResult(len(metrics), product_hunt_available)


def rank_hot_products(connection, *, limit: int = 50):
    """Return the hot-tab ranking, capped by the configured UI limit."""
    return connection.execute(
        "SELECT * FROM product ORDER BY heat_score DESC, last_updated_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def rank_new_products(
    connection,
    *,
    now: datetime | None = None,
    window_hours: int = 48,
):
    """Return only products first seen in the new-tab window, newest first."""
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=window_hours)
    ranked = []
    for product in connection.execute("SELECT * FROM product"):
        first_seen = _parse_datetime(product["first_seen_at"])
        if first_seen is None or first_seen < cutoff:
            continue
        ranked.append((product, _latest_source_signal(connection, product["id"])))
    ranked.sort(
        key=lambda item: (_parse_datetime(item[0]["first_seen_at"]), item[1], item[0]["id"]),
        reverse=True,
    )
    return [product for product, _signal in ranked]


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
    return {
        "source_item_id": source_item_id,
        "stars_delta": _difference(last["stars"], first["stars"]),
        "forks_delta": _difference(last["forks"], first["forks"]),
        "window_days": _window_days(first["snapshot_date"], last["snapshot_date"]),
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
        SELECT stars, forks, snapshot_date
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


def _apply_normalisation(metrics, product_hunt_available):
    _normalise_component(metrics, "github", "stars_delta")
    _normalise_component(metrics, "github", "forks_delta")
    if product_hunt_available:
        _normalise_component(metrics, "producthunt", "votes_count")
        _normalise_component(metrics, "producthunt", "rank_inverse")


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


def _score_product(metric, *, config, score_date, product_hunt_available):
    github = metric["github"]
    product_hunt = metric["producthunt"]
    github_score = _github_score(github, config)
    product_hunt_score = _product_hunt_score(product_hunt, config) if product_hunt_available else 0.0
    if product_hunt_available:
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
            "source_weights": config["source_weights"] if product_hunt_available else {"github": 1.0},
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


def _latest_source_signal(connection, product_id):
    row = connection.execute(
        """
        SELECT COALESCE(SUM(COALESCE(snapshot.stars, 0) + COALESCE(snapshot.votes_count, 0)), 0) AS signal
        FROM product_source
        JOIN (
          SELECT source_item_id, MAX(snapshot_date) AS latest_date
          FROM metric_snapshot GROUP BY source_item_id
        ) latest ON latest.source_item_id = product_source.source_item_id
        JOIN metric_snapshot snapshot
          ON snapshot.source_item_id = latest.source_item_id
         AND snapshot.snapshot_date = latest.latest_date
        WHERE product_source.product_id = ?
        """,
        (product_id,),
    ).fetchone()
    return row["signal"]


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
