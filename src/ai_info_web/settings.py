"""Runtime configuration with environment-variable overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.json"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    enable_product_hunt: bool
    product_hunt_pages_per_run: int
    enable_summary: bool
    summary_monthly_budget_cny: float
    github_pages_per_query: int
    github_queries: tuple[str, ...]
    github_recent_created_days: int = 7
    github_max_enrichment_items: int = 20
    github_max_stargazer_prefill_items: int = 30


def load_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Load non-secret JSON configuration and apply explicit env overrides."""
    config = _read_config(config_path)
    database_path = _resolve_path(
        _env_or_config("AI_INFO_WEB_DB_PATH", config, "database_path"), config_path
    )
    return Settings(
        database_path=database_path,
        enable_product_hunt=_as_bool(
            _env_or_config("ENABLE_PRODUCT_HUNT", config, "enable_product_hunt")
        ),
        product_hunt_pages_per_run=int(config["product_hunt_pages_per_run"]),
        enable_summary=_as_bool(_env_or_config("ENABLE_SUMMARY", config, "enable_summary")),
        summary_monthly_budget_cny=float(
            _env_or_config(
                "SUMMARY_MONTHLY_BUDGET_CNY", config, "summary_monthly_budget_cny"
            )
        ),
        github_pages_per_query=int(config["github_pages_per_query"]),
        github_queries=tuple(config["github_queries"]),
        github_recent_created_days=int(config.get("github_recent_created_days", 7)),
        github_max_enrichment_items=int(config.get("github_max_enrichment_items", 20)),
        github_max_stargazer_prefill_items=int(config.get("github_max_stargazer_prefill_items", 30)),
    )


def _read_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as config_file:
        return json.load(config_file)


def _env_or_config(env_name: str, config: dict[str, Any], config_name: str) -> Any:
    return os.environ.get(env_name, config[config_name])


def _resolve_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, received {value!r}")
