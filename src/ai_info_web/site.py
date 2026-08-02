"""Static site export and local atomic publication helpers."""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ai_info_web.heat import rank_hot_products, rank_new_products


class PublicationError(RuntimeError):
    """A static build could not be verified or switched into place."""


def stable_slug(product, sources) -> str:
    """Return a stable detail identifier that never relies on rebuilt product.id."""
    identity = "|".join(
        [product["first_seen_at"] or ""]
        + sorted(f"{source['source']}:{source['external_id']}" for source in sources)
    )
    return f"product-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"


def build_static_site(connection, output_directory: Path, *, generated_at: datetime | None = None) -> dict[str, int]:
    """Write a complete static batch to an isolated output directory."""
    timestamp = generated_at or datetime.now(timezone.utc)
    output_directory.mkdir(parents=True, exist_ok=True)
    products = _export_products(connection, timestamp)
    statuses = _latest_statuses(connection)
    payload = {
        "generated_at": timestamp.isoformat(),
        "sources": statuses,
        "products": products,
    }
    _write_json(output_directory / "data" / "products.json", payload)
    _write_json(output_directory / "data" / "status.json", {"generated_at": timestamp.isoformat(), "sources": statuses})
    (output_directory / "assets").mkdir(exist_ok=True)
    (output_directory / "assets" / "site.css").write_text(_SITE_CSS, encoding="utf-8")
    (output_directory / "assets" / "site.js").write_text(_SITE_JS, encoding="utf-8")
    (output_directory / "index.html").write_text(_index_page(products, statuses, timestamp), encoding="utf-8")
    for product in products:
        detail_directory = output_directory / "products" / product["slug"]
        detail_directory.mkdir(parents=True, exist_ok=True)
        (detail_directory / "index.html").write_text(_detail_page(product, statuses, timestamp), encoding="utf-8")
    return {"products": len(products), "details": len(products)}


def publish_site(target_directory: Path, builder) -> dict[str, int]:
    """Build, verify, then replace a local publication directory with rollback on error."""
    target_directory.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(tempfile.mkdtemp(prefix=f".{target_directory.name}-staging-", dir=target_directory.parent))
    try:
        result = builder(staging_directory)
        verify_static_site(staging_directory)
        _replace_directory(staging_directory, target_directory)
        return result
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise


def verify_static_site(output_directory: Path, *, forbidden_values: tuple[str, ...] = ()) -> None:
    """Reject incomplete artifacts and any known runtime secret copied into output."""
    required = (output_directory / "index.html", output_directory / "data" / "products.json", output_directory / "data" / "status.json")
    if any(not path.is_file() for path in required):
        raise PublicationError("Static build is missing a required index or data artifact.")
    products = json.loads((output_directory / "data" / "products.json").read_text(encoding="utf-8"))["products"]
    for product in products:
        if not (output_directory / "products" / product["slug"] / "index.html").is_file():
            raise PublicationError(f"Static build is missing detail page for {product['slug']}.")
    forbidden = tuple(value for value in forbidden_values if value)
    for path in output_directory.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if any(value in content for value in forbidden):
            raise PublicationError("Static build contained a configured runtime secret.")
        if "DEEPSEEK_API_KEY=" in content or "GITHUB_TOKEN=" in content:
            raise PublicationError("Static build contained a credential assignment.")


def _replace_directory(staging_directory: Path, target_directory: Path) -> None:
    backup_directory = target_directory.with_name(f".{target_directory.name}-previous")
    if backup_directory.exists():
        shutil.rmtree(backup_directory)
    if target_directory.exists():
        os.replace(target_directory, backup_directory)
    try:
        os.replace(staging_directory, target_directory)
    except Exception:
        if backup_directory.exists():
            os.replace(backup_directory, target_directory)
        raise
    shutil.rmtree(backup_directory, ignore_errors=True)


def _export_products(connection, now: datetime) -> list[dict[str, object]]:
    source_rows = connection.execute(
        """
        SELECT product_source.product_id, product_source.is_primary, source_item.*
        FROM product_source JOIN source_item ON source_item.id = product_source.source_item_id
        ORDER BY product_source.product_id, product_source.is_primary DESC, source_item.id
        """
    ).fetchall()
    sources_by_product: dict[int, list] = {}
    for row in source_rows:
        sources_by_product.setdefault(row["product_id"], []).append(row)
    hot_ids = {row["id"] for row in rank_hot_products(connection)}
    new_ids = {row["id"] for row in rank_new_products(connection, now=now)}
    products = []
    for product in connection.execute("SELECT * FROM product ORDER BY heat_score DESC, name COLLATE NOCASE"):
        sources = sources_by_product.get(product["id"], [])
        primary = next((source for source in sources if source["is_primary"]), sources[0] if sources else None)
        links = [
            {"source": source["source"], "label": source["name"], "url": _safe_url(source["url"]), "homepage": _safe_url(source["homepage"])}
            for source in sources
        ]
        products.append(
            {
                "slug": stable_slug(product, sources),
                "name": product["name"],
                "category": product["category"] or "other",
                "summary": product["summary_zh"] or (primary["description"] if primary else "暂无可展示的产品描述。"),
                "summary_status": product["summary_status"],
                "heat_score": round(product["heat_score"] or 0.0, 1),
                "score_breakdown": _json_object(product["score_breakdown"]),
                "first_seen_at": product["first_seen_at"],
                "last_updated_at": product["last_updated_at"],
                "sources": links,
                "is_hot": product["id"] in hot_ids,
                "is_new": product["id"] in new_ids,
            }
        )
    return products


def _latest_statuses(connection) -> dict[str, str]:
    statuses: dict[str, str] = {"github": "unknown", "producthunt": "unknown", "summary": "unknown", "pipeline": "unknown"}
    for row in connection.execute("SELECT provider_status FROM run_log ORDER BY id DESC"):
        for provider, status in _json_object(row["provider_status"]).items():
            if provider in statuses and statuses[provider] == "unknown" and isinstance(status, str):
                statuses[provider] = status
    return statuses


def _index_page(products, statuses, timestamp: datetime) -> str:
    cards = "".join(_card(product) for product in products)
    categories = sorted({product["category"] for product in products})
    category_buttons = "".join(
        f'<button class="filter" data-category="{html.escape(category)}">{html.escape(category)}</button>' for category in categories
    )
    return _page_shell(
        title="AI Product Radar",
        body=f"""
<header class="topbar"><a class="brand" href="index.html">AI Product Radar</a><span class="eyebrow">每日 AI 产品情报</span></header>
<main>
  <section class="overview"><p class="eyebrow">AI Product Intelligence</p><h1>今日值得关注的 AI 产品</h1><p class="muted">按最新发现与热度变化整理，所有信息均可回溯至原始来源。</p><div class="status">{_status_markup(statuses, timestamp)}</div></section>
  <section class="toolbar"><div class="tabs"><button class="tab active" data-tab="all">全部</button><button class="tab" data-tab="new">今日新品</button><button class="tab" data-tab="hot">热门榜</button></div><div class="filters"><button class="filter active" data-category="all">全部分类</button>{category_buttons}</div></section>
  <section id="product-list" class="product-list">{cards or '<p class="empty">当前批次没有可发布的产品。</p>'}</section>
</main>""",
        prefix="",
    )


def _detail_page(product, statuses, timestamp: datetime) -> str:
    links = "".join(
        f'<a class="source-link" href="{html.escape(link["homepage"] or link["url"] or "#", quote=True)}" rel="noopener noreferrer" target="_blank">{html.escape(link["source"])} 来源</a>'
        for link in product["sources"]
        if link["homepage"] or link["url"]
    )
    window_days = product["score_breakdown"].get("github", {}).get("window_days") if product["score_breakdown"].get("github") else None
    window = f"近 {window_days} 天数据窗口" if window_days is not None else "暂无热度窗口数据"
    return _page_shell(
        title=str(product["name"]),
        prefix="../../",
        body=f"""
<header class="topbar"><a class="brand" href="../../index.html">AI Product Radar</a><a class="back" href="../../index.html">返回列表</a></header>
<main><article class="detail">
<p class="eyebrow">{html.escape(str(product["category"]))}</p><h1>{html.escape(str(product["name"]))}</h1>
<p class="summary">{html.escape(str(product["summary"]))}</p>
<dl class="metrics"><div><dt>热度</dt><dd>{product["heat_score"]}</dd></div><div><dt>数据窗口</dt><dd>{html.escape(window)}</dd></div><div><dt>首次发现</dt><dd>{html.escape(str(product["first_seen_at"] or "未知"))}</dd></div></dl>
<div class="source-links">{links or '<span class="muted">暂无可用外链</span>'}</div>
<div class="status">{_status_markup(statuses, timestamp)}</div>
</article></main>""",
    )


def _page_shell(*, title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="AI 产品情报网站"><title>{html.escape(title)} | AI Product Radar</title><link rel="stylesheet" href="{prefix}assets/site.css"></head><body>{body}<script src="{prefix}assets/site.js"></script></body></html>"""


def _card(product) -> str:
    flags = " ".join(flag for flag, enabled in (("new", product["is_new"]), ("hot", product["is_hot"])) if enabled)
    return f"""<article class="product-card" data-category="{html.escape(str(product['category']))}" data-tabs="{flags}"><a href="products/{html.escape(str(product['slug']))}/"><p class="eyebrow">{html.escape(str(product['category']))}</p><h2>{html.escape(str(product['name']))}</h2><p>{html.escape(str(product['summary']))}</p><footer><span>热度 {product['heat_score']}</span><span>{html.escape(str(product['first_seen_at'] or ''))[:10]}</span></footer></a></article>"""


def _status_markup(statuses, timestamp: datetime) -> str:
    labels = {"github": "GitHub", "producthunt": "Product Hunt", "summary": "摘要"}
    chips = "".join(f'<span class="chip {html.escape(statuses.get(key, "unknown"))}">{label}: {html.escape(statuses.get(key, "unknown"))}</span>' for key, label in labels.items())
    return f'<span>数据更新于 {html.escape(timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</span>{chips}'


def _safe_url(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _json_object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


_SITE_CSS = """*{box-sizing:border-box}body{margin:0;background:#101418;color:#eef4f7;font:16px/1.6 -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}.topbar,main{max-width:1120px;margin:auto;padding-left:24px;padding-right:24px}.topbar{height:64px;display:flex;align-items:center;gap:16px;border-bottom:1px solid #28333b}.brand{color:#fff;font-weight:700;text-decoration:none}.back,.muted{color:#aebbc4}.overview{padding:64px 0 32px}.overview h1,.detail h1{font-size:36px;line-height:1.18;margin:6px 0 12px}.eyebrow{margin:0;color:#63d7bd;font-size:13px;font-weight:700;letter-spacing:0}.status{display:flex;flex-wrap:wrap;align-items:center;gap:8px;color:#aebbc4;font-size:13px;margin-top:18px}.chip{padding:2px 8px;border:1px solid #45545d;border-radius:4px}.chip.ok{border-color:#2f957d;color:#80e8cf}.chip.degraded{border-color:#a78542;color:#eed28e}.chip.failed{border-color:#b45c61;color:#ffb5b9}.toolbar{border-top:1px solid #28333b;border-bottom:1px solid #28333b;padding:16px 0;display:flex;gap:16px;justify-content:space-between;flex-wrap:wrap}.tabs,.filters{display:flex;gap:8px;flex-wrap:wrap}.tab,.filter{background:transparent;border:1px solid #45545d;border-radius:4px;color:#d6e1e7;padding:7px 11px;cursor:pointer}.tab.active,.filter.active{background:#1f5d58;border-color:#63d7bd;color:#fff}.product-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;padding:24px 0 56px}.product-card{border:1px solid #2b3942;border-radius:6px;background:#151d23;min-height:220px}.product-card a{display:block;color:inherit;text-decoration:none;padding:18px;height:100%}.product-card:hover{border-color:#63d7bd}.product-card h2{font-size:20px;line-height:1.28;margin:6px 0}.product-card footer{display:flex;justify-content:space-between;color:#aebbc4;font-size:13px;margin-top:18px}.detail{max-width:760px;padding:64px 0}.summary{font-size:19px;color:#d6e1e7}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:32px 0}.metrics div{border:1px solid #2b3942;padding:12px}.metrics dt{font-size:13px;color:#aebbc4}.metrics dd{margin:4px 0 0}.source-links{display:flex;gap:10px;flex-wrap:wrap}.source-link{color:#80e8cf}@media(max-width:620px){.overview h1,.detail h1{font-size:30px}.metrics{grid-template-columns:1fr}.topbar,main{padding-left:16px;padding-right:16px}}"""

_SITE_JS = """(() => {const tabs=[...document.querySelectorAll('.tab')], filters=[...document.querySelectorAll('.filter')], cards=[...document.querySelectorAll('.product-card')];let tab='all',category='all';function render(){cards.forEach(card=>{const tabMatch=tab==='all'||card.dataset.tabs.split(' ').includes(tab);const categoryMatch=category==='all'||card.dataset.category===category;card.hidden=!(tabMatch&&categoryMatch);});}tabs.forEach(button=>button.addEventListener('click',()=>{tab=button.dataset.tab;tabs.forEach(item=>item.classList.toggle('active',item===button));render();}));filters.forEach(button=>button.addEventListener('click',()=>{category=button.dataset.category;filters.forEach(item=>item.classList.toggle('active',item===button));render();}));})();"""
