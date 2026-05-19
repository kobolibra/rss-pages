#!/usr/bin/env python3
import html
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
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
    "from sitemap discovery plus article extraction, so feed readers can open local pages."
)
SITE_BASE = "https://www.citadelsecurities.com"
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
        f"https://r.jina.ai/http://{stripped}",
        f"https://r.jina.ai/{url}",
    ]


def looks_blocked(text: str) -> bool:
    head = text[:2000] if text else ""
    return any(marker in head for marker in BLOCKED_MARKERS)


def parse_iso_to_rss(value: str | None) -> str:
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return format_datetime(dt.astimezone(timezone.utc), usegmt=True)
        except Exception:
            pass
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


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
            rel = urlparse(link).path.lstrip("/")
            out_dir = site_dir / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_bytes(item_bytes)
    except Exception:
        pass
    return True


def fetch_post_candidates() -> list[dict]:
    xml_text = fetch_text(POST_SITEMAP_URL, timeout=30)
    root = ET.fromstring(xml_text)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    items = []
    for node in root.findall(f"{ns}url"):
        loc = (node.findtext(f"{ns}loc") or "").strip()
        lastmod = (node.findtext(f"{ns}lastmod") or "").strip()
        if "/news-and-insights/" not in loc:
            continue
        items.append({"url": loc, "lastmod": lastmod})
    return list(reversed(items))


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
    published_iso = None
    m = re.search(r"^Published Time:\s*(.+)$", source, flags=re.M)
    if m:
        published_iso = m.group(1).strip()
    if not published_iso:
        published_iso = candidate.get("lastmod") or None

    body = source.split("Markdown Content:", 1)[1].strip()

    category = None
    title = meta_title or slugify(urlparse(url).path.rstrip("/").split("/")[-1]).replace("-", " ").title()

    cm = CATEGORY_RE.search(body)
    if cm:
        category = cm.group(1).strip()
        title = cm.group(2).strip() or title
        body = body[cm.end():].strip()
        if category != "Market Insights":
            return None
    else:
        # 新文章有时会被 Jina 直接抽成正文片段，没有 breadcrumb。
        # 这时只能在保证是 /news-and-insights/ 正文且内容足够长的前提下做兜底收录。
        if "/news-and-insights/" not in url:
            return None

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
        "rss_date": parse_iso_to_rss(published_iso),
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
        guid.text = item["local_url"]
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

    built_items = []
    seen_titles = set()
    candidates = fetch_post_candidates()[:MAX_CANDIDATES]
    for candidate in candidates:
        article = parse_article(candidate)
        if not article:
            continue
        title_key = article["title"].strip().lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        slug = slugify(urlparse(article["source_url"]).path.rstrip("/").split("/")[-1])
        local_url = f"{public_base.rstrip('/')}/item/{FEED_NAME}/{slug}/"
        description = article["body_text"][:320].strip()
        if len(article["body_text"]) > 320:
            description += "..."

        item_dir = site_dir / "item" / FEED_NAME / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        body_html = markdown_to_html(article["body_markdown"])
        (item_dir / "index.html").write_text(
            build_item_page(article["title"], article["source_url"], article["published_iso"], body_html),
            encoding="utf-8",
        )

        article["local_url"] = local_url
        article["description"] = description
        built_items.append(article)
        if len(built_items) >= MAX_ITEMS:
            break

    if len(built_items) < MIN_ITEMS and restore_live_feed(public_base, site_dir):
        print(f"restored live {FEED_NAME}: freshly built items={len(built_items)}")
        sys.exit(0)

    if not built_items:
        raise SystemExit("No Citadel Market Insights items could be built")

    xml = build_xml(built_items, public_base)
    (site_dir / f"{FEED_NAME}.xml").write_bytes(xml)
    print(f"built {FEED_NAME} with {len(built_items)} items from {len(candidates)} candidates")
