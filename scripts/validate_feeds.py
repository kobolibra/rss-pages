#!/usr/bin/env python3
import re
import sys
import html
from pathlib import Path
from xml.etree import ElementTree as ET

SITE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('site')
CONTENT = '{http://purl.org/rss/1.0/modules/content/}encoded'
BASE = 'https://kobolibra.github.io/rss-pages'


def strip_html(s: str) -> str:
    s = s or ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def fail(msg: str):
    print(f'VALIDATION FAILED: {msg}')
    sys.exit(1)

# Barclays: local item page link present;正文应通过 item page 获取，不再在 feed 内重复塞 content:encoded
barclays = ET.parse(SITE / 'barclays_weekly_insights.xml').getroot().find('channel').findall('item')[0]
barclays_link = (barclays.findtext('link') or '').strip()
barclays_content = strip_html(barclays.findtext(CONTENT) or '')
if not barclays_link.startswith(BASE + '/item/barclays_weekly_insights/'):
    fail(f'Barclays item link not localized: {barclays_link}')
if barclays_content:
    fail(f'Barclays should not embed feed body anymore: {len(barclays_content)} chars present')

# BlackRock: local item page link present;正文应通过 item page 获取，不再在 feed 内重复塞 content:encoded
blackrock = ET.parse(SITE / 'blackrock_weekly_commentary.xml').getroot().find('channel').findall('item')[0]
blackrock_link = (blackrock.findtext('link') or '').strip()
blackrock_content_html = blackrock.findtext(CONTENT) or ''
blackrock_content = strip_html(blackrock_content_html)
if not blackrock_link.startswith(BASE + '/item/blackrock_weekly_commentary/'):
    fail(f'BlackRock item link not localized: {blackrock_link}')
if blackrock_content:
    fail(f'BlackRock should not embed feed body anymore: {len(blackrock_content)} chars present')

# Trivium: boilerplate footer stripped
trivium = ET.parse(SITE / 'trivium_finance_regs.xml').getroot().find('channel').findall('item')[0]
trivium_text = (trivium.findtext('description') or '') + ' ' + (trivium.findtext(CONTENT) or '')
if 'appeared first on' in trivium_text.lower():
    fail('Trivium still contains boilerplate footer')

print('VALIDATION OK')
