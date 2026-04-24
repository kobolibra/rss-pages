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
BASE = None
if len(sys.argv) > 2:
    arg = sys.argv[2]
    p = Path(arg).expanduser()
    if p.exists() and p.is_dir():
        BASE = DEFAULT_BASE
    elif arg.startswith('/'):
        # absolute path passed by mistake; treat as base repo path fallback
        BASE = DEFAULT_BASE
    else:
        BASE = arg.rstrip('/')
else:
    BASE = DEFAULT_BASE


def strip_html(s: str) -> str:
    s = s or ''
    s = html.unescape(s)
    s = re.sub(r'<[^>]+>', ' ', s)
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
    if not parsed.scheme:
        if not link.startswith(expected_suffix):
            fail(f'{feed_name} item link not localized: {link}')
        return

    if BASE and parsed.netloc != base_parsed.netloc:
        fail(f'{feed_name} item link host mismatch: {link}')

    base_path = (base_parsed.path.rstrip('/') if base_parsed else '')
    if not (parsed.path.startswith(base_path + expected_suffix) or parsed.path.startswith(expected_suffix)):
        fail(f'{feed_name} item link not localized: {link}')


def resolve_local_item_path(link: str) -> Path:
    item_path = urlparse(link).path
    if BASE:
        base_path = urlparse(BASE).path.rstrip('/')
        if base_path and item_path.startswith(base_path):
            item_path = item_path[len(base_path):]
    return SITE / item_path.lstrip('/') / 'index.html' if not item_path.endswith('index.html') else SITE / item_path.lstrip('/')


def validate_blackrock_local_page(local_path: Path):
    if not local_path.exists():
        fail(f'BlackRock local item page missing: {local_path}')

    page_html = local_path.read_text(encoding='utf-8')
    page_text = strip_html(page_html).lower()
    if len(page_text) < 500:
        fail(f'BlackRock local item page unexpectedly short: {len(page_text)} chars')
    for needle in ['our bottom line', 'market backdrop', 'week ahead']:
        if needle not in page_text:
            fail(f'BlackRock local item page missing body marker: {needle}')


def extract_natural_source_link(page_html: str) -> str:
    # 页面里保留“来源：xxx”文本行，提取以便做源站映射校验
    m = re.search(r'来源：([^<\s]+)', page_html)
    return (m.group(1).strip() if m else '').strip()


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
validate_blackrock_local_page(resolve_local_item_path(blackrock_link))

# Natixis: 改为原文链接策略，避免本地化 link 与 content:encoded 嵌入正文
natixis = read_first_item('natixis_insights')
natixis_link = (natixis.findtext('link') or '').strip()
natixis_desc = (natixis.findtext('description') or '').strip()
natixis_desc_text = strip_html(natixis_desc)
natixis_content_html = natixis.findtext(CONTENT) or ''
if not natixis_desc_text:
    fail('Natixis first item missing description text')
if not natixis_link.startswith('https://www.im.natixis.com/en-us/insights/'):
    fail(f'Natixis item link should stay on source domain: {natixis_link}')
if strip_html(natixis_content_html):
    fail('Natixis item should not embed content:encoded anymore; reader should follow original link')
if natixis_content_html:
    print('Natixis content:encoded length:', len(natixis_content_html.strip()))

# 非强制要求 natixis 本地 item 页面；仅做轻量兜底：若本地页存在，需含对应源站链接文本
natixis_local_path = resolve_local_item_path(natixis_link)
if natixis_local_path.exists():
    natixis_page_html = natixis_local_path.read_text(encoding='utf-8')
    source_link = extract_natural_source_link(natixis_page_html)
    if source_link and not source_link.startswith('https://www.im.natixis.com/en-us/insights/'):
        fail(f'Natixis local item source link mismatch: {source_link}')

# Trivium: boilerplate footer stripped
trivium = read_first_item('trivium_finance_regs')
trivium_text = (trivium.findtext('description') or '') + ' ' + (trivium.findtext(CONTENT) or '')
if 'appeared first on' in trivium_text.lower():
    fail('Trivium still contains boilerplate footer')

print('VALIDATION OK')
