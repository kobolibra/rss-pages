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
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def extract_latest(html_text: str):
    m = re.search(r'Weekly Insights\s*\n\s*27\s+Mar\s+2026.*?<h2>\s*(.*?)\s*</h2>\s*<p><span class="intro">(.*?)</span></p>(.*?)(?:<div class="clear"></div>|</div>\s*<div class="whitespace20">)', html_text, re.S | re.I)
    if not m:
        m = re.search(r'<h2>\s*Extend\.\.\. and hope for an end\s*</h2>\s*<p><span class="intro">(.*?)</span></p>(.*?)(?:<div class="clear"></div>|</div>\s*<div class="whitespace20">)', html_text, re.S | re.I)
        if not m:
            raise RuntimeError("Could not parse Barclays weekly insights page")
        title = 'Extend... and hope for an end'
        intro = m.group(1).strip()
        body = m.group(2)
    else:
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        intro = m.group(2).strip()
        body = m.group(3)

    body = re.sub(r'\s+', ' ', body)
    body = body.replace('&nbsp;', ' ')
    # keep simple allowed markup only
    html_block = f'<p>{html.escape(intro)}</p>'

    list_items = re.findall(r'<li>(.*?)</li>', body, re.S | re.I)
    if list_items:
        html_block += '<ul>'
        for li in list_items:
            clean = li.replace('<br>', ' ').replace('<br/>', ' ').replace('<br />', ' ')
            html_block += f'<li>{clean}</li>'
        html_block += '</ul>'

    desc = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', intro))).strip()
    return title, desc, html_block


def build_xml(title: str, desc: str, content_html: str, public_base: str) -> bytes:
    rss = Element('rss', version='2.0')
    channel = SubElement(rss, 'channel')
    SubElement(channel, 'title').text = TITLE
    SubElement(channel, 'link').text = URL
    SubElement(channel, 'description').text = DESCRIPTION
    SubElement(channel, 'lastBuildDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(channel, 'generator').text = 'GitHub Pages RSS rewrite'

    slug = re.sub(r'[^a-zA-Z0-9]+', '-', title).strip('-').lower() or 'barclays-weekly'
    item_url = URL

    item = SubElement(channel, 'item')
    SubElement(item, 'title').text = title
    SubElement(item, 'link').text = item_url
    guid = SubElement(item, 'guid')
    guid.set('isPermaLink', 'false')
    import hashlib
    guid.text = hashlib.md5(f'{item_url}|{title}'.encode('utf-8')).hexdigest()
    SubElement(item, 'pubDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    SubElement(item, 'description').text = content_html

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
