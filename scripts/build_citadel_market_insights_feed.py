#!/usr/bin/env python3
import html
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

import requests

FEED_NAME = "citadel_market_insights"
FEED_TITLE = "Citadel Securities Market Insights"
FEED_DESC = (
    "Citadel Securities Market Insights rebuilt to local full-text item pages "
    "from the official Market Insights category feed plus article extraction, so feed readers can open local pages."
)
SITE_BASE = "https://www.citadelsecurities.com"
CATEGORY_FEED_URL = f"{SITE_BASE}/news-and-insights/category/market-insights/feed/"
CATEGORY_PAGE_FALLBACKS = [
    "https://r.jina.ai/http://www.citadelsecurities.com/zh-hans/news-and-insights/category/%E5%B8%82%E5%9C%BA%E8%A7%82%E7%82%B9/",
    "https://r.jina.ai/http://www.citadelsecurities.com/news-and-insights/category/market-insights/",
]
POST_SITEMAP_URL = f"{SITE_BASE}/post-sitemap.xml"
UA = "Mozilla/5.0 (compatible; GitHubActions-RSS-Mirror/1.0)"
MAX_ITEMS = 20
MAX_CANDIDATES = 40
MIN_ITEMS = 5
BLOCKED_MARKERS = (
    "Warning: Target URL returned error 403",
    "Performing security verification",
    "Just a moment...",
)
CATEGORY_RE = re.compile(
    r"\[(Market Insights|In the Media|Policy Positions|Announcements)\]\([^\)]+\)/\[(.*?)\]\((.*?)\)",
    re.S,
)
DATE_LINE_RE = re.compile(r"^[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\s*$", re.M)
RSS_DATE_LINE_RE = re.compile(r"^[A-Z][a-z]{2},\s+\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+[+-]\d{4}$")
CATEGORY_SECTION_RE = re.compile(r"## Market Insights\s*(.*?)\s*## Policy Positions", re.S)
CATEGORY_CARD_RE = re.compile(
    r"\[\]\((https://www\.citadelsecurities\.com/news-and-insights/[^)]+/)\)\s*\n\n"
    r"!\[Image[^\n]*\n\n([^\n]+)\n\n"
    r"Series:\[([^\]]+)\]\((https://www\.citadelsecurities\.com/news-and-insights/series/[^)]+)\)",
    re.S,
)


def fetch_text(url: str, timeout: int = 60) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.content


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def jina_proxy_urls(url: str) -> list[str]:
    stripped = re.sub(r"^https?://", "", url)
    return [
        f"https://r.jina.ai/{url}",
        f"https://r.jina.ai/http://{stripped}",
    ]


def looks_blocked(text: str) -> bool:
    head = text[:2000] if text else ""
    return any(marker in head for marker in BLOCKED_MARKERS)


def parse_iso_to_rss(value: str | None) -> str:
    dt = parse_sort_datetime(value)
    if dt != datetime.min.replace(tzinfo=timezone.utc):
        return format_datetime(dt.astimezone(timezone.utc), usegmt=True)
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def normalize_published_datetime(value: str | None) -> tuple[str | None, str]:
    dt = parse_sort_datetime(value)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return None, format_datetime(datetime.now(timezone.utc), usegmt=True)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), format_datetime(dt, usegmt=True)


def parse_sort_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def strip_markdown_links(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\(([^\)]+)\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"\1", text)
    text = re.sub(r"[*_`>#-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return html.unescape(text).strip()


def cleanup_article_markdown(body: str, title: str) -> str:
    body = body.replace("\r", "").strip()
    body = re.sub(r"^#\s+" + re.escape(title) + r"(?:\s+-\s+Citadel Securities)?\s*", "", body, count=1, flags=re.M)
    body = re.sub(r"^#\s+.*?\s+-\s+Citadel Securities\s*", "", body, count=1, flags=re.M)
    body = DATE_LINE_RE.sub("", body, count=1)
    body = re.sub(r"^Share on\s*$", "", body, flags=re.M)
    body = re.sub(r"^\*\s*\[\]\([^\)]*linkedin[^\)]*\)\s*$", "", body, flags=re.M | re.I)
    body = re.sub(r"^\*\s*\[\]\([^\)]*x\.com[^\)]*\)\s*$", "", body, flags=re.M | re.I)
    body = re.sub(r"^\*\s*\[\]\([^\)]*twitter[^\)]*\)\s*$", "", body, flags=re.M | re.I)
    body = re.sub(r"\n## Explore\s*[\s\S]*$", "", body, flags=re.I)
    body = re.sub(r"\n### Manage Consent Preferences[\s\S]*$", "", body, flags=re.I)
    body = re.sub(r"\nThis website uses cookies[\s\S]*$", "", body, flags=re.I)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def inline_markdown_to_html(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"!\[([^\]]*)\]\(([^\)]+)\)", lambda m: f'<img src="{html.escape(m.group(2), quote=True)}" alt="{html.escape(m.group(1), quote=True)}" loading="lazy" />', text)
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"_([^_]+)_", r"<em>\1</em>", text)
    return text


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("### "):
            out.append(f"<h3>{inline_markdown_to_html(stripped[4:].strip())}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{inline_markdown_to_html(stripped[3:].strip())}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            out.append(f"<h2>{inline_markdown_to_html(stripped[2:].strip())}</h2>")
            i += 1
            continue
        if stripped.startswith("> "):
            out.append(f"<blockquote>{inline_markdown_to_html(stripped[2:].strip())}</blockquote>")
            i += 1
            continue
        if re.fullmatch(r"!\[[^\]]*\]\([^\)]+\)", stripped):
            out.append(f"<figure>{inline_markdown_to_html(stripped)}</figure>")
            i += 1
            continue
        if stripped.startswith("* "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("* "):
                items.append(f"<li>{inline_markdown_to_html(lines[i].strip()[2:].strip())}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        block = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                i += 1
                break
            if nxt.startswith(("# ", "## ", "### ", "* ", "> ")) or re.fullmatch(r"!\[[^\]]*\]\([^\)]+\)", nxt):
                break
            block.append(nxt)
            i += 1
        out.append(f"<p>{inline_markdown_to_html(' '.join(block))}</p>")
    return "\n".join(out)


def build_item_page(title: str, source_url: str, published_iso: str | None, body_html: str) -> str:
    published = html.escape((published_iso or "").replace("T", " ").replace("+00:00", " UTC"))
    source_html = f'<p class="source">Source: <a href="{html.escape(source_url, quote=True)}">{html.escape(source_url)}</a></p>'
    meta_html = f'<p class="meta">Published: {published}</p>' if published else ""
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(title)}</title>
  <link rel=\"canonical\" href=\"{html.escape(source_url, quote=True)}\">
  <style>
    body {{ font-family: Georgia, \"Times New Roman\", serif; color:#111; background:#fff; max-width: 860px; margin: 40px auto; padding: 0 18px; line-height: 1.75; }}
    h1, h2, h3 {{ line-height: 1.25; }}
    h1 {{ margin-bottom: .6rem; }}
    .meta, .source {{ color:#666; font-size:.95rem; }}
    img {{ max-width:100%; height:auto; display:block; margin: 1rem 0; }}
    figure {{ margin: 1.25rem 0; }}
    blockquote {{ border-left: 4px solid #ddd; padding-left: 1rem; color:#333; margin: 1.25rem 0; }}
    a {{ color:#0b57d0; }}
  </style>
</head>
<body>
  <article>
    <h1>{html.escape(title)}</h1>
    {meta_html}
    {source_html}
    <div>{body_html}</div>
  </article>
</body>
</html>
"""


def local_site_rel_from_url(public_base: str, url: str) -> str:
    item_path = urlparse(url).path
    base_path = urlparse(public_base.rstrip("/")).path.rstrip("/")
    if base_path and item_path.startswith(base_path):
        item_path = item_path[len(base_path):]
    return item_path.lstrip("/")


def item_slug_from_local_url(link: str) -> str | None:
    parts = [p for p in urlparse(link).path.split("/") if p]
    if "item" not in parts:
        return None
    idx = parts.index("item")
    if len(parts) <= idx + 2:
        return None
    if parts[idx + 1] != FEED_NAME:
        return None
    return parts[idx + 2]


def parse_existing_items(xml_path: Path) -> list[dict]:
    root = ET.parse(xml_path).getroot()
    channel = root.find("channel")
    if channel is None:
        return []

    items: list[dict] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        local_url = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or local_url).strip()
        rss_date = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        slug = item_slug_from_local_url(local_url)
        if not local_url or not slug:
            continue
        items.append(
            {
                "title": title,
                "local_url": local_url,
                "guid": guid,
                "rss_date": rss_date,
                "description": description,
                "slug": slug,
            }
        )
    return items


def restore_live_feed(public_base: str, site_dir: Path) -> bool:
    feed_url = f"{public_base.rstrip('/')}/{FEED_NAME}.xml"
    try:
        xml_bytes = fetch_bytes(feed_url, timeout=30)
    except Exception:
        return False

    xml_path = site_dir / f"{FEED_NAME}.xml"
    xml_path.write_bytes(xml_bytes)

    try:
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return True
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            if not link.startswith(public_base.rstrip("/") + "/item/"):
                continue
            try:
                item_bytes = fetch_bytes(link, timeout=30)
            except Exception:
                continue
            rel = local_site_rel_from_url(public_base, link)
            out_dir = site_dir / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_bytes(item_bytes)
    except Exception:
        pass
    return True


def load_existing_items(site_dir: Path, public_base: str) -> list[dict]:
    xml_path = site_dir / f"{FEED_NAME}.xml"
    if not xml_path.exists():
        restore_live_feed(public_base, site_dir)
    if not xml_path.exists():
        return []
    try:
        return parse_existing_items(xml_path)
    except Exception:
        return []


def fetch_post_candidates() -> list[dict]:
    try:
        xml_bytes = fetch_bytes(CATEGORY_FEED_URL, timeout=30)
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            raise RuntimeError("Official Citadel Market Insights category feed XML missing channel")

        items: list[dict] = []
        for node in channel.findall("item"):
            title = (node.findtext("title") or "").strip()
            url = (node.findtext("link") or "").strip()
            rss_date = (node.findtext("pubDate") or "").strip()
            if not url.startswith(f"{SITE_BASE}/news-and-insights/"):
                continue
            if not title or not url:
                continue
            items.append({"title": title, "url": url, "rss_date": rss_date})

        if items:
            return sorted(items, key=lambda item: parse_sort_datetime(item.get("rss_date")), reverse=True)
    except Exception:
        pass

    items = fetch_category_page_candidates()
    if not items:
        raise RuntimeError("Citadel Market Insights source returned zero usable items")
    return items


def fetch_category_page_candidates() -> list[dict]:
    last_error = None
    for url in CATEGORY_PAGE_FALLBACKS:
        try:
            text = fetch_text(url, timeout=30)
        except Exception as e:
            last_error = e
            continue
        if looks_blocked(text) or "## Market Insights" not in text:
            continue

        sm = CATEGORY_SECTION_RE.search(text)
        if not sm:
            continue
        section = sm.group(1)

        items: list[dict] = []
        seen_urls = set()
        for m in CATEGORY_CARD_RE.finditer(section):
            article_url = m.group(1).strip()
            title = html.unescape(m.group(2).strip())
            series = html.unescape(m.group(3).strip())
            if article_url in seen_urls:
                continue
            seen_urls.add(article_url)
            items.append({
                "title": title,
                "url": article_url,
                "series": series,
                "rss_date": None,
            })

        if items:
            return items

    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Could not fetch usable Citadel Market Insights category page fallback{detail}")
def fetch_article_source(url: str) -> str | None:
    for proxy_url in jina_proxy_urls(url):
        try:
            text = fetch_text(proxy_url, timeout=25)
        except Exception:
            continue
        if looks_blocked(text):
            continue
        if "Markdown Content:" not in text:
            continue
        return text
    return None


def parse_article(candidate: dict) -> dict | None:
    url = candidate["url"]
    source = fetch_article_source(url)
    if not source:
        return None

    meta_title = ""
    m = re.search(r"^Title:\s*(.+)$", source, flags=re.M)
    if m:
        meta_title = m.group(1).strip()

    published_value = None
    m = re.search(r"^Published Time:\s*(.+)$", source, flags=re.M)
    if m:
        published_value = m.group(1).strip()
    if not published_value:
        published_value = candidate.get("rss_date") or candidate.get("lastmod") or None
    published_iso, rss_date = normalize_published_datetime(published_value)

    body = source.split("Markdown Content:", 1)[1].strip()

    title = candidate.get("title") or meta_title or slugify(urlparse(url).path.rstrip("/").split("/")[-1]).replace("-", " ").title()

    cm = CATEGORY_RE.search(body)
    if cm:
        title = cm.group(2).strip() or title
        body = body[cm.end():].strip()

    body = cleanup_article_markdown(body, title)
    if not body:
        return None

    plain = strip_markdown_links(body)
    if len(plain) < 280:
        return None

    return {
        "title": title,
        "source_url": url,
        "published_iso": published_iso,
        "rss_date": rss_date,
        "body_markdown": body,
        "body_text": plain,
    }


def build_xml(items: list[dict], public_base: str) -> bytes:
    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = FEED_TITLE
    SubElement(channel, "link").text = f"{public_base.rstrip('/')}/{FEED_NAME}.xml"
    SubElement(channel, "description").text = FEED_DESC
    SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc), usegmt=True)
    SubElement(channel, "generator").text = "GitHub Pages RSS rewrite"

    for item in items:
        rss_item = SubElement(channel, "item")
        SubElement(rss_item, "title").text = item["title"]
        SubElement(rss_item, "link").text = item["local_url"]
        guid = SubElement(rss_item, "guid")
        guid.set("isPermaLink", "true")
        guid.text = item.get("guid") or item["local_url"]
        SubElement(rss_item, "pubDate").text = item["rss_date"]
        SubElement(rss_item, "description").text = item["description"]

    return minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_citadel_market_insights_feed.py <site_dir> <public_base>")
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    site_dir.mkdir(parents=True, exist_ok=True)

    existing_items = load_existing_items(site_dir, public_base)
    existing_by_slug = {item["slug"]: item for item in existing_items if item.get("slug")}

    candidates = fetch_post_candidates()[:MAX_CANDIDATES]
    parsed_candidates = []
    seen_candidate_slugs = set()
    seen_candidate_titles = set()
    preserved_items = 0
    for candidate in candidates:
        source_slug = slugify(urlparse(candidate["url"]).path.rstrip("/").split("/")[-1])
        if source_slug in seen_candidate_slugs:
            continue

        article = parse_article(candidate)
        if not article:
            existing_item = existing_by_slug.get(source_slug)
            if not existing_item:
                continue
            article = existing_item.copy()
            article["title"] = candidate.get("title") or article.get("title") or source_slug.replace("-", " ").title()
            article["sort_dt"] = parse_sort_datetime(candidate.get("rss_date") or article.get("rss_date"))
            article["rss_date"] = parse_iso_to_rss(candidate.get("rss_date") or article.get("rss_date"))
            article["slug"] = source_slug
            article["preserved_existing"] = True
            preserved_items += 1
        else:
            title_key = article["title"].strip().lower()
            if title_key in seen_candidate_titles:
                continue
            article["slug"] = source_slug
            article["sort_dt"] = parse_sort_datetime(article.get("published_iso") or candidate.get("rss_date") or article.get("rss_date"))
            article["preserved_existing"] = False

        seen_candidate_slugs.add(source_slug)
        seen_candidate_titles.add(article["title"].strip().lower())
        parsed_candidates.append(article)

    parsed_candidates.sort(key=lambda item: item["sort_dt"], reverse=True)
    selected_items = parsed_candidates[:MAX_ITEMS]

    if len(selected_items) < MIN_ITEMS:
        print(
            f"warning: only {len(selected_items)} verified {FEED_NAME} items after strict category filtering; "
            "rebuilding without backfilling old items"
        )

    selected_slugs = {item["slug"] for item in selected_items}
    new_items = 0
    for article in selected_items:
        source_slug = article["slug"]
        if article.get("preserved_existing"):
            continue
        local_url = f"{public_base.rstrip('/')}/item/{FEED_NAME}/{source_slug}/"
        description = article["body_text"][:320].strip()
        if len(article["body_text"]) > 320:
            description += "..."

        item_dir = site_dir / "item" / FEED_NAME / source_slug
        item_dir.mkdir(parents=True, exist_ok=True)
        body_html = markdown_to_html(article["body_markdown"])
        (item_dir / "index.html").write_text(
            build_item_page(article["title"], article["source_url"], article["published_iso"], body_html),
            encoding="utf-8",
        )

        article["local_url"] = local_url
        article["guid"] = local_url
        article["description"] = description
        if source_slug not in existing_by_slug:
            new_items += 1

    xml = build_xml(selected_items, public_base)
    (site_dir / f"{FEED_NAME}.xml").write_bytes(xml)
    removed_items = len([slug for slug in existing_by_slug if slug not in selected_slugs])
    print(
        f"rebuilt {FEED_NAME} by published date: selected_items={len(selected_items)}, new_items={new_items}, "
        f"removed_items={removed_items}, candidates_scanned={len(candidates)}, parsed_candidates={len(parsed_candidates)}"
    )
