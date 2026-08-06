"""Static site export and local atomic publication helpers."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
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
    chat_endpoint = _safe_https_url(os.environ.get("CHAT_API_URL"))
    payload = {
        "generated_at": timestamp.isoformat(),
        "sources": statuses,
        "products": products,
    }
    _write_json(output_directory / "data" / "products.json", payload)
    _write_json(output_directory / "data" / "status.json", {"generated_at": timestamp.isoformat(), "sources": statuses})
    (output_directory / "assets").mkdir(exist_ok=True)
    (output_directory / "assets" / "site.css").write_text(
        _SITE_CSS + _DETAIL_CSS + _POLISH_CSS, encoding="utf-8"
    )
    (output_directory / "assets" / "site.js").write_text(_SITE_JS, encoding="utf-8")
    (output_directory / "index.html").write_text(_index_page(products, statuses, timestamp), encoding="utf-8")
    for product in products:
        detail_directory = output_directory / "products" / product["slug"]
        detail_directory.mkdir(parents=True, exist_ok=True)
        (detail_directory / "index.html").write_text(
            _detail_page(product, statuses, timestamp, chat_endpoint=chat_endpoint), encoding="utf-8"
        )
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
    latest_metrics = _latest_metrics(connection)
    hot_ids = {row["id"] for row in rank_hot_products(connection)}
    new_ids = {row["id"] for row in rank_new_products(connection, now=now)}
    history_ids = _history_product_ids(connection)
    trending_source_ids, trending_observed_at = _trending_source_ids(connection)
    products = []
    for product in connection.execute("SELECT * FROM product ORDER BY heat_score DESC, name COLLATE NOCASE"):
        sources = sources_by_product.get(product["id"], [])
        primary = next((source for source in sources if source["is_primary"]), sources[0] if sources else None)
        github_source = _github_source(sources)
        signal = _core_signal(sources, latest_metrics)
        summary = product["summary_zh"] or (
            primary["description"] if primary and primary["description"] else "暂无可展示的产品描述。"
        )
        links = [
            {
                "source": source["source"],
                "label": source["name"],
                "url": _safe_url(source["url"]),
                "homepage": _safe_url(source["homepage"]),
                "readme_images": _safe_image_urls(_json_list(source["readme_images"])),
                "og_image": _safe_image_url(source["og_image"]),
            }
            for source in sources
        ]
        images = _detail_images(links)
        products.append(
            {
                "slug": stable_slug(product, sources),
                "name": product["name"],
                "category": product["category"] or "other",
                "summary": summary,
                "card_summary": _card_summary(summary),
                "summary_status": product["summary_status"],
                "heat_score": round(product["heat_score"] or 0.0, 1),
                "score_breakdown": _json_object(product["score_breakdown"]),
                "github_created_at": _source_created_at(github_source),
                "last_updated_at": product["last_updated_at"],
                "sources": links,
                "analysis_basis": "README" if any(source["readme_text"] for source in sources) else "简介",
                "images": images,
                "heat_evidence": _heat_evidence(_json_object(product["score_breakdown"])),
                "chat_context": _chat_context(sources, links),
                "is_hot": product["id"] in hot_ids,
                "is_new": product["id"] in new_ids,
                "is_historical": product["id"] in history_ids,
                "is_trending_observation": any(source["id"] in trending_source_ids for source in sources),
                "trending_observed_at": trending_observed_at,
                "signal": signal,
            }
        )
    return products


def _latest_statuses(connection) -> dict[str, str]:
    statuses: dict[str, str] = {
        "github": "unknown",
        "github_trending_observation": "unknown",
        "producthunt": "unknown",
        "summary": "unknown",
        "pipeline": "unknown",
    }
    for row in connection.execute("SELECT provider_status FROM run_log ORDER BY id DESC"):
        for provider, status in _json_object(row["provider_status"]).items():
            if provider in statuses and statuses[provider] == "unknown" and isinstance(status, str):
                statuses[provider] = status
    return statuses


def _index_page(products, statuses, timestamp: datetime) -> str:
    weekly_new = [product for product in products if product["is_new"]]
    hot = [product for product in products if product["is_hot"]]
    trending = [product for product in products if product["is_trending_observation"]]
    historical = [product for product in products if product["is_historical"]]
    categories = sorted(
        {
            product["category"]
            for product in weekly_new + hot + trending + historical
        }
    )
    category_buttons = "".join(
        f'<button class="filter" data-category="{html.escape(category)}">{html.escape(_category_label(category))}</button>' for category in categories
    )
    all_cards = "".join(_card(product, all_index=index) for index, product in enumerate(historical))
    trending_cards = "".join(_card(product, observation=True) for product in trending)
    trending_observation = (
        f"GitHub Trending 周观察，抓取于 {_display_date(trending[0]['trending_observed_at'])} UTC · 非 API 数据源"
        if trending
        else "本周观察暂不可用"
    )
    trending_markup = trending_cards or f'<p class="empty">{html.escape(trending_observation)}</p>'
    return _page_shell(
        title="AI Product Radar",
        body=f"""
<header class="topbar"><a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true">AR</span><span>AI 产品雷达</span></a><span class="eyebrow">AI PRODUCT RADAR</span></header>
<main>
  <section class="overview"><p class="eyebrow">每周更新的 AI 项目情报</p><h1>发现近期上线与增长最快的 AI 产品</h1><p class="muted">每周发现近期值得关注的 AI 开源项目，帮你快速发现新工具、新框架和增长最快的项目。</p><div class="status">{_status_markup(statuses, timestamp)}</div></section>
  <section class="toolbar" aria-label="项目浏览控制"><div class="tabs" role="tablist"><button class="tab active" role="tab" aria-selected="true" data-tab="weekly">本周新品<span>{len(weekly_new)}</span></button><button class="tab" role="tab" aria-selected="false" data-tab="hot">热门榜<span>{len(hot)}</span></button><button class="tab" role="tab" aria-selected="false" data-tab="all">全部<span>{len(historical)}</span></button></div><div class="filters" aria-label="分类筛选"><button class="filter active" data-category="all">全部分类</button>{category_buttons}</div></section>
  <section class="tab-panel active" data-panel="weekly"><div class="section-heading"><div><h2>本周新品</h2><p>近 7 天创建并通过收录门槛，按 stars 排序。</p></div></div><div class="product-list">{''.join(_card(product) for product in weekly_new) or '<p class="empty">近 7 天暂无达到收录门槛的项目。</p>'}</div><p class="empty filter-empty" hidden>当前分类没有本周新品。</p></section>
  <section class="tab-panel" data-panel="hot" hidden><div class="section-heading"><div><h2>近 7 日 GitHub 热度榜</h2><p>按 star/fork 增量、数据窗口和新鲜度计算；启动期数据会标注实际窗口。</p></div></div><div class="product-list">{''.join(_card(product) for product in hot) or '<p class="empty">热度数据仍在积累中，暂未形成可比较的榜单。</p>'}</div><p class="empty filter-empty" hidden>当前分类没有热门项目。</p><div class="observation-heading"><div><h2>GitHub Trending 周观察</h2><p>{html.escape(trending_observation)}</p></div><a href="https://github.com/trending?since=weekly" target="_blank" rel="noopener noreferrer">查看来源</a></div><div class="product-list observation-list">{trending_markup}</div><p class="empty filter-empty" hidden>当前分类没有周观察项目。</p></section>
  <section class="tab-panel" data-panel="all" hidden><div class="section-heading"><div><h2>全部入选项目</h2><p>历史本周新品与热门榜的去重并集，每页 30 项。</p></div></div><div class="product-list" data-history-list>{all_cards or '<p class="empty">历史榜单尚未积累项目。</p>'}</div><p class="empty filter-empty" hidden>当前分类没有历史入选项目。</p><nav class="pagination" aria-label="历史项目分页" hidden><button class="page-control" type="button" data-page-action="previous" aria-label="上一页">上一页</button><span data-page-status></span><button class="page-control" type="button" data-page-action="next" aria-label="下一页">下一页</button></nav></section>
</main>""",
        prefix="",
    )


def _detail_page(product, statuses, timestamp: datetime, *, chat_endpoint: str | None) -> str:
    github_link = next((link["url"] for link in product["sources"] if link["source"].startswith("github") and link["url"]), None)
    homepage_link = next((link["homepage"] for link in product["sources"] if link["homepage"]), None)
    links = "".join(
        link
        for link in (
            _source_link(github_link, "GitHub 仓库"),
            _source_link(homepage_link, "项目官网"),
        )
        if link
    )
    image_markup = "".join(
        f'<figure class="project-image"><img src="{html.escape(image["url"], quote=True)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest(\'figure\').hidden=true"><figcaption>图：{html.escape(image["source"])}</figcaption></figure>'
        for image in product["images"]
    )
    heat_evidence = "".join(f"<li>{html.escape(item)}</li>" for item in product["heat_evidence"])
    chat_disabled = "" if chat_endpoint else " disabled"
    quick_questions = (
        "它适合什么场景？",
        "和同类项目比有什么特点？",
        "这个项目最近有什么更新？",
    )
    quick_question_buttons = "".join(
        f'<button type="button" data-chat-prompt="{html.escape(question, quote=True)}"{chat_disabled}>{html.escape(question)}</button>'
        for question in quick_questions
    )
    chat_markup = f"""
<section class="detail-section chat" data-chat data-endpoint="{html.escape(chat_endpoint or '', quote=True)}" data-product-slug="{html.escape(str(product['slug']), quote=True)}">
  <div class="detail-heading"><h2>问问这个项目</h2><span data-chat-state>{'服务已连接' if chat_endpoint else '服务配置中'}</span></div>
  <div class="chat-empty" data-chat-empty><p>想快速了解它适合什么场景、怎么开始用，或者最近有什么更新，可以直接提问。</p><div class="chat-prompts">{quick_question_buttons}</div></div>
  <div class="chat-messages" data-chat-messages role="log" aria-live="polite" aria-relevant="additions text" hidden></div>
  <form data-chat-form><textarea name="message" maxlength="1000" rows="3" placeholder="输入关于该项目的问题"{chat_disabled}></textarea><div class="chat-actions"><label class="chat-web-toggle"><input type="checkbox" name="use_web"{chat_disabled}> 联网检索</label><span data-chat-error role="status"></span><button type="submit"{chat_disabled}>发送</button></div></form>
</section>"""
    window_days = product["score_breakdown"].get("github", {}).get("window_days") if product["score_breakdown"].get("github") else None
    window = f"近 {window_days} 天数据窗口" if window_days is not None else "暂无热度窗口数据"
    return _page_shell(
        title=str(product["name"]),
        prefix="../../",
        body=f"""
<header class="topbar"><a class="brand" href="../../index.html">AI Product Radar</a><a class="back" href="../../index.html">返回列表</a></header>
<main><article class="detail">
<p class="eyebrow detail-category {_category_class(str(product["category"]))}">{html.escape(_category_label(str(product["category"])))}</p><h1>{html.escape(str(product["name"]))}</h1>
<section class="detail-section analysis"><div class="detail-heading"><h2>项目分析</h2></div><p class="summary">{html.escape(str(product["summary"]))}</p></section>
{f'<section class="detail-section media"><div class="detail-heading"><h2>项目图片</h2><span>外链展示</span></div><div class="image-gallery">{image_markup}</div></section>' if image_markup else ''}
<dl class="metrics"><div><dt>{html.escape(str(product["signal"]["label"]))}</dt><dd>{html.escape(str(product["signal"]["display"]))}</dd></div><div><dt>数据窗口</dt><dd>{html.escape(window)}</dd></div><div><dt>GitHub 创建</dt><dd>{html.escape(_display_date(product["github_created_at"]))}</dd></div></dl>
<section class="detail-section evidence"><div class="detail-heading"><h2>为什么值得关注</h2></div><ul>{heat_evidence}</ul></section>
<div class="source-links">{links or '<span class="muted">暂无可用 GitHub 或官网链接</span>'}</div>
{chat_markup}
<div class="status">{_status_markup(statuses, timestamp)}</div>
</article></main>""",
    )


def _page_shell(*, title: str, body: str, prefix: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="AI 产品情报网站"><title>{html.escape(title)} | AI Product Radar</title><link rel="stylesheet" href="{prefix}assets/site.css"></head><body>{body}<script src="{prefix}assets/site.js"></script></body></html>"""


def _card(product, *, all_index: int | None = None, observation: bool = False) -> str:
    badges = []
    badge_labels = set()
    for link in product["sources"]:
        label = _source_badge_label(link["source"])
        if label in badge_labels:
            continue
        badge_labels.add(label)
        badges.append(
            f'<span class="source-badge {html.escape(_source_badge_class(link["source"]))}">{html.escape(label)}</span>'
        )
    flags = []
    if product["is_new"]:
        flags.append('<span class="flag new">本周新</span>')
    if product["is_hot"]:
        flags.append('<span class="flag hot">热门</span>')
    if observation:
        flags.append('<span class="flag observation">周观察</span>')
    page = "" if all_index is None else f' data-history-card data-history-index="{all_index}"'
    category = str(product["category"])
    return f"""<article class="product-card" data-category="{html.escape(category)}"{page}><a href="products/{html.escape(str(product['slug']))}/"><header><div class="source-badges">{''.join(badges)}</div><div class="flags">{''.join(flags)}</div></header><span class="category-pill {_category_class(category)}">{html.escape(_category_label(category))}</span><h2>{html.escape(str(product['name']))}</h2><p class="card-summary">{html.escape(str(product['card_summary']))}</p><footer><span class="core-signal">{html.escape(str(product['signal']['label']))} <strong>{html.escape(str(product['signal']['display']))}</strong></span><span>创建 {_display_date(product['github_created_at'])}</span></footer></a></article>"""


def _status_markup(statuses, timestamp: datetime) -> str:
    return f'<span>数据更新于 {html.escape(timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</span>'


def _latest_metrics(connection) -> dict[int, dict[str, int | None]]:
    rows = connection.execute(
        """
        SELECT metric_snapshot.*
        FROM metric_snapshot
        JOIN (
          SELECT source_item_id, MAX(snapshot_date) AS snapshot_date
          FROM metric_snapshot GROUP BY source_item_id
        ) latest
          ON latest.source_item_id = metric_snapshot.source_item_id
         AND latest.snapshot_date = metric_snapshot.snapshot_date
        """
    ).fetchall()
    return {int(row["source_item_id"]): dict(row) for row in rows}


def _history_product_ids(connection) -> set[int]:
    rows = connection.execute(
        """
        SELECT DISTINCT product_source.product_id
        FROM rank_history
        JOIN product_source ON product_source.source_item_id = rank_history.source_item_id
        """
    ).fetchall()
    return {int(row["product_id"]) for row in rows}


def _trending_source_ids(connection) -> tuple[set[int], str | None]:
    row = connection.execute(
        "SELECT MAX(last_seen_at) AS observed_at FROM source_item WHERE source = 'github_trending_observation'"
    ).fetchone()
    observed_at = row["observed_at"] if row else None
    if not observed_at:
        return set(), None
    rows = connection.execute(
        "SELECT id FROM source_item WHERE source = 'github_trending_observation' AND last_seen_at = ?",
        (observed_at,),
    ).fetchall()
    return {int(row["id"]) for row in rows}, str(observed_at)


def _github_source(sources):
    return next((source for source in sources if source["source"] == "github"), next((source for source in sources if source["source"].startswith("github")), None))


def _core_signal(sources, latest_metrics) -> dict[str, str]:
    github = _github_source(sources)
    if github is not None:
        stars = latest_metrics.get(int(github["id"]), {}).get("stars")
        if stars is not None:
            return {"label": "Stars", "display": _compact_number(int(stars))}
    product_hunt = next((source for source in sources if source["source"] == "producthunt"), None)
    if product_hunt is not None:
        votes = latest_metrics.get(int(product_hunt["id"]), {}).get("votes_count")
        if votes is not None:
            return {"label": "Votes", "display": _compact_number(int(votes))}
    return {"label": "信号", "display": "—"}


def _source_created_at(source) -> str | None:
    if source is None:
        return None
    value = source["github_created_at"]
    if isinstance(value, str) and value:
        return value
    raw = _json_object(source["raw_json"])
    created_at = raw.get("created_at")
    return created_at if isinstance(created_at, str) else None


def _card_summary(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:200].rstrip()


def _display_date(value) -> str:
    return str(value)[:10] if value else "未知"


def _compact_number(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(value)


def _category_label(category: str) -> str:
    labels = {
        "agent": "AI Agent",
        "design": "设计工具",
        "dev-tools": "开发工具",
        "infra-model": "模型基础设施",
        "other": "其他",
    }
    return labels.get(category, category.replace("-", " ").title())


def _category_class(category: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", category.lower()).strip("-")
    return f"category-{normalized or 'other'}"


def _source_badge_label(source: str) -> str:
    return "PH" if source == "producthunt" else "GH"


def _source_badge_class(source: str) -> str:
    return "ph" if source == "producthunt" else "gh"


def _json_list(value) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _safe_image_urls(values: list[str]) -> list[str]:
    return [url for value in values if (url := _safe_image_url(value))]


def _safe_image_url(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    return value if parsed.scheme == "https" and parsed.netloc else None


def _detail_images(links) -> list[dict[str, str]]:
    readme = [url for link in links for url in link["readme_images"]]
    images = [(url, "项目 README") for url in readme[:3]]
    if images:
        return [{"url": url, "source": source} for url, source in images]
    og_image = next((link["og_image"] for link in links if link["og_image"]), None)
    return [{"url": og_image, "source": "项目官网"}] if og_image else []


def _chat_context(sources, links) -> dict[str, object]:
    readme = next((source["readme_text"] for source in sources if source["readme_text"]), "")
    urls = []
    for link in links:
        for url in (link["url"], link["homepage"]):
            if url and url not in urls:
                urls.append(url)
    return {"readme_excerpt": " ".join(str(readme).split())[:2000], "links": urls[:4]}


def _heat_evidence(breakdown: dict) -> list[str]:
    github = breakdown.get("github")
    product_hunt = breakdown.get("producthunt")
    evidence = []
    if isinstance(github, dict):
        raw = github.get("raw") if isinstance(github.get("raw"), dict) else {}
        window = github.get("window_days") or 0
        evidence.append(
            f"最近 GitHub 增长明显：近 {window} 天 Stars +{raw.get('stars_delta') or 0}，Forks +{raw.get('forks_delta') or 0}。"
        )
        if github.get("used_prefill"):
            evidence.append("启动期已补充历史增长数据，便于观察早期热度。")
    if isinstance(product_hunt, dict):
        raw = product_hunt.get("raw") if isinstance(product_hunt.get("raw"), dict) else {}
        evidence.append(f"Product Hunt：{raw.get('votes_count') or 0} votes。")
    freshness = breakdown.get("freshness")
    if isinstance(freshness, dict) and freshness.get("enabled"):
        evidence.append(
            f"新近收录：进入榜单 {freshness.get('age_days') or 0} 天，适合继续关注后续变化。"
        )
    return evidence or ["尚未积累足够的热度窗口数据。"]


def _source_link(url: str | None, label: str) -> str:
    if not url:
        return ""
    return f'<a class="source-link" href="{html.escape(url, quote=True)}" rel="noopener noreferrer" target="_blank">{html.escape(label)}</a>'


def _safe_url(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _safe_https_url(value: str | None) -> str | None:
    """Only publish a production chat endpoint when it uses HTTPS."""
    safe = _safe_url(value)
    return safe if safe and urlparse(safe).scheme == "https" else None


def _json_object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


_SITE_CSS = """
:root{color-scheme:dark;--bg:#111619;--surface:#182025;--surface-raised:#1d272e;--line:#334149;--text:#edf3f4;--muted:#9babb1;--teal:#6be0c2;--coral:#ff9e7a;--gold:#e8c66e;--radius:8px}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Noto Sans SC",sans-serif}.topbar,main{max-width:1180px;margin:auto;padding-left:24px;padding-right:24px}.topbar{height:64px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--line)}.brand{display:inline-flex;align-items:center;gap:9px;color:var(--text);font-size:16px;font-weight:700;text-decoration:none}.brand-mark{display:grid;place-items:center;width:28px;height:28px;border:1px solid var(--teal);border-radius:6px;color:var(--teal);font-size:10px;letter-spacing:0}.back,.muted{color:var(--muted)}.overview{padding:42px 0 28px;border-bottom:1px solid var(--line)}.overview h1,.detail h1{max-width:760px;margin:5px 0 10px;font-size:30px;line-height:1.22;letter-spacing:0}.eyebrow{margin:0;color:var(--teal);font-size:12px;font-weight:700;letter-spacing:0}.status{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-top:16px;color:var(--muted);font-size:12px}.toolbar{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:16px 0;border-bottom:1px solid var(--line)}.tabs,.filters{display:flex;gap:6px;flex-wrap:wrap}.tab,.filter,.page-control{border:1px solid var(--line);border-radius:5px;background:transparent;color:var(--muted);padding:7px 10px;cursor:pointer;font:inherit;font-size:13px}.tab{display:inline-flex;align-items:center;gap:7px;color:var(--text)}.tab span{min-width:18px;color:var(--muted);font-size:12px}.tab.active,.filter.active{border-color:var(--teal);background:#173630;color:var(--text)}.tab.active span{color:var(--teal)}.tab:focus-visible,.filter:focus-visible,.page-control:focus-visible{outline:2px solid var(--coral);outline-offset:2px}.tab-panel{padding:26px 0 48px}.section-heading,.observation-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.section-heading h2,.observation-heading h2{margin:0;font-size:18px;line-height:1.3}.section-heading p,.observation-heading p{margin:4px 0 0;color:var(--muted);font-size:13px}.observation-heading{margin-top:12px;padding-top:24px;border-top:1px solid var(--line)}.observation-heading a{margin-top:3px;color:var(--teal);white-space:nowrap;font-size:13px}.product-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:18px 0}.product-card{min-height:302px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);overflow:hidden}.product-card a{display:flex;flex-direction:column;height:100%;padding:16px;color:inherit;text-decoration:none}.product-card:hover{border-color:var(--teal);background:var(--surface-raised)}.product-card header{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;min-height:22px}.source-badges,.flags{display:flex;gap:5px;flex-wrap:wrap}.source-badge,.flag{border-radius:4px;padding:2px 6px;font-size:10px;font-weight:700;line-height:1.25}.source-badge.gh{background:#29363d;color:#d8e5e8}.source-badge.ph{background:#4b303a;color:#ffc2cf}.flag.new{border:1px solid #368d7b;color:var(--teal)}.flag.hot{border:1px solid #a98741;color:var(--gold)}.flag.observation{border:1px solid #a46255;color:var(--coral)}.category-pill{align-self:flex-start;margin-top:14px;border:1px solid #3b5c64;border-radius:5px;background:#223139;color:#dbe9ec;padding:3px 8px;font-size:12px;font-weight:700;line-height:1.25}.product-card h2{margin:9px 0 8px;font-size:18px;line-height:1.28;letter-spacing:0}.card-summary{display:-webkit-box;overflow:hidden;margin:0;color:#c6d1d4;font-size:13px;line-height:1.62;-webkit-box-orient:vertical;-webkit-line-clamp:5}.product-card footer{display:flex;justify-content:space-between;gap:8px;margin-top:auto;padding-top:16px;color:var(--muted);font-size:11px}.core-signal{color:var(--muted)}.core-signal strong{color:var(--text);font-size:13px}.empty{margin:18px 0;color:var(--muted);font-size:14px}.filter-empty{padding-bottom:20px}.pagination{display:flex;align-items:center;justify-content:center;gap:12px;padding:4px 0 12px;color:var(--muted);font-size:13px}.page-control:disabled{cursor:default;opacity:.42}.detail{max-width:760px;padding:52px 0}.summary{font-size:18px;color:#d6e1e4}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:28px 0}.metrics div{border:1px solid var(--line);border-radius:6px;background:var(--surface);padding:12px}.metrics dt{font-size:12px;color:var(--muted)}.metrics dd{margin:4px 0 0}.source-links{display:flex;gap:10px;flex-wrap:wrap}.source-link{color:var(--teal)}@media(max-width:860px){.product-list{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.topbar,main{padding-left:16px;padding-right:16px}.topbar{height:56px}.overview{padding:30px 0 24px}.overview h1,.detail h1{font-size:26px}.toolbar{gap:12px}.product-list{grid-template-columns:1fr}.product-card{min-height:278px}.product-card footer{align-items:flex-start;flex-direction:column;gap:2px}.metrics{grid-template-columns:1fr}.observation-heading{display:block}.observation-heading a{display:inline-block;margin-top:10px}}
"""

_DETAIL_CSS = """
.detail-section{margin:28px 0}.detail-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:10px}.detail-heading h2{margin:0;font-size:18px;line-height:1.3}.detail-heading span{color:var(--muted);font-size:12px}.analysis .summary{margin:0}.image-gallery{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.project-image{min-width:0;margin:0;overflow:hidden;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}.project-image img{display:block;width:100%;height:156px;object-fit:cover;background:var(--surface-raised)}.project-image figcaption{padding:6px 8px;color:var(--muted);font-size:11px}.evidence{border-top:1px solid var(--line);padding-top:22px}.evidence ul{margin:0;padding-left:20px;color:#c6d1d4}.evidence li+li{margin-top:5px}.detail .source-links{margin-top:22px}.chat{border-top:1px solid var(--line);padding-top:22px}.chat .detail-heading{margin-bottom:12px}.chat .detail-heading span{border:1px solid #378d7a;border-radius:999px;background:#173630;color:var(--teal);padding:2px 8px}.chat-empty{border:1px solid var(--line);border-radius:var(--radius);background:#121a1e;padding:14px;color:#c6d1d4}.chat-empty p{margin:0 0 12px}.chat-prompts{display:flex;flex-wrap:wrap;gap:8px}.chat-prompts button{border:1px solid #3b5c64;border-radius:5px;background:#18252a;color:#d8e5e8;padding:6px 9px;cursor:pointer;font:inherit;font-size:12px}.chat-prompts button:hover:not(:disabled){border-color:var(--teal);color:var(--text)}.chat-messages{display:grid;gap:10px;min-height:180px;max-height:380px;overflow-y:auto;border:1px solid var(--line);border-radius:var(--radius);background:#121a1e;padding:14px;scroll-behavior:smooth}.chat-messages[hidden],.chat-empty[hidden]{display:none}.chat-message{max-width:86%;margin:0;padding:10px 12px;border-radius:8px;white-space:pre-wrap;line-height:1.58}.chat-message.user{justify-self:end;border:1px solid #378d7a;background:#23423a;color:#e7fbf5}.chat-message.assistant{justify-self:start;border:1px solid var(--line);background:var(--surface-raised);color:#d8e5e8}.chat-message.pending{color:var(--muted);font-style:italic}.chat-message.error{border-color:#a75b5f;color:#ffb7b7}.chat-sources{margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:12px;line-height:1.45}.chat-sources a{color:var(--teal)}.chat form{margin-top:12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);padding:12px}.chat textarea{display:block;width:100%;min-height:76px;resize:vertical;border:1px solid var(--line);border-radius:6px;background:#121a1e;color:var(--text);padding:10px;font:inherit}.chat textarea:focus{border-color:var(--teal);outline:2px solid #173630;outline-offset:1px}.chat-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px}.chat-web-toggle{color:var(--muted);font-size:12px;cursor:pointer}.chat-web-toggle input{accent-color:var(--teal)}.chat-actions span{margin-left:auto;color:var(--coral);font-size:12px}.chat-actions button{min-width:76px;border:1px solid var(--teal);border-radius:5px;background:#173630;color:var(--text);padding:7px 12px;cursor:pointer;font:inherit;font-size:13px}.chat-actions button:hover:not(:disabled){background:#1e4b40}.chat-actions button:disabled,.chat textarea:disabled,.chat-web-toggle input:disabled,.chat-prompts button:disabled{cursor:not-allowed;opacity:.6}.detail .status{padding-top:20px;border-top:1px solid var(--line)}@media(max-width:620px){.image-gallery{grid-template-columns:1fr}.project-image img{height:220px}.detail-heading{align-items:flex-start;flex-direction:column;gap:2px}.chat-messages{min-height:164px;padding:12px}.chat-message{max-width:94%}.chat-actions{align-items:flex-start;flex-wrap:wrap}.chat-actions span{margin-left:0;flex-basis:100%}.chat-actions button{margin-left:auto}}
"""

_POLISH_CSS = """
body{background:#0d1316;color:#eef5f6}.topbar{border-bottom-color:#25343b}.brand-mark{background:#10231f;box-shadow:0 0 0 3px rgba(107,224,194,.05)}.overview{padding-top:46px;padding-bottom:30px}.overview h1,.detail h1{letter-spacing:0;max-width:820px}.overview .muted{max-width:720px;color:#b8c8cd;font-size:15px}.status{color:#84979e}.toolbar{align-items:center;padding:18px 0}.tab,.filter,.page-control{border-color:#2b3d45;background:#111a1e;transition:border-color .16s ease,background .16s ease,color .16s ease}.tab:hover,.filter:hover,.page-control:hover:not(:disabled){border-color:#49616a;color:#e4eff1}.tab.active,.filter.active{border-color:#65d8bc;background:#122c28;box-shadow:inset 0 0 0 1px rgba(107,224,194,.12)}.tab span{display:inline-grid;place-items:center;min-width:24px;height:20px;border-radius:999px;background:#223138;color:#c5d3d7}.tab.active span{background:#173d36;color:#75e8ca}.section-heading h2,.observation-heading h2,.detail-heading h2{font-weight:750}.product-list{gap:14px;padding-top:20px}.product-card{border-color:#263942;background:#151e23;box-shadow:0 1px 0 rgba(255,255,255,.03),0 12px 30px rgba(0,0,0,.12);transition:transform .16s ease,border-color .16s ease,background .16s ease,box-shadow .16s ease}.product-card:hover{transform:translateY(-2px);border-color:#5bcdb2;background:#18242a;box-shadow:0 1px 0 rgba(255,255,255,.04),0 18px 40px rgba(0,0,0,.22)}.product-card a{padding:17px}.source-badge,.flag{border-radius:5px}.flag.new{background:#102b26}.flag.hot{background:#2a2414}.flag.observation{background:#2a1d19}.category-pill{border-color:#37515a;background:#1b2a30;color:#e2edef}.category-agent,.category-ai-agent{border-color:#3b806d;background:#163029;color:#9ff0d7}.category-dev-tools{border-color:#446c91;background:#172838;color:#a9d6ff}.category-infra-model{border-color:#837448;background:#2b2819;color:#eed994}.category-design{border-color:#8a5f69;background:#2d2025;color:#ffc7d2}.category-other{border-color:#43535a;background:#202b30;color:#cbd8dc}.product-card h2{font-size:19px;color:#f4fafb}.card-summary{color:#c2cfd3}.product-card footer{border-top:1px solid rgba(51,65,73,.65);padding-top:14px}.core-signal strong{font-size:14px}.detail{max-width:820px}.detail-section{margin:30px 0}.summary{font-size:18px;line-height:1.75;color:#dce8eb}.metrics{gap:12px}.metrics div{border-color:#2a3c44;background:#151f24;box-shadow:0 1px 0 rgba(255,255,255,.03)}.metrics dt{color:#8fa1a8}.metrics dd{font-size:16px;color:#edf5f6}.evidence{border-top-color:#273942}.evidence ul{padding-left:18px;color:#d2dde0}.evidence li+li{margin-top:8px}.source-links{padding-top:2px}.source-link{display:inline-flex;align-items:center;border:1px solid #386d61;border-radius:6px;background:#122820;color:#83efd0;padding:7px 10px;text-decoration:none}.source-link:hover{border-color:#6be0c2;color:#effffc}.chat{border-top-color:#273942}.chat .detail-heading span{border-color:#2c7768;background:#102822;font-size:11px}.chat-empty{border-color:#2a3d45;background:#121b20;padding:16px}.chat-empty p{color:#c7d4d8}.chat-prompts button{border-color:#30464f;background:#17242a;color:#d3e0e4;border-radius:999px;padding:7px 10px}.chat-prompts button:hover:not(:disabled){background:#19332f}.chat form{border-color:#2a3d45;background:#151f24;padding:13px}.chat textarea{min-height:70px;border-color:#2a3d45;background:#10181c}.chat-actions button{border-color:#65d8bc;background:#12332c;border-radius:6px;padding:8px 14px;font-weight:650}.chat-actions button:hover:not(:disabled){background:#17443a}.chat-messages{border-color:#2a3d45;background:#10181c}.detail .status{border-top-color:#273942}@media(max-width:620px){.overview .muted{font-size:14px}.tab,.filter{padding:7px 9px}.product-card a{padding:16px}.detail{padding-top:38px}.source-link{width:100%;justify-content:center}.chat-prompts button{width:100%;text-align:left}}
"""

_SITE_JS = """
(() => {
  const tabs = [...document.querySelectorAll('.tab')];
  const panels = [...document.querySelectorAll('.tab-panel')];
  const filters = [...document.querySelectorAll('.filter')];
  const historyCards = [...document.querySelectorAll('[data-history-card]')];
  const pageSize = 30;
  let activeTab = 'weekly';
  let activeCategory = 'all';
  let historyPage = 1;

  const matchesCategory = (card) => activeCategory === 'all' || card.dataset.category === activeCategory;

  function updateList(list) {
    const cards = [...list.querySelectorAll('.product-card')];
    const visible = cards.filter(matchesCategory);
    cards.forEach((card) => { card.hidden = !matchesCategory(card); });
    const empty = list.nextElementSibling;
    if (empty?.classList.contains('filter-empty')) empty.hidden = cards.length === 0 || visible.length > 0;
  }

  function updateHistory() {
    const visible = historyCards.filter(matchesCategory);
    const pages = Math.max(1, Math.ceil(visible.length / pageSize));
    historyPage = Math.min(historyPage, pages);
    historyCards.forEach((card) => { card.hidden = true; });
    visible.slice((historyPage - 1) * pageSize, historyPage * pageSize).forEach((card) => { card.hidden = false; });
    const panel = document.querySelector('[data-panel="all"]');
    const empty = panel.querySelector('.filter-empty');
    empty.hidden = historyCards.length === 0 || visible.length > 0;
    const pagination = panel.querySelector('.pagination');
    const status = panel.querySelector('[data-page-status]');
    const previous = panel.querySelector('[data-page-action="previous"]');
    const next = panel.querySelector('[data-page-action="next"]');
    pagination.hidden = visible.length <= pageSize;
    status.textContent = `第 ${historyPage} / ${pages} 页`;
    previous.disabled = historyPage === 1;
    next.disabled = historyPage === pages;
  }

  function render() {
    panels.forEach((panel) => {
      const selected = panel.dataset.panel === activeTab;
      panel.hidden = !selected;
      panel.classList.toggle('active', selected);
      if (!selected) return;
      if (activeTab === 'all') {
        updateHistory();
      } else {
        panel.querySelectorAll('.product-list').forEach(updateList);
      }
    });
  }

  tabs.forEach((button) => button.addEventListener('click', () => {
    activeTab = button.dataset.tab;
    historyPage = 1;
    tabs.forEach((item) => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', String(selected));
    });
    render();
  }));
  filters.forEach((button) => button.addEventListener('click', () => {
    activeCategory = button.dataset.category;
    historyPage = 1;
    filters.forEach((item) => item.classList.toggle('active', item === button));
    render();
  }));
  document.querySelector('[data-page-action="previous"]')?.addEventListener('click', () => { historyPage -= 1; render(); });
  document.querySelector('[data-page-action="next"]')?.addEventListener('click', () => { historyPage += 1; render(); });
  const appendChatMessage = (messages, className, text) => {
    const section = messages.closest('[data-chat]');
    section?.querySelector('[data-chat-empty]')?.setAttribute('hidden', '');
    messages.hidden = false;
    const item = document.createElement('p');
    item.className = `chat-message ${className}`;
    item.textContent = text;
    messages.append(item);
    messages.scrollTop = messages.scrollHeight;
    item.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    return item;
  };
  const appendChatSources = (reply, sources) => {
    if (!Array.isArray(sources) || !sources.length) return;
    const list = document.createElement('ul');
    list.className = 'chat-sources';
    sources.slice(0, 3).forEach((source) => {
      if (!source || typeof source.url !== 'string' || !source.url.startsWith('https://')) return;
      const item = document.createElement('li');
      const link = document.createElement('a');
      link.href = source.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = typeof source.title === 'string' && source.title ? source.title : source.url;
      item.append(link);
      list.append(item);
    });
    if (list.childElementCount) reply.after(list);
  };
  document.querySelectorAll('[data-chat-form]').forEach((form) => form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const section = form.closest('[data-chat]');
    const endpoint = section.dataset.endpoint;
    const textarea = form.elements.message;
    const error = form.querySelector('[data-chat-error]');
    const messages = section.querySelector('[data-chat-messages]');
    const button = form.querySelector('button');
    const promptButtons = [...section.querySelectorAll('[data-chat-prompt]')];
    const webToggle = form.elements.use_web;
    const state = section.querySelector('[data-chat-state]');
    const message = textarea.value.trim();
    if (!endpoint || !message) return;
    button.disabled = true;
    textarea.disabled = true;
    promptButtons.forEach((prompt) => { prompt.disabled = true; });
    if (webToggle) webToggle.disabled = true;
    button.textContent = '回答中...';
    error.textContent = '';
    appendChatMessage(messages, 'user', message);
    const reply = appendChatMessage(messages, 'assistant pending', '正在生成回答...');
    textarea.value = '';
    state.textContent = '正在回答...';
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({product_slug: section.dataset.productSlug, message, use_web: Boolean(webToggle?.checked)}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || typeof payload.reply !== 'string') throw new Error(payload.error || 'request_failed');
      reply.textContent = payload.reply;
      reply.classList.remove('pending');
      appendChatSources(reply, payload.sources);
      state.textContent = payload.retrieval === 'unavailable' ? '已使用项目资料回答' : '服务已连接';
      messages.scrollTop = messages.scrollHeight;
      reply.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    } catch (failure) {
      const labels = {rate_limited: '请求过于频繁，请稍后再试。', budget_exhausted: '本月对话额度已用完。', quota_unavailable: '对话配额服务暂时不可用，请稍后重试。', model_unavailable: '当前无法完成回答，请稍后再试。'};
      const label = labels[failure.message] || '当前无法完成回答，请稍后再试。';
      reply.textContent = label;
      reply.classList.remove('pending');
      reply.classList.add('error');
      error.textContent = '请检查网络后重试。';
      textarea.value = message;
      state.textContent = '请求失败';
      messages.scrollTop = messages.scrollHeight;
      reply.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    } finally {
      button.disabled = false;
      textarea.disabled = false;
      promptButtons.forEach((prompt) => { prompt.disabled = false; });
      if (webToggle) webToggle.disabled = false;
      button.textContent = '发送';
    }
  }));
  document.querySelectorAll('[data-chat-prompt]').forEach((button) => button.addEventListener('click', () => {
    const section = button.closest('[data-chat]');
    const form = section?.querySelector('[data-chat-form]');
    const textarea = form?.elements.message;
    if (!form || !textarea || button.disabled) return;
    textarea.value = button.dataset.chatPrompt || '';
    form.requestSubmit();
  }));
  if (tabs.length) render();
})();
"""
