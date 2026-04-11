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
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>')


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


def fetch_next_json(page_url: str) -> dict:
    r = requests.get(page_url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = NEXT_RE.search(r.text)
    if not m:
        raise RuntimeError(f"__NEXT_DATA__ not found: {page_url}")
    return json.loads(m.group(1))


def clean_html_fragment(fragment: str) -> str:
    fragment = fragment or ""
    fragment = fragment.replace("\r", "").strip()
    return fragment


def extract_article_html(page_json: dict) -> tuple[str, str, list[dict], list[dict], str]:
    page_props = ((page_json or {}).get("props") or {}).get("pageProps") or {}
    data = page_props.get("data") or {}
    props = data.get("properties") or {}
    items = data.get("items") or {}

    summary = props.get("summaryDescription") or props.get("summaryTeaserText") or props.get("socialDescription") or ""
    raw_key_takeaways = props.get("keyTakeaways") or []
    key_takeaways = [x for x in raw_key_takeaways if isinstance(x, dict)]
    raw_authors = props.get("authorDetails") or []
    if isinstance(raw_authors, dict):
        authors = [raw_authors]
    elif isinstance(raw_authors, list):
        authors = [x for x in raw_authors if isinstance(x, dict)]
    else:
        authors = []
    read_meta = ""
    if props.get("summaryDisplaydate"):
        read_meta = props.get("summaryDisplaydate")

    html_parts = []

    main_text = items.get("text") if isinstance(items, dict) else None
    if isinstance(main_text, dict) and main_text.get("text"):
        html_parts.append(clean_html_fragment(main_text.get("text", "")))

    if isinstance(items, dict):
        for key, value in items.items():
            if key == "text" or not isinstance(value, dict):
                continue
            vtype = value.get("type") or ""
            title = (value.get("title") or "").strip()

            if vtype == "text" and value.get("text"):
                if title:
                    html_parts.append(f"<h2>{html.escape(title)}</h2>")
                html_parts.append(clean_html_fragment(value.get("text", "")))
                continue

            if vtype == "paragraphlist":
                if title:
                    html_parts.append(f"<h2>{html.escape(title)}</h2>")
                rich = clean_html_fragment(value.get("richText", ""))
                if rich:
                    html_parts.append(rich)
                plist = value.get("paragraphList") or []
                if plist:
                    html_parts.append("<ul>")
                    for entry in plist:
                        etitle = (entry or {}).get("title", "").strip()
                        edesc = clean_html_fragment((entry or {}).get("description", ""))
                        if etitle and edesc:
                            html_parts.append(f"<li><strong>{html.escape(etitle)}</strong> — {edesc}</li>")
                        elif etitle:
                            html_parts.append(f"<li><strong>{html.escape(etitle)}</strong></li>")
                        elif edesc:
                            html_parts.append(f"<li>{edesc}</li>")
                    html_parts.append("</ul>")
                continue

            if title and vtype in {"horizontaltab", "quote", "inlinevideo", "promotionalblock"}:
                html_parts.append(f"<h2>{html.escape(title)}</h2>")
                for text_key in ["introduction", "description", "text", "richText"]:
                    frag = clean_html_fragment(value.get(text_key, ""))
                    if frag:
                        html_parts.append(frag)
                if vtype == "horizontaltab":
                    tab_items = value.get("items") or []
                    for tab in tab_items:
                        tab_title = (tab or {}).get("title", "").strip()
                        tab_intro = clean_html_fragment((tab or {}).get("introduction", ""))
                        tab_rich = clean_html_fragment((tab or {}).get("richText", ""))
                        if tab_title:
                            html_parts.append(f"<h3>{html.escape(tab_title)}</h3>")
                        if tab_intro:
                            html_parts.append(tab_intro)
                        if tab_rich:
                            html_parts.append(tab_rich)

    full_html = "\n".join([part for part in html_parts if part.strip()])
    return summary, full_html, key_takeaways, authors, read_meta


def build_item_page(item: dict, page_url: str, article_html: str, summary: str, key_takeaways: list[dict], authors: list[dict], read_meta: str) -> str:
    title = item.get("title") or item.get("summaryTitle") or "Item"
    publish = item.get("publishDate") or ""
    subtype = item.get("subType") or ""
    theme = item.get("theme") or item.get("capability") or ""

    parts = [f"<p>来源：<a href=\"{html.escape(page_url)}\">{html.escape(page_url)}</a></p>"]
    meta_bits = []
    if subtype:
        meta_bits.append(f"类型：{html.escape(subtype)}")
    if publish:
        meta_bits.append(f"发布时间：{html.escape(publish)}")
    if read_meta:
        meta_bits.append(f"页面展示：{html.escape(read_meta)}")
    if theme:
        meta_bits.append(f"主题：{html.escape(str(theme))}")
    if meta_bits:
        parts.append(f"<p>{' ｜ '.join(meta_bits)}</p>")

    if authors:
        parts.append("<h2>Author(s)</h2><ul>")
        for a in authors:
            if not isinstance(a, dict):
                continue
            person_ref = a.get("personReferencePath") or {}
            if not isinstance(person_ref, dict):
                continue
            meta = person_ref.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            raw_title = meta.get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                name = raw_title.strip()
            else:
                first_name = meta.get("firstName") or ""
                last_name = meta.get("lastName") or ""
                fallback_title = a.get("title") if isinstance(a.get("title"), str) else ""
                fallback_name = a.get("name") if isinstance(a.get("name"), str) else ""
                name = f"{first_name} {last_name}".strip() or fallback_title or fallback_name or ""
            job = meta.get("jobTitle") if isinstance(meta.get("jobTitle"), str) else ""
            if name and job:
                parts.append(f"<li><strong>{html.escape(str(name))}</strong> — {html.escape(str(job))}</li>")
            elif name:
                parts.append(f"<li><strong>{html.escape(str(name))}</strong></li>")
        parts.append("</ul>")

    if summary:
        parts.append(f"<p><strong>摘要：</strong>{html.escape(summary)}</p>")

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

    if article_html:
        parts.append("<h2>全文</h2>")
        parts.append(article_html)
    else:
        parts.append("<p><strong>未抓到全文块。</strong></p>")

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
        page_json = fetch_next_json(page_url)
        summary, article_html, key_takeaways, authors, read_meta = extract_article_html(page_json)
        item_dir = site_dir / 'item' / FEED_NAME / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / 'index.html').write_text(
            build_item_page(hit, page_url, article_html, summary, key_takeaways, authors, read_meta),
            encoding='utf-8'
        )

    xml = build_xml(items, public_base)
    (site_dir / f'{FEED_NAME}.xml').write_bytes(xml)
    print(f'built {FEED_NAME} with {len(items)} items')
