import html
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LIST_URL = "https://www.dws.com/en-us/insights/archive/"
SITE_BASE = "https://www.dws.com"
FEED_NAME = "dws_cio"
FEED_TITLE = "DWS CIO View"
FEED_DESC = "DWS CIO archive rebuilt from source pages with local full-text item pages."
UA = "Mozilla/5.0"
CHART_TOKEN_PREFIX = "__DWS_CHART_"


def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_bytes(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.content


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def public_url_for_path(path: Path, public_base: str, site_dir: Path) -> str:
    public_path = "/".join(path.relative_to(site_dir).parts)
    return public_base.rstrip("/") + "/" + public_path


def parse_dws_date(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    for fmt in ("%d-%b-%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def format_rss_date(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def load_live_feed_state(public_base: str) -> dict:
    feed_url = f"{public_base.rstrip('/')}/{FEED_NAME}.xml"
    try:
        xml_bytes = fetch_bytes(feed_url)
    except Exception:
        return {"items": {}, "last_build_date": None}

    state = {"items": {}, "last_build_date": None}
    try:
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return state
        last_build = clean_text(channel.findtext("lastBuildDate") or "")
        if last_build:
            try:
                state["last_build_date"] = parsedate_to_datetime(last_build)
            except Exception:
                state["last_build_date"] = None
        for rss_item in channel.findall("item"):
            link = clean_text(rss_item.findtext("link") or "")
            if not link:
                continue
            slug = slugify(link.rstrip("/").split("/")[-1])
            pub_date_text = clean_text(rss_item.findtext("pubDate") or "")
            pub_date = None
            if pub_date_text:
                try:
                    pub_date = parsedate_to_datetime(pub_date_text)
                except Exception:
                    pub_date = None
            state["items"][slug] = {
                "title": clean_text(rss_item.findtext("title") or ""),
                "description": clean_text(rss_item.findtext("description") or ""),
                "link": link,
                "pub_date": pub_date,
            }
    except Exception:
        return {"items": {}, "last_build_date": None}
    return state


def restore_live_item_tree(item_url: str, public_base: str, site_dir: Path) -> bool:
    try:
        item_bytes = fetch_bytes(item_url)
    except Exception:
        return False

    parsed_item = urlparse(item_url)
    base_path = urlparse(public_base).path.rstrip("/")

    def relative_site_path(url: str) -> Path | None:
        parsed = urlparse(url)
        rel_path = parsed.path
        if base_path and rel_path.startswith(base_path + "/"):
            rel_path = rel_path[len(base_path) + 1 :]
        else:
            rel_path = rel_path.lstrip("/")
        if not rel_path:
            return None
        return site_dir / rel_path

    item_path = parsed_item.path.rstrip("/") + "/"
    out_dir = relative_site_path(item_url)
    if out_dir is None:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_bytes(item_bytes)

    soup = BeautifulSoup(item_bytes, "html.parser")
    asset_urls = set()

    for tag in soup.find_all(src=True):
        asset_url = urljoin(item_url, tag.get("src") or "")
        parsed = urlparse(asset_url)
        if parsed.netloc == parsed_item.netloc and parsed.path.startswith(item_path) and not parsed.path.endswith("/"):
            asset_urls.add(asset_url)

    for tag in soup.find_all(srcset=True):
        srcset = tag.get("srcset") or ""
        for part in srcset.split(","):
            candidate = part.strip().split(" ")[0]
            if not candidate:
                continue
            asset_url = urljoin(item_url, candidate)
            parsed = urlparse(asset_url)
            if parsed.netloc == parsed_item.netloc and parsed.path.startswith(item_path) and not parsed.path.endswith("/"):
                asset_urls.add(asset_url)

    for asset_url in sorted(asset_urls):
        try:
            asset_bytes = fetch_bytes(asset_url)
        except Exception:
            continue
        asset_path = relative_site_path(asset_url)
        if asset_path is None:
            continue
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(asset_bytes)

    return True


def item_looks_unchanged(item: dict, previous: dict | None) -> bool:
    if not previous:
        return False
    return clean_text(item.get("title")) == previous.get("title", "") and clean_text(item.get("description")) == previous.get("description", "")


def live_item_page_needs_rebuild(item_url: str) -> bool:
    try:
        item_html = fetch(item_url)
    except Exception:
        return True

    suspicious_text = ("â\x80", "Â\xa0", "Â </", "Â<", "Â\n", "â", "Â ")
    if any(token in item_html for token in suspicious_text):
        return True

    soup = BeautifulSoup(item_html, "html.parser")
    for tag in soup.find_all(src=True):
        src = tag.get("src") or ""
        if src.startswith("/globalassets/") or src.startswith("/_-"):
            return True
    for tag in soup.find_all(srcset=True):
        srcset = tag.get("srcset") or ""
        for part in srcset.split(","):
            candidate = part.strip().split(" ")[0]
            if candidate.startswith("/globalassets/") or candidate.startswith("/_-"):
                return True
    return False


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

        title_el = a.select_one(".teaser__title-cover")
        desc_el = a.select_one(".teaser__copy-cover")
        date_el = a.select_one(".label-grey")
        type_el = a.select_one(".label-dark")
        detail_el = a.select_one(".teaser__detail")
        author_el = a.select_one(".author")
        img_el = a.select_one("picture img")
        img_src = ""
        if img_el:
            img_src = img_el.get("data-src") or img_el.get("src") or ""
            if img_src.startswith("/"):
                img_src = urljoin(SITE_BASE, img_src)

        title = clean_text(title_el.get_text(" ", strip=True) if title_el else "")
        desc = clean_text(desc_el.get_text(" ", strip=True) if desc_el else "")
        date = clean_text(date_el.get_text(" ", strip=True) if date_el else "")
        item_type = clean_text(type_el.get_text(" ", strip=True) if type_el else "")
        detail = clean_text(detail_el.get_text(" ", strip=True) if detail_el else "")
        author = clean_text(author_el.get_text(" ", strip=True).replace("By:", "") if author_el else "")

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


def repair_mojibake_text(s: str) -> str:
    if not s:
        return s
    suspicious = ("â", "Â", "Ã")
    if not any(token in s for token in suspicious):
        return s
    try:
        repaired = s.encode("latin1").decode("utf-8")
        if sum(repaired.count(token) for token in suspicious) < sum(s.count(token) for token in suspicious):
            return repaired
    except Exception:
        pass
    return s


def absolutize_asset_urls(fragment: str, base_url: str) -> str:
    if not fragment:
        return fragment
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup.find_all(src=True):
        src = tag.get("src") or ""
        if src.startswith("/"):
            tag["src"] = urljoin(base_url, src)
    for tag in soup.find_all(href=True):
        href = tag.get("href") or ""
        if href.startswith("/"):
            tag["href"] = urljoin(base_url, href)
    for tag in soup.find_all(srcset=True):
        srcset = tag.get("srcset") or ""
        parts = []
        for part in srcset.split(","):
            chunk = part.strip()
            if not chunk:
                continue
            bits = chunk.split()
            bits[0] = urljoin(base_url, bits[0])
            parts.append(" ".join(bits))
        tag["srcset"] = ", ".join(parts)
    return str(soup)


def decode_vue_content(raw: str) -> str:
    s = html.unescape(raw)
    s = s.encode("utf-8").decode("unicode_escape")
    s = html.unescape(s)
    s = repair_mojibake_text(s)
    s = s.replace("\\n", "\n")
    s = s.replace("\\/", "/")
    s = re.sub(
        r'<PageFootnoteReference[^>]*title="([^"]+)"[^>]*>.*?</PageFootnoteReference>',
        lambda m: f'<sup>[Footnote: {html.escape(html.unescape(m.group(1)))}]</sup>',
        s,
    )
    s = re.sub(r"</?vue-[^>]+>", "", s)
    s = s.replace("\u200B", "").replace("\u0000", "").replace("\u001f", "")
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
        raw = decoded_page[idx + len(marker) :]
        try:
            s = raw.encode("utf-8").decode("unicode_escape")
            s = html.unescape(s)
        except Exception:
            pos = idx + len(marker)
            continue
        start = s.find("{")
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
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
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


def inject_chart_placeholders(body_html: str, chart_count: int) -> tuple[str, int]:
    idx = 0

    def repl(_: re.Match) -> str:
        nonlocal idx
        if idx >= chart_count:
            return _.group(0)
        idx += 1
        return f"<div>{CHART_TOKEN_PREFIX}{idx}__</div>"

    body_html = re.sub(
        r"<div class=\"vue dws-dx\">[\s\S]*?<client-only>\s*</client-only>[\s\S]*?</div>",
        repl,
        body_html,
        flags=re.I,
    )
    body_html = re.sub(r"</?client-only>", "", body_html, flags=re.I)
    return body_html, idx


class DWSChartCapturer:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None

    def __enter__(self):
        executable = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
        if not executable:
            raise RuntimeError("No Chrome/Chromium executable found for DWS chart capture")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1600, "height": 2400},
            device_scale_factor=2,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    @staticmethod
    def _clean_chart_svg(svg_markup: str) -> str:
        svg_markup = re.sub(r"<text[^>]*class=\"highcharts-credits\"[\\s\\S]*?</text>", "", svg_markup, flags=re.I)
        svg_markup = re.sub(r"<g[^>]*class=\"highcharts-contextbutton\"[\\s\\S]*?</g>", "", svg_markup, flags=re.I)
        svg_markup = re.sub(r"<g[^>]*class=\"highcharts-a11y-proxy-group\"[\\s\\S]*?</g>", "", svg_markup, flags=re.I)
        if "xmlns=\"http://www.w3.org/2000/svg\"" not in svg_markup:
            svg_markup = svg_markup.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
        return svg_markup

    def _capture_chart_svg(self, chart, out: Path) -> bool:
        svg = chart.locator("svg.highcharts-root").first
        if svg.count() == 0:
            return False
        svg_markup = svg.evaluate("el => el.outerHTML")
        if not svg_markup or "<svg" not in svg_markup:
            return False
        out.write_text(self._clean_chart_svg(svg_markup), encoding="utf-8")
        return True

    def _capture_chart_screenshot(self, page, chart, out: Path) -> bool:
        target = chart.locator(".highcharts-container").first
        try:
            page.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})", chart.element_handle())
        except Exception:
            try:
                chart.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
        page.wait_for_timeout(500)
        try:
            target.screenshot(path=str(out), timeout=10000)
            return True
        except Exception:
            try:
                chart.screenshot(path=str(out), timeout=10000)
                return True
            except Exception:
                return False

    def capture(self, url: str, chart_dir: Path, public_base: str, site_dir: Path) -> list[str]:
        chart_dir.mkdir(parents=True, exist_ok=True)
        page = self.context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=90000)
            page.add_style_tag(
                content="""
                .highcharts-a11y-proxy-container-before,
                .highcharts-a11y-proxy-container-after,
                .highcharts-a11y-proxy-group,
                .highcharts-announcer-container,
                .highcharts-contextbutton,
                .highcharts-button,
                .highcharts-credits {
                  display: none !important;
                }
                body { background: #fff !important; }
                """
            )
            page.wait_for_timeout(1000)
            charts = page.locator(".d-everviz-chart")
            count = charts.count()
            chart_urls = []
            for idx in range(count):
                chart = charts.nth(idx)
                svg_out = chart_dir / f"chart-{idx + 1}.svg"
                png_out = chart_dir / f"chart-{idx + 1}.png"
                if self._capture_chart_svg(chart, svg_out):
                    out = svg_out
                elif self._capture_chart_screenshot(page, chart, png_out):
                    out = png_out
                else:
                    continue
                chart_urls.append(public_url_for_path(out, public_base, site_dir))
            return chart_urls
        finally:
            page.close()


def detail_content(url: str, chart_dir: Path, public_base: str, site_dir: Path, chart_capturer: DWSChartCapturer) -> dict:
    page = fetch(url)

    header_match = re.search(r'<vue-article-page-layout :model="([\s\S]*?)">', page)
    header = {}
    if header_match:
        header = json.loads(html.unescape(header_match.group(1)))

    blocks = re.findall(
        r'Blocks\.Html\.Blocks\.HtmlBlock[\s\S]*?&quot;content&quot;:&quot;([\s\S]*?)&quot;,&quot;productBuyingProcessPageProps&quot;',
        page,
    )

    html_blocks = []
    for raw_block in blocks:
        decoded = decode_vue_content(raw_block)
        if decoded.strip():
            html_blocks.append(decoded)

    body_html = "\n".join(html_blocks)
    body_html = absolutize_asset_urls(body_html, url)
    source_chart_count = len(extract_chart_models_from_page(page))
    body_html, chart_index = inject_chart_placeholders(body_html, source_chart_count)
    chart_urls = chart_capturer.capture(url, chart_dir, public_base, site_dir) if chart_index else []

    for idx in range(1, chart_index + 1):
        token = f"{CHART_TOKEN_PREFIX}{idx}__"
        if idx <= len(chart_urls):
            replacement = f'<figure class="chart-image"><img src="{html.escape(chart_urls[idx - 1])}" alt="Chart {idx}" loading="lazy"></figure>'
        else:
            replacement = '<div class="chart-fallback-note">[Chart capture unavailable]</div>'
        body_html = body_html.replace(token, replacement)

    article_header = header.get("articleHeaderProps", {}) if isinstance(header, dict) else {}
    intro = decode_vue_content(article_header.get("introText", "") or "")
    intro = absolutize_asset_urls(intro, url)
    hero = article_header.get("image") or {}
    hero_src = hero.get("src") or ""
    if hero_src.startswith("/"):
        hero_src = urljoin(SITE_BASE, hero_src)
    hero_alt = hero.get("alt") or article_header.get("headline") or ""

    return {
        "headline": article_header.get("headline") or "",
        "date": article_header.get("date") or "",
        "intro_html": intro,
        "hero_src": hero_src,
        "hero_alt": hero_alt,
        "body_html": body_html,
    }


def build_item_page(item: dict, detail: dict) -> str:
    title = detail.get("headline") or item["title"]
    date = detail.get("date") or item.get("date") or ""
    author = item.get("author") or ""
    detail_line = item.get("detail") or ""
    hero_src = detail.get("hero_src") or item.get("image") or ""
    hero_alt = detail.get("hero_alt") or title
    intro_html = detail.get("intro_html") or ""
    body_html = detail.get("body_html") or ""

    meta_parts = [x for x in [date, author, detail_line] if x]
    meta_line = " · ".join(meta_parts)
    hero_html = f'<figure><img src="{html.escape(hero_src)}" alt="{html.escape(hero_alt)}" loading="lazy"></figure>' if hero_src else ""

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
    .chart-fallback-note {{ color:#555; font-size:.92rem; margin:.5rem 0; }}
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


def build_xml(items: list[dict], public_base: str, last_build_date: datetime | None) -> bytes:
    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = FEED_TITLE
    SubElement(channel, "link").text = f"{public_base.rstrip('/')}/{FEED_NAME}.xml"
    SubElement(channel, "description").text = FEED_DESC
    SubElement(channel, "lastBuildDate").text = format_rss_date(last_build_date)
    SubElement(channel, "generator").text = "GitHub Pages RSS rewrite"

    for item in items:
        slug = slugify(item["url"].rstrip("/").split("/")[-1])
        local_url = f"{public_base.rstrip('/')}/item/{FEED_NAME}/{slug}/"
        it = SubElement(channel, "item")
        SubElement(it, "title").text = item["title"]
        SubElement(it, "link").text = local_url
        guid = SubElement(it, "guid")
        guid.set("isPermaLink", "true")
        guid.text = local_url
        SubElement(it, "pubDate").text = format_rss_date(item.get("pub_date"))
        if item.get("description"):
            SubElement(it, "description").text = item["description"]

    return minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dws_cio_feed.py <site_dir> <public_base>")
        sys.exit(1)

    site_dir = Path(sys.argv[1])
    public_base = sys.argv[2]
    site_dir.mkdir(parents=True, exist_ok=True)

    items = list_items()
    live_state = load_live_feed_state(public_base)
    live_items = live_state.get("items", {})
    selected = []
    changed = False
    chart_capturer = None

    try:
        for item in items:
            if "/en-us/insights/cio-view/" not in item["url"]:
                continue

            slug = slugify(item["url"].rstrip("/").split("/")[-1])
            previous = live_items.get(slug)
            item["pub_date"] = parse_dws_date(item.get("date", "")) or (previous or {}).get("pub_date")
            selected.append(item)

            if item_looks_unchanged(item, previous) and previous and previous.get("link"):
                if not live_item_page_needs_rebuild(previous["link"]) and restore_live_item_tree(previous["link"], public_base, site_dir):
                    continue

            changed = True
            item_dir = site_dir / "item" / FEED_NAME / slug
            chart_dir = item_dir / "charts"
            item_dir.mkdir(parents=True, exist_ok=True)

            if chart_capturer is None:
                chart_capturer = DWSChartCapturer().__enter__()

            detail = detail_content(item["url"], chart_dir, public_base, site_dir, chart_capturer)
            item["pub_date"] = parse_dws_date(detail.get("date", "")) or item.get("pub_date")
            (item_dir / "index.html").write_text(build_item_page(item, detail), encoding="utf-8")
    finally:
        if chart_capturer is not None:
            chart_capturer.__exit__(None, None, None)

    current_slugs = {slugify(item["url"].rstrip("/").split("/")[-1]) for item in selected}
    if set(live_items.keys()) != current_slugs:
        changed = True

    last_build_date = datetime.now(timezone.utc) if changed or not live_state.get("last_build_date") else live_state.get("last_build_date")
    xml = build_xml(selected, public_base, last_build_date)
    (site_dir / f"{FEED_NAME}.xml").write_bytes(xml)
    print(f"built {FEED_NAME} with {len(selected)} items")
