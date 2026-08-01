"""Cleaning, conservative cross-source matching, and rule-based classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


BLACKLIST = ("template", "tutorial", "book", "awesome-list", "dotfiles", "freebie", "mockup")


@dataclass(frozen=True)
class CurationResult:
    products_created: int
    source_items_accepted: int
    weak_matches: int


def curate(connection, *, rules_path: Path, review_queue_path: Path, now: datetime | None = None) -> CurationResult:
    """Rebuild products from accepted sources; weak matches are never auto-merged."""
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    candidates = [
        _candidate(connection, row, rules, now or datetime.now(timezone.utc))
        for row in connection.execute("SELECT * FROM source_item")
    ]
    accepted = [candidate for candidate in candidates if candidate is not None]
    groups, weak_matches = _group(accepted)
    with connection:
        connection.execute("DELETE FROM product_source")
        connection.execute("DELETE FROM product")
        for group in groups:
            primary = _primary_item(group)
            cursor = connection.execute(
                "INSERT INTO product(name, category, first_seen_at, last_updated_at) VALUES (?, ?, ?, ?)",
                (primary["name"], primary["category"], primary["first_seen_at"], primary["last_seen_at"]),
            )
            product_id = cursor.lastrowid
            for item in group:
                connection.execute(
                    "INSERT INTO product_source(product_id, source_item_id, source, is_primary) VALUES (?, ?, ?, ?)",
                    (product_id, item["id"], item["source"], int(item is primary)),
                )
    review_queue_path.parent.mkdir(parents=True, exist_ok=True)
    review_queue_path.write_text(json.dumps(weak_matches, ensure_ascii=False, indent=2), encoding="utf-8")
    return CurationResult(len(groups), len(accepted), len(weak_matches))


def _candidate(connection, row, rules, now):
    raw = _json_object(row["raw_json"])
    text = f"{row['name']} {row['description'] or ''}".lower()
    metrics = _latest_snapshot(connection, row["id"])
    if (
        _is_empty(row, raw)
        or any(term in text for term in BLACKLIST)
        or not _meets_threshold(row, raw, metrics, now)
    ):
        return None
    topics = [topic.lower() for topic in _json_list(row["topics"])]
    return {
        **dict(row),
        "raw": raw,
        "topics_list": topics,
        "category": _category(topics, text, rules),
        "url_keys": _url_keys(row["url"], row["homepage"]),
        "domain": _domain(row["homepage"]),
        "name_key": _name_key(row["name"]),
    }


def _is_empty(row, raw):
    if row["source"] == "github":
        return not (row["description"] or row["homepage"])
    return not (row["description"] or raw.get("tagline"))


def _meets_threshold(row, raw, metrics, now):
    if row["source"] == "github":
        stars = _metric_value(metrics, raw, "stars") or 0
        created = _parse_time(raw.get("created_at"))
        return stars >= 50 or (created is not None and created >= now - timedelta(days=7) and stars >= 20)
    rank = _metric_value(metrics, raw, "daily_rank")
    votes = _metric_value(metrics, raw, "votes_count") or 0
    return votes >= 30 or (rank is not None and rank <= 50)


def _metric_value(metrics, raw, field):
    if metrics is not None and metrics[field] is not None:
        return int(metrics[field])
    aliases = {"stars": "stargazers_count", "votes_count": "votesCount", "daily_rank": "dailyRank"}
    value = raw.get(field, raw.get(aliases.get(field, field)))
    return int(value) if isinstance(value, int) else None


def _latest_snapshot(connection, source_item_id):
    return connection.execute(
        "SELECT stars, votes_count, daily_rank FROM metric_snapshot "
        "WHERE source_item_id = ? ORDER BY snapshot_date DESC LIMIT 1",
        (source_item_id,),
    ).fetchone()


def _group(items):
    groups, weak = [], []
    remaining = list(items)
    while remaining:
        item = remaining.pop(0)
        match = None
        for other in remaining:
            if item["source"] == other["source"]:
                continue
            strong = bool(item["url_keys"] & other["url_keys"])
            domain = item["domain"] and item["domain"] == other["domain"]
            if strong or domain:
                match = other; break
            if item["name_key"] == other["name_key"]:
                weak.append(
                    {
                        "left_source_item_id": item["id"],
                        "right_source_item_id": other["id"],
                        "name": item["name"],
                        "reason": "normalized_name_match",
                    }
                )
        if match:
            remaining.remove(match)
            groups.append([item, match])
        else:
            groups.append([item])
    return groups, weak


def _primary_item(group):
    github = next((item for item in group if item["source"] == "github"), None)
    if github is None:
        return group[0]
    alternative = next((item for item in group if item is not github), None)
    if alternative is not None and _completeness(alternative) > _completeness(github):
        return alternative
    return github


def _completeness(item):
    return sum(
        (
            bool(item["description"]),
            bool(item["homepage"]),
            bool(item["topics_list"]),
        )
    )


def _category(topics, text, rules):
    for topic in topics:
        if topic in rules["topic_map"]: return rules["topic_map"][topic]
    for keyword, category in rules["keyword_map"].items():
        if keyword in text: return category
    return "other"


def _url_keys(*values):
    return {value.rstrip("/").lower() for value in values if value}


def _domain(value):
    host = urlparse(value).hostname if value else None
    return host.lower().removeprefix("www.") if host else ""


def _name_key(value):
    return "".join(token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in {"ai", "app", "official"})


def _parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
