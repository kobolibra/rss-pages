import html
import re
import sys
from datetime import datetime
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement, QName, register_namespace

import requests

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
register_namespace("content", CONTENT_NS)

URL = "https://www.ib.barclays/our-insights/weekly-insights.html"
TITLE = "Barclays Weekly Insights"
DESCRIPTION = "Barclays Investment Bank analyses key macroeconomic developments and prepares readers for related data and events in the week ahead."


def fetch_page() -> str:
    r = requests.get("https://r.jina.ai/http://www.ib.barclays/our-insights/weekly-insights.html", timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def extract_latest(markdown_text: str):
    text = markdown_text.replace('\r', '')

    m = re.search(
        r'##\s+(.+?)\n\n(.+?)(?:\n\n###\s+Get the latest report|\n\n\*\s+\[Global Economics Weekly:)',
        text,
        re.S,
    )
    if not m:
        raise RuntimeError("Could not parse Barclays weekly insights page")

    title = re.sub(r'\s+', ' ', m.group(1)).strip()
    body = re.sub(r'\s+', ' ', m.group(2)).strip()
    body = body.replace('&nbsp;', ' ')

    paragraphs = [p.strip() for p in re.split(r'\n\n+', m.group(2)) if p.strip()]
    intro = re.sub(r'\s+', ' ', paragraphs[0]).strip() if paragraphs else body
    clean_paragraphs = [re.sub(r'\s+', ' ', p).strip() for p in paragraphs[:6]]
    html_block = ''.join(f'<p>{html.escape(p)}</p>' for p in clean_paragraphs)

    desc = intro
    return title, desc, html_block


def build_xml(title: str, desc: str, content_html: str, public_base: str) -> bytes:
    rss = Element('rss', version='2.0')
    channel = SubElement(rss, 'channel')
    SubElement(channel, 'title').text = TITLE
    SubElement(channel, 'link').text = URL
    SubElement(channel, 'description').text = DESCRIPTION
    SubElement(channel, 'lastBuildDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(channel, 'generator').text = 'GitHub Pages RSS rewrite'

    item_url = URL

    item = SubElement(channel, 'item')
    SubElement(item, 'title').text = title
    SubElement(item, 'link').text = item_url
    guid = SubElement(item, 'guid')
    guid.set('isPermaLink', 'true')
    guid.text = item_url
    SubElement(item, 'pubDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(item, 'description').text = desc
    content = SubElement(item, QName(CONTENT_NS, 'encoded'))
    content.text = content_html

    return minidom.parseString(ET.tostring(rss, encoding='utf-8')).toprettyxml(indent='  ', encoding='utf-8')


def build_item_page(title: str, content_html: str) -> str:
    return f"""<!doctype html>
<html>
<meta charset=\"utf-8\">
<head><title>{html.escape(title)}</title></head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>来源：{html.escape(URL)}</p>
  <div>{content_html}</div>
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
