import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

import requests

LIST_URL = "https://am.gs.com/services/search-engine/en-us/institutions/search/insights?q=&hitsPerPage=50&insightType=article,report-survey"
SITE_BASE = "https://am.gs.com"
FEED_NAME = "gsam_insights"
FEED_TITLE = "Goldman Sachs Asset Management Insights"
FEED_DESC = "GSAM insights feed built from the official search JSON and rewritten to local item pages for better reader compatibility."
UA = "Mozilla/5.0"


def fetch_list() -> dict:
    r = requests.get(
        LIST_URL,
        headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def parse_iso(dt: str) -> str:
    if not dt:
        return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    try:
        z = dt.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(z)
    except Exception:
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')


def build_item_page(item: dict, page_url: str) -> str:
    title = item.get("title") or item.get("summaryTitle") or "Item"
    desc = item.get("summaryDescription") or item.get("summaryTeaserText") or ""
    social_desc = item.get("socialDescription") or ""
    publish = item.get("publishDate") or ""
    subtype = item.get("subType") or ""
    theme = item.get("theme") or item.get("capability") or ""
    key_takeaways = item.get("keyTakeaways") or []

    parts = [f"<p>来源：{html.escape(page_url)}</p>"]
    meta_bits = []
    if subtype:
        meta_bits.append(f"类型：{html.escape(subtype)}")
    if publish:
        meta_bits.append(f"发布时间：{html.escape(publish)}")
    if theme:
        meta_bits.append(f"主题：{html.escape(str(theme))}")
    if meta_bits:
        parts.append(f"<p>{' ｜ '.join(meta_bits)}</p>")
    if desc:
        parts.append(f"<p>{html.escape(desc)}</p>")
    if social_desc and social_desc != desc:
        parts.append(f"<p>{html.escape(social_desc)}</p>")
    if key_takeaways:
        parts.append("<h2>Key Takeaways</h2><ul>")
        for kt in key_takeaways[:10]:
            kt_title = (kt or {}).get("title", "")
            kt_desc = (kt or {}).get("description", "")
            bits = []
            if kt_title:
                bits.append(f"<strong>{html.escape(kt_title.strip())}</strong>")
            if kt_desc:
                bits.append(html.escape(re.sub(r'\s+', ' ', kt_desc).strip()))
            if bits:
                parts.append(f"<li>{' — '.join(bits)}</li>")
        parts.append("</ul>")

    return f"""<!doctype html>
<html>
<meta charset=\"utf-8\">
<head><title>{html.escape(title)}</title></head>
<body>
  <h1>{html.escape(title)}</h1>
  {''.join(parts)}
</body>
</html>
"""


def build_xml(items: list[dict], public_base: str) -> bytes:
    rss = Element('rss', version='2.0')
    channel = SubElement(rss, 'channel')
    SubElement(channel, 'title').text = FEED_TITLE
    SubElement(channel, 'link').text = f"{public_base.rstrip('/')}/{FEED_NAME}.xml"
    SubElement(channel, 'description').text = FEED_DESC
    SubElement(channel, 'lastBuildDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(channel, 'generator').text = 'GitHub Pages RSS rewrite'

    for item in items:
        title = item.get("title") or item.get("summaryTitle") or "Untitled"
        page_path = item.get("pagePath") or item.get("slug") or ""
        page_url = urljoin(SITE_BASE, page_path)
        slug = slugify(page_path.split('/')[-1] if page_path else title)
        local_url = f"{public_base.rstrip('/')}/item/{FEED_NAME}/{slug}/"
        description = item.get("summaryDescription") or item.get("summaryTeaserText") or item.get("socialDescription") or ""

        rss_item = SubElement(channel, 'item')
        SubElement(rss_item, 'title').text = title.strip()
        SubElement(rss_item, 'link').text = local_url
        guid = SubElement(rss_item, 'guid')
        guid.set('isPermaLink', 'true')
        guid.text = local_url
        SubElement(rss_item, 'pubDate').text = parse_iso(item.get("publishDate", ""))
        if description:
            SubElement(rss_item, 'description').text = re.sub(r'\s+', ' ', description).strip()

    return minidom.parseString(ET.tostring(rss, encoding='utf-8')).toprettyxml(indent='  ', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python build_gsam_insights_feed.py <site_dir> <public_base>')
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    site_dir.mkdir(parents=True, exist_ok=True)

    raw = fetch_list()
    hits = (((raw or {}).get('insights') or {}).get('hits') or [])
    items = []
    for hit in hits:
        items.append(hit)
        title = hit.get("title") or hit.get("summaryTitle") or "Untitled"
        page_path = hit.get("pagePath") or hit.get("slug") or ""
        page_url = urljoin(SITE_BASE, page_path)
        slug = slugify(page_path.split('/')[-1] if page_path else title)
        item_dir = site_dir / 'item' / FEED_NAME / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / 'index.html').write_text(build_item_page(hit, page_url), encoding='utf-8')

    xml = build_xml(items, public_base)
    (site_dir / f'{FEED_NAME}.xml').write_bytes(xml)
    print(f'built {FEED_NAME} with {len(items)} items')
