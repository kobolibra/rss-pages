#!/usr/bin/env python3
import html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

SITE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('site')
CONTENT = '{http://purl.org/rss/1.0/modules/content/}encoded'
DEFAULT_BASE = 'https://kobolibra.github.io/rss-pages'
BASE = sys.argv[2].rstrip('/') if len(sys.argv) > 2 else DEFAULT_BASE


def strip_html(s: str) -> str:
    s = s or ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def fail(msg: str):
    print(f'VALIDATION FAILED: {msg}')
    sys.exit(1)


def read_first_item(feed_name: str):
    root = ET.parse(SITE / f'{feed_name}.xml').getroot()
    channel = root.find('channel')
    if channel is None:
        fail(f'{feed_name} missing channel')
    items = channel.findall('item')
    if not items:
        fail(f'{feed_name} has no items')
    return items[0]


def assert_localized_link(feed_name: str, link: str):
    expected_suffix = f'/item/{feed_name}/'
    parsed = urlparse(link)
    base_parsed = urlparse(BASE) if BASE else None
    if parsed.scheme and parsed.netloc:
        if BASE and parsed.netloc != base_parsed.netloc:
            fail(f'{feed_name} item link host mismatch: {link}')
        base_path = (base_parsed.path.rstrip('/') if base_parsed else '')
        if not (parsed.path.startswith(base_path + expected_suffix) or parsed.path.startswith(expected_suffix)):
            fail(f'{feed_name} item link not localized: {link}')
    else:
        if not link.startswith(BASE + expected_suffix) and not link.startswith(expected_suffix):
            fail(f'{feed_name} item link not localized: {link}')


# Barclays: local item page link present; 正文应通过 item page 获取，不再在 feed 内重复塞 content:encoded
barclays = read_first_item('barclays_weekly_insights')
barclays_link = (barclays.findtext('link') or '').strip()
barclays_content = strip_html(barclays.findtext(CONTENT) or '')
assert_localized_link('barclays_weekly_insights', barclays_link)
if barclays_content:
    fail(f'Barclays should not embed feed body anymore: {len(barclays_content)} chars present')

# BlackRock: local item page link present; 正文应通过 item page 获取，不再在 feed 内重复塞 content:encoded
blackrock = read_first_item('blackrock_weekly_commentary')
blackrock_link = (blackrock.findtext('link') or '').strip()
blackrock_content_html = blackrock.findtext(CONTENT) or ''
blackrock_content = strip_html(blackrock_content_html)
assert_localized_link('blackrock_weekly_commentary', blackrock_link)
if blackrock_content:
    fail(f'BlackRock should not embed feed body anymore: {len(blackrock_content)} chars present')

# Trivium: boilerplate footer stripped
trivium = read_first_item('trivium_finance_regs')
trivium_text = (trivium.findtext('description') or '') + ' ' + (trivium.findtext(CONTENT) or '')
if 'appeared first on' in trivium_text.lower():
    fail('Trivium still contains boilerplate footer')

print('VALIDATION OK')
