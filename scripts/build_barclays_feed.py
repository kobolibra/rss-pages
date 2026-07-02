import html
import re
import sys
from datetime import datetime, timezone
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement, register_namespace

import requests

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
register_namespace("content", CONTENT_NS)

URL = "https://www.ib.barclays/our-insights/weekly-insights.html"
TITLE = "Barclays Weekly Insights"
DESCRIPTION = "Barclays Investment Bank analyses key macroeconomic developments and prepares readers for related data and events in the week ahead."

ITEM_STYLE = """    body { font-family: Georgia, "Times New Roman", serif; color:#111; background:#fff; max-width:820px; margin:40px auto; padding:0 18px; line-height:1.7; }
    article { width:100%; }
    h1 { line-height:1.2; margin:0 0 10px; font-size:2rem; }
    h2 { margin-top:1.8rem; line-height:1.25; }
    h3 { margin-top:1.4rem; line-height:1.3; }
    h4 { margin-top:1.2rem; line-height:1.3; }
    p { margin:0 0 1.1rem; }
    ul { padding-left:1.35rem; margin:1.1rem 0; }
    li { margin:0 0 .75rem; }
    a { color:#0b57d0; }
    .source { color:#666; font-size:.95rem; margin:0 0 24px; }"""


def fetch_page() -> str:
    r = requests.get("https://r.jina.ai/http://www.ib.barclays/our-insights/weekly-insights.html", timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def render_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^)\s]+)[^)]*\)',
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    return text


def render_list_item(item: str) -> str:
    item = re.sub(r'\s+', ' ', item).strip()
    m = re.match(r'^\*\*(.+?)\*\*\s*:\s*(.*)$', item)
    if m:
        label = html.escape(m.group(1).strip())
        rest = render_inline(m.group(2).strip())
        return f'<strong>{label}:</strong> {rest}'.strip()
    m = re.match(r'^\*\*(.+?:)\*\*\s*(.*)$', item)
    if m:
        label = html.escape(m.group(1).strip())
        rest = render_inline(m.group(2).strip())
        return f'<strong>{label}</strong> {rest}'.strip()
    m = re.match(r'^\*\*(.+?)\*\*\s*(.*)$', item)
    if m:
        label = html.escape(m.group(1).strip())
        rest = render_inline(m.group(2).strip())
        return f'<strong>{label}</strong> {rest}'.strip()
    return render_inline(item)


def extract_latest(markdown_text: str):
    text = markdown_text.replace('\r', '')
    if 'Markdown Content:' in text:
        text = text.split('Markdown Content:', 1)[1]

    # The page includes noisy site chrome (notably `## Search`) before the real weekly article.
    # We anchor on `### Get the latest report`, then walk backwards to the nearest valid
    # `## ...` heading before it; on the current page shape this yields the true weekly title.
    report_marker = re.search(r'(?m)^###\s+Get the latest report\s*$', text)
    if not report_marker:
        raise RuntimeError("Could not find Barclays report marker")

    prefix = text[:report_marker.start()]
    heading_matches = list(re.finditer(r'(?m)^##\s+(.+?)\s*$', prefix))
    if not heading_matches:
        raise RuntimeError("Could not parse Barclays weekly insights title")

    bad_titles = {'search', 'share this page', 'weekly insights | barclays investment bank'}
    title_match = None
    for m in reversed(heading_matches):
        candidate = re.sub(r'\s+', ' ', m.group(1)).strip()
        if candidate.lower() not in bad_titles:
            title_match = m
            break
    if not title_match:
        raise RuntimeError("Could not find valid Barclays article title before report marker")

    title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
    body = text[title_match.end():report_marker.start()].strip()

    html_parts: list[str] = []
    list_buffer: list[str] = []
    para_buffer: list[str] = []
    desc = ''

    def flush_list():
        if list_buffer:
            html_parts.append('<ul>' + ''.join(f'<li>{li}</li>' for li in list_buffer) + '</ul>')
            list_buffer.clear()

    def flush_para():
        nonlocal desc
        if para_buffer:
            paragraph = re.sub(r'\s+', ' ', ' '.join(para_buffer)).strip().replace('&nbsp;', ' ')
            if paragraph:
                html_parts.append(f'<p>{render_inline(paragraph)}</p>')
                if not desc:
                    desc = re.sub(r'\*\*(.+?)\*\*', r'\1', paragraph)
            para_buffer.clear()

    for raw in body.split('\n'):
        line = raw.strip().replace('&nbsp;', ' ')
        if not line:
            flush_para()
            flush_list()
            continue
        if re.fullmatch(r'Parsys\s+\d+', line):
            continue
        hm = re.match(r'^(#{1,6})\s+(.*)$', line)
        if hm:
            flush_para()
            flush_list()
            level = min(len(hm.group(1)) + 1, 6)
            html_parts.append(f'<h{level}>{render_inline(hm.group(2).strip())}</h{level}>')
            continue
        lm = re.match(r'^[\*\-]\s+(.*)$', line)
        if lm:
            flush_para()
            list_buffer.append(render_list_item(lm.group(1)))
            continue
        para_buffer.append(line)

    flush_para()
    flush_list()

    if not html_parts:
        raise RuntimeError("Could not parse Barclays weekly insights body")

    if not desc:
        desc = title

    content_html = ''.join(html_parts)
    return title, desc, content_html


def build_xml(title: str, desc: str, content_html: str, public_base: str) -> bytes:
    rss = Element('rss', version='2.0')
    channel = SubElement(rss, 'channel')
    SubElement(channel, 'title').text = TITLE
    SubElement(channel, 'link').text = f"{public_base.rstrip('/')}/barclays_weekly_insights.xml"
    SubElement(channel, 'description').text = DESCRIPTION
    SubElement(channel, 'lastBuildDate').text = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(channel, 'generator').text = 'GitHub Pages RSS rewrite'

    slug = re.sub(r'[^a-zA-Z0-9]+', '-', title).strip('-').lower() or 'barclays-weekly'
    item_url = f"{public_base.rstrip('/')}/item/barclays_weekly_insights/{slug}/"

    item = SubElement(channel, 'item')
    SubElement(item, 'title').text = title
    SubElement(item, 'link').text = item_url
    guid = SubElement(item, 'guid')
    guid.set('isPermaLink', 'true')
    guid.text = item_url
    SubElement(item, 'pubDate').text = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(item, 'description').text = desc

    return minidom.parseString(ET.tostring(rss, encoding='utf-8')).toprettyxml(indent='  ', encoding='utf-8')


def build_item_page(title: str, content_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
{ITEM_STYLE}
  </style>
</head>
<body>
  <article>
    <h1>{html.escape(title)}</h1>
    <p class="source">原文链接：<a href="{html.escape(URL)}" target="_blank" rel="noopener">{html.escape(URL)}</a></p>
    {content_html}
  </article>
</body>
</html>
"""


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python build_barclays_feed.py <site_dir> <public_base>')
        sys.exit(1)
    from pathlib import Path
    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    site_dir.mkdir(parents=True, exist_ok=True)

    page = fetch_page()
    title, desc, content_html = extract_latest(page)
    xml = build_xml(title, desc, content_html, public_base)
    (site_dir / 'barclays_weekly_insights.xml').write_bytes(xml)

    slug = re.sub(r'[^a-zA-Z0-9]+', '-', title).strip('-').lower() or 'barclays-weekly'
    item_dir = site_dir / 'item' / 'barclays_weekly_insights' / slug
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / 'index.html').write_text(build_item_page(title, content_html), encoding='utf-8')
    print('built barclays feed')
