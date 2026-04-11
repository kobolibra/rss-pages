import csv
import html
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://www.dws.com/en-us/insights/cio-view/cio-archive/?view=List&Sort=-Date%2c-Title"
SITE_BASE = "https://www.dws.com"
FEED_NAME = "dws_cio"
FEED_TITLE = "DWS CIO View"
FEED_DESC = "DWS CIO archive rebuilt from source pages with local full-text item pages."
UA = "Mozilla/5.0"


def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def list_items() -> list[dict]:
    html_text = fetch(LIST_URL)
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    seen = set()
    for a in soup.select('a.teaser[href^="/en-us/insights/"]'):
        href = a.get("href", "")
        url = urljoin(SITE_BASE, href)
        if url in seen:
            continue
        seen.add(url)

        title_el = a.select_one('.teaser__title-cover')
        desc_el = a.select_one('.teaser__copy-cover')
        date_el = a.select_one('.label-grey')
        type_el = a.select_one('.label-dark')
        detail_el = a.select_one('.teaser__detail')
        author_el = a.select_one('.author')
        img_el = a.select_one('picture img')
        img_src = ""
        if img_el:
            img_src = img_el.get('data-src') or img_el.get('src') or ''
            if img_src.startswith('/'):
                img_src = urljoin(SITE_BASE, img_src)

        title = clean_text(title_el.get_text(" ", strip=True) if title_el else "")
        desc = clean_text(desc_el.get_text(" ", strip=True) if desc_el else "")
        date = clean_text(date_el.get_text(" ", strip=True) if date_el else "")
        item_type = clean_text(type_el.get_text(" ", strip=True) if type_el else "")
        detail = clean_text(detail_el.get_text(" ", strip=True) if detail_el else "")
        author = clean_text(author_el.get_text(" ", strip=True).replace('By:', '') if author_el else "")

        if not title:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "description": desc,
                "date": date,
                "item_type": item_type,
                "detail": detail,
                "author": author,
                "image": img_src,
            }
        )
    return items


def decode_vue_content(raw: str) -> str:
    s = html.unescape(raw)
    s = s.encode('utf-8').decode('unicode_escape')
    s = html.unescape(s)
    s = s.replace('\\n', '\n')
    s = s.replace('\\/', '/')
    s = re.sub(r'<PageFootnoteReference[^>]*title="([^"]+)"[^>]*>.*?</PageFootnoteReference>', lambda m: f'<sup>[Footnote: {html.escape(html.unescape(m.group(1)))}]</sup>', s)
    s = re.sub(r'<vue-glossary-item[^>]*>(.*?)</vue-glossary-item>', r'\1', s)
    s = re.sub(r'</?vue-[^>]+>', '', s)
    s = s.replace('\u200B', '').replace('\u0000', '').replace('\u001f', '')
    return s


def extract_chart_models_from_page(page: str) -> list[dict]:
    decoded_page = html.unescape(page)
    charts = []
    marker = 'vue-everviz-charts :model=\\u0022'
    pos = 0
    while True:
        idx = decoded_page.find(marker, pos)
        if idx == -1:
            break
        raw = decoded_page[idx + len(marker):]
        try:
            s = raw.encode('utf-8').decode('unicode_escape')
            s = html.unescape(s)
        except Exception:
            pos = idx + len(marker)
            continue
        start = s.find('{')
        if start == -1:
            pos = idx + len(marker)
            continue
        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(s[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end is not None:
            try:
                charts.append(json.loads(s[start:end]))
            except Exception:
                pass
        pos = idx + len(marker)
    return charts


def render_chart_tables(page: str) -> str:
    charts = extract_chart_models_from_page(page)
    parts = []
    for chart in charts:
        data = chart.get('data', {})
        csv_text = (((data.get('data') or {}).get('csv')) or '').strip()
        if not csv_text:
            continue
        title = clean_text(BeautifulSoup((data.get('title') or {}).get('text') or '', 'html.parser').get_text(' ', strip=True))
        credits = clean_text((data.get('credits') or {}).get('text') or '')
        y_axes = data.get('yAxis') or []
        axis_labels = []
        if isinstance(y_axes, list):
            for axis in y_axes[:3]:
                label = clean_text(BeautifulSoup(((axis or {}).get('title') or {}).get('text') or '', 'html.parser').get_text(' ', strip=True))
                if label:
                    axis_labels.append(label)
        rows = list(csv.reader(io.StringIO(csv_text), delimiter='\t'))
        if not rows:
            continue
        header = rows[0]
        data_rows = rows[1:]
        parts.append('<section class="chart-table">')
        if title:
            parts.append(f'<h2>{html.escape(title)}</h2>')
        parts.append('<div class="chart-fallback-note">Chart data table</div>')
        if axis_labels:
            parts.append(f'<div class="chart-meta">Axes: {html.escape(" / ".join(axis_labels))}</div>')
        parts.append('<table><thead><tr>')
        for col in header:
            parts.append(f'<th>{html.escape(col)}</th>')
        parts.append('</tr></thead><tbody>')
        for row in data_rows:
            parts.append('<tr>')
            for col in row:
                parts.append(f'<td>{html.escape(col)}</td>')
            parts.append('</tr>')
        parts.append('</tbody></table>')
        if credits:
            parts.append(f'<div class="chart-meta">Source: {html.escape(credits)}</div>')
        parts.append('</section>')
    return ''.join(parts)


def detail_content(url: str) -> dict:
    page = fetch(url)

    header_match = re.search(r'<vue-article-page-layout :model="([\s\S]*?)">', page)
    header = {}
    if header_match:
        header = json.loads(html.unescape(header_match.group(1)))

    blocks = re.findall(r'Blocks\.Html\.Blocks\.HtmlBlock[\s\S]*?&quot;content&quot;:&quot;([\s\S]*?)&quot;,&quot;productBuyingProcessPageProps&quot;', page)
    html_blocks = [decode_vue_content(b) for b in blocks]
    body_html = "\n".join([b for b in html_blocks if b.strip()])
    chart_tables_html = render_chart_tables(page)
    if chart_tables_html:
        body_html += "\n" + chart_tables_html

    article_header = header.get('articleHeaderProps', {}) if isinstance(header, dict) else {}
    intro = decode_vue_content(article_header.get('introText', '') or '')
    hero = article_header.get('image') or {}
    hero_src = hero.get('src') or ''
    if hero_src.startswith('/'):
        hero_src = urljoin(SITE_BASE, hero_src)
    hero_alt = hero.get('alt') or article_header.get('headline') or ''

    return {
        'headline': article_header.get('headline') or '',
        'date': article_header.get('date') or '',
        'intro_html': intro,
        'hero_src': hero_src,
        'hero_alt': hero_alt,
        'body_html': body_html,
    }


def build_item_page(item: dict, detail: dict) -> str:
    title = detail.get('headline') or item['title']
    date = detail.get('date') or item.get('date') or ''
    author = item.get('author') or ''
    detail_line = item.get('detail') or ''
    hero_src = detail.get('hero_src') or item.get('image') or ''
    hero_alt = detail.get('hero_alt') or title
    intro_html = detail.get('intro_html') or ''
    body_html = detail.get('body_html') or ''

    meta_parts = [x for x in [date, author, detail_line] if x]
    meta_line = ' · '.join(meta_parts)
    hero_html = f'<figure><img src="{html.escape(hero_src)}" alt="{html.escape(hero_alt)}" loading="lazy"></figure>' if hero_src else ''

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(title)}</title>
  <link rel=\"canonical\" href=\"{html.escape(item['url'])}\">
  <style>
    body {{ font-family: Georgia, \"Times New Roman\", serif; color:#111; background:#fff; max-width:900px; margin:40px auto; padding:0 18px; line-height:1.7; }}
    article {{ width:100%; }}
    h1 {{ line-height:1.15; margin:0 0 14px; font-size:2.2rem; }}
    h2 {{ margin-top:2rem; line-height:1.25; }}
    .meta {{ color:#666; font-size:.95rem; margin:0 0 18px; }}
    img {{ max-width:100%; height:auto; display:block; }}
    figure {{ margin:1.4rem 0; }}
    p, ul, ol {{ margin:1rem 0; }}
    sup {{ font-size:.8em; color:#555; }}
    table {{ width:100%; border-collapse:collapse; margin:1rem 0 1.4rem; font-size:.92rem; }}
    th, td {{ border:1px solid #ddd; padding:6px 8px; text-align:right; }}
    th:first-child, td:first-child {{ text-align:left; }}
    .chart-table {{ margin:2rem 0; padding:1rem 0; }}
    .chart-fallback-note, .chart-meta {{ color:#555; font-size:.92rem; margin:.5rem 0; }}
    a {{ color:#0b57d0; }}
  </style>
</head>
<body>
  <article>
    <h1>{html.escape(title)}</h1>
    {f'<div class="meta">{html.escape(meta_line)}</div>' if meta_line else ''}
    {hero_html}
    {intro_html}
    {body_html}
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
        slug = slugify(item['url'].rstrip('/').split('/')[-1])
        local_url = f"{public_base.rstrip('/')}/item/{FEED_NAME}/{slug}/"
        it = SubElement(channel, 'item')
        SubElement(it, 'title').text = item['title']
        SubElement(it, 'link').text = local_url
        guid = SubElement(it, 'guid')
        guid.set('isPermaLink', 'true')
        guid.text = local_url
        SubElement(it, 'pubDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        if item.get('description'):
            SubElement(it, 'description').text = item['description']

    return minidom.parseString(ET.tostring(rss, encoding='utf-8')).toprettyxml(indent='  ', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python build_dws_cio_feed.py <site_dir> <public_base>')
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    site_dir.mkdir(parents=True, exist_ok=True)

    items = list_items()
    selected = []
    for item in items:
        if '/en-us/insights/cio-view/' not in item['url']:
            continue
        selected.append(item)
        detail = detail_content(item['url'])
        slug = slugify(item['url'].rstrip('/').split('/')[-1])
        item_dir = site_dir / 'item' / FEED_NAME / slug
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / 'index.html').write_text(build_item_page(item, detail), encoding='utf-8')

    xml = build_xml(selected, public_base)
    (site_dir / f'{FEED_NAME}.xml').write_bytes(xml)
    print(f'built {FEED_NAME} with {len(selected)} items')
