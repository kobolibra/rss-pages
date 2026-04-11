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
FEED_NAME = "gsam"
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


def clean_html_fragment(fragment) -> str:
    if fragment is None:
        return ""
    if not isinstance(fragment, str):
        return ""
    return fragment.replace("\r", "").strip()


def asset_url(path: str) -> str:
    return urljoin(SITE_BASE, path or "") if path else ""


def render_image_html(image: dict, fallback_title: str = "") -> str:
    if not isinstance(image, dict):
        return ""

    file_ref = image.get("fileReference") if isinstance(image.get("fileReference"), dict) else image
    path = file_ref.get("path") if isinstance(file_ref, dict) else image.get("path")
    src = asset_url(path or "")
    if not src:
        return ""

    alt = image.get("alt") or image.get("title") or fallback_title or ""
    title = image.get("title") or ""
    source = clean_html_fragment(image.get("source", ""))

    caption_parts = []
    if title:
        caption_parts.append(f"<strong>{html.escape(title)}</strong>")
    if source:
        caption_parts.append(source)
    figcaption = ""
    if caption_parts:
        figcaption = f"<figcaption>{'<br/>'.join(caption_parts)}</figcaption>"

    return (
        f"<figure><img src=\"{html.escape(src)}\" alt=\"{html.escape(alt)}\" loading=\"lazy\" />"
        f"{figcaption}</figure>"
    )


def normalize_authors(raw_authors) -> list[dict]:
    if isinstance(raw_authors, dict):
        return [raw_authors]
    if isinstance(raw_authors, list):
        return [x for x in raw_authors if isinstance(x, dict)]
    return []


def author_name(author: dict) -> str:
    if not isinstance(author, dict):
        return ""
    person_ref = author.get("personReferencePath") if isinstance(author.get("personReferencePath"), dict) else {}
    meta = person_ref.get("metadata") if isinstance(person_ref.get("metadata"), dict) else {}
    raw_title = meta.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()
    first_name = meta.get("firstName") or ""
    last_name = meta.get("lastName") or ""
    if first_name or last_name:
        return f"{first_name} {last_name}".strip()
    fallback_title = author.get("title") if isinstance(author.get("title"), str) else ""
    fallback_name = author.get("name") if isinstance(author.get("name"), str) else ""
    return fallback_title or fallback_name or ""


def author_job(author: dict) -> str:
    if not isinstance(author, dict):
        return ""
    person_ref = author.get("personReferencePath") if isinstance(author.get("personReferencePath"), dict) else {}
    meta = person_ref.get("metadata") if isinstance(person_ref.get("metadata"), dict) else {}
    return meta.get("jobTitle") if isinstance(meta.get("jobTitle"), str) else ""


def extract_article_data(page_json: dict) -> dict:
    page_props = ((page_json or {}).get("props") or {}).get("pageProps") or {}
    data = page_props.get("data") or {}
    props = data.get("properties") or {}
    items = data.get("items") or {}

    summary = props.get("summaryDescription") or props.get("summaryTeaserText") or props.get("socialDescription") or ""
    key_takeaways = [x for x in (props.get("keyTakeaways") or []) if isinstance(x, dict)]
    authors = normalize_authors(props.get("authorDetails") or [])
    display_date = props.get("summaryDisplaydate") or ""
    read_time = props.get("readTime") or ""

    hero = props.get("heroImageLarge") or props.get("heroImage") or props.get("heroImageBeyondLarge") or props.get("heroImageSmall") or {}
    hero_image_html = ""
    if isinstance(hero, dict) and hero.get("path"):
        hero_image_html = render_image_html({"path": hero.get("path"), "alt": props.get("title") or props.get("summaryTitle") or ""})

    body_parts: list[str] = []

    if isinstance(items, dict):
        for key, value in items.items():
            if not isinstance(value, dict):
                continue

            vtype = value.get("type") or ""
            component_name = str(vtype).split("/")[-1]
            title = (value.get("title") or "").strip()

            if value.get("images") and isinstance(value.get("images"), list):
                for img in value.get("images") or []:
                    img_html = render_image_html(img, fallback_title=title)
                    if img_html:
                        body_parts.append(img_html)
                rich = clean_html_fragment(value.get("richText", ""))
                if rich:
                    body_parts.append(rich)
                continue

            if component_name == "text" and value.get("text"):
                if title:
                    body_parts.append(f"<h2>{html.escape(title)}</h2>")
                body_parts.append(clean_html_fragment(value.get("text", "")))
                continue

            if component_name == "paragraphlist":
                if title:
                    body_parts.append(f"<h2>{html.escape(title)}</h2>")
                rich = clean_html_fragment(value.get("richText", ""))
                if rich:
                    body_parts.append(rich)
                plist = value.get("paragraphList") or []
                for entry in plist:
                    if not isinstance(entry, dict):
                        continue
                    ptitle = (entry.get("paragraphTitle") or entry.get("title") or "").strip()
                    ptext = clean_html_fragment(entry.get("paragraphText", ""))
                    pimg_html = ""
                    if isinstance(entry.get("fileReference"), dict):
                        pimg_html = render_image_html({
                            "path": entry["fileReference"].get("path"),
                            "alt": ptitle,
                            "title": ptitle,
                        }, fallback_title=ptitle)
                    body_parts.append('<div class="paragraph-card">')
                    if pimg_html:
                        body_parts.append(pimg_html)
                    if ptitle:
                        body_parts.append(f"<h3>{html.escape(ptitle)}</h3>")
                    if ptext:
                        body_parts.append(ptext)
                    body_parts.append('</div>')
                continue

            if component_name == "quote" and value.get("text"):
                body_parts.append(f"<blockquote>{clean_html_fragment(value.get('text', ''))}</blockquote>")
                continue

            if component_name == "horizontaltab":
                if title:
                    body_parts.append(f"<h2>{html.escape(title)}</h2>")
                intro = clean_html_fragment(value.get("introduction", ""))
                rich = clean_html_fragment(value.get("richText", ""))
                if intro:
                    body_parts.append(intro)
                if rich:
                    body_parts.append(rich)
                for tab in value.get("items") or []:
                    if not isinstance(tab, dict):
                        continue
                    tab_title = (tab.get("title") or "").strip()
                    tab_intro = clean_html_fragment(tab.get("introduction", ""))
                    tab_rich = clean_html_fragment(tab.get("richText", ""))
                    if tab_title:
                        body_parts.append(f"<h3>{html.escape(tab_title)}</h3>")
                    if tab_intro:
                        body_parts.append(tab_intro)
                    if tab_rich:
                        body_parts.append(tab_rich)
                continue

    article_html = "\n".join(part for part in body_parts if part.strip())
    return {
        "summary": summary,
        "key_takeaways": key_takeaways,
        "authors": authors,
        "display_date": display_date,
        "read_time": str(read_time).strip(),
        "hero_image_html": hero_image_html,
        "article_html": article_html,
    }


def build_item_page(item: dict, page_url: str, article: dict) -> str:
    title = item.get("title") or item.get("summaryTitle") or "Item"
    summary = article.get("summary") or ""
    display_date = article.get("display_date") or ""
    read_time = article.get("read_time") or ""
    hero_image_html = article.get("hero_image_html") or ""
    article_html = article.get("article_html") or ""
    key_takeaways = article.get("key_takeaways") or []
    authors = article.get("authors") or []

    meta_line_parts = []
    if display_date:
        meta_line_parts.append(html.escape(display_date))
    if read_time:
        meta_line_parts.append(f"{html.escape(read_time)} min read")
    meta_line = " · ".join(meta_line_parts)

    author_lines = []
    for author in authors:
        name = author_name(author)
        job = author_job(author)
        if name and job:
            author_lines.append(f"<div>{html.escape(name)} — {html.escape(job)}</div>")
        elif name:
            author_lines.append(f"<div>{html.escape(name)}</div>")

    takeaways_html = ""
    if key_takeaways:
        bits = ["<section><h2>Key Takeaways</h2><ul>"]
        for kt in key_takeaways[:10]:
            kt_title = (kt or {}).get("title", "")
            kt_desc = (kt or {}).get("description", "")
            kt_desc_clean = html.escape(re.sub(r"\s+", " ", kt_desc).strip()) if kt_desc else ""
            if kt_title and kt_desc_clean:
                bits.append(f"<li><strong>{html.escape(kt_title.strip())}</strong> — {kt_desc_clean}</li>")
            elif kt_title:
                bits.append(f"<li><strong>{html.escape(kt_title.strip())}</strong></li>")
            elif kt_desc_clean:
                bits.append(f"<li>{kt_desc_clean}</li>")
        bits.append("</ul></section>")
        takeaways_html = "".join(bits)

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(title)}</title>
  <link rel=\"canonical\" href=\"{html.escape(page_url)}\">
  <style>
    body {{ font-family: Georgia, \"Times New Roman\", serif; color:#111; background:#fff; max-width:820px; margin:40px auto; padding:0 18px; line-height:1.7; }}
    article {{ width:100%; }}
    h1 {{ line-height:1.15; margin:0 0 14px; font-size:2.2rem; }}
    h2 {{ margin-top:2rem; line-height:1.25; }}
    h3 {{ margin-top:1.5rem; line-height:1.25; }}
    .meta {{ color:#666; font-size:.95rem; margin:0 0 10px; }}
    .authors {{ color:#333; font-size:1rem; margin:0 0 20px; }}
    .summary {{ font-size:1.12rem; margin:1.1rem 0 1.4rem; }}
    img {{ max-width:100%; height:auto; display:block; }}
    figure {{ margin:1.5rem 0; }}
    figcaption {{ color:#555; font-size:.92rem; margin-top:.55rem; }}
    blockquote {{ border-left:4px solid #ddd; margin:1.4rem 0; padding:.2rem 0 .2rem 1rem; color:#333; }}
    ul {{ padding-left:1.3rem; }}
    .paragraph-card {{ margin:1.2rem 0 1.8rem; }}
    .paragraph-card figure {{ margin:.6rem 0; max-width:72px; }}
    .paragraph-card h3 {{ margin:.4rem 0 .45rem; font-size:1.08rem; }}
    a {{ color:#0b57d0; }}
  </style>
</head>
<body>
  <article>
    <h1>{html.escape(title)}</h1>
    {f'<div class="meta">{meta_line}</div>' if meta_line else ''}
    {f'<div class="authors">{"".join(author_lines)}</div>' if author_lines else ''}
    {hero_image_html}
    {f'<div class="summary">{html.escape(summary)}</div>' if summary else ''}
    {takeaways_html}
    {article_html}
  </article>
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
        article = extract_article_data(page_json)
        item_dir = site_dir / 'item' / FEED_NAME / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / 'index.html').write_text(build_item_page(hit, page_url, article), encoding='utf-8')

    xml = build_xml(items, public_base)
    (site_dir / f'{FEED_NAME}.xml').write_bytes(xml)
    print(f'built {FEED_NAME} with {len(items)} items')
