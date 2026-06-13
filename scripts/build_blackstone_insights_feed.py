#!/usr/bin/env python3
"""Blackstone Insights builder: WordPress REST API (full text) + chart screenshots.

WHY THIS DESIGN
---------------
blackstone.com is a WordPress site whose article HTML is JS-rendered behind
Akamai-style bot protection, so a plain page fetch returns a shell with no body.
But the site exposes a clean WordPress REST API for the `insight` post type:

    https://www.blackstone.com/wp-json/wp/v2/insight?orderby=date&order=desc
    https://www.blackstone.com/wp-json/wp/v2/insight/<id>

`content.rendered` there is the COMPLETE article body (headings, paragraphs,
lists, tables, footnotes, disclosures) -- BUT ONLY when the post object is
fetched WITHOUT a `_fields` filter. Narrowing the response with `_fields=content`
returns an EMPTY content.rendered for this post type, so we fetch each article's
full object individually for its body (see fetch_full_content).

Charts are drawn client-side (Highcharts SVG) and are NOT present in
content.rendered (only empty mount points), so no text source can capture them.
We therefore (best-effort) drive a headless browser to SCREENSHOT each chart and
splice the PNGs back into the text at the matching chart heading (matched by the
footnote number, e.g. [1], that appears in both the chart title and the text).
If the browser is unavailable, the article still ships with full text (charts
simply omitted).

Output:
* site/blackstone_insights.xml : items link to local pages, full article in
  <content:encoded> with absolute image URLs.
* site/item/blackstone_insights/<slug>/index.html (+ fig-NNN.png chart shots).

Incremental: restore published feed + item pages from live Pages, reuse already
localized articles, render only new ones. Bump RENDER_VERSION to force rebuild.

Local item pages are regenerated each run and shipped inside the Pages artifact
(they are NOT committed to the repo). To make sure the feed's local links never
404 -- even on a run that kept an existing feed without re-rendering pages -- we
self-heal: any feed item missing its on-disk page is rebuilt from the feed's
content:encoded HTML before deploy (see ensure_local_pages).

Usage: python scripts/build_blackstone_insights_feed.py <site_dir> <public_base>
"""
import asyncio
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
CONTENT_ENCODED = "{" + CONTENT_NS + "}encoded"
ET.register_namespace("content", CONTENT_NS)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SITE_BASE = "https://www.blackstone.com"
REST_BASE = os.environ.get("BLACKSTONE_REST_BASE", f"{SITE_BASE}/wp-json/wp/v2/insight")
SOURCE_FEED_URL = os.environ.get("BLACKSTONE_SOURCE_FEED_URL", "https://www.blackstone.com/insights/feed/")
FEED_NAME = os.environ.get("BLACKSTONE_FEED_NAME", "blackstone_insights")
OUTPUT_FILE = os.environ.get("BLACKSTONE_OUTPUT_FILE", f"{FEED_NAME}.xml")
FEED_TITLE = os.environ.get("BLACKSTONE_FEED_TITLE", "Blackstone Insights")
FEED_DESC = "Blackstone Insights articles rendered to local reader-friendly pages with full text and charts."
MAX_ITEMS = int(os.environ.get("BLACKSTONE_MAX_ITEMS", "25"))
MAX_FIGS = int(os.environ.get("BLACKSTONE_MAX_FIGS", "30"))
NAV_TIMEOUT = int(os.environ.get("BLACKSTONE_NAV_TIMEOUT", "45"))
REQUEST_TIMEOUT = int(os.environ.get("BLACKSTONE_TIMEOUT", "45"))
FORCE_REBUILD = os.environ.get("BLACKSTONE_FORCE_REBUILD", "0") == "1"
BROWSER_ENABLED = os.environ.get("BLACKSTONE_BROWSER", "1") != "0"
USER_AGENT = os.environ.get(
    "BLACKSTONE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ARTICLE_RE = re.compile(r"https://www\.blackstone\.com/insights/article/[a-z0-9\-]+/", re.I)

# Bump when rendering changes so published pages regenerate on the next run.
RENDER_VERSION = 5

INLINE_OK = {"strong", "em", "b", "i", "u", "a", "br", "sup", "sub", "code"}
BLOCK_OK = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote",
    "figure", "figcaption", "table", "thead", "tbody", "tfoot", "tr", "th",
    "td", "caption",
}
DROP = {"script", "style", "noscript", "svg", "form", "button", "iframe",
        "input", "select", "textarea", "nav", "aside", "header", "footer"}

# --------------------------------------------------------------------------- #
# Browser-side JS (charts only)
# --------------------------------------------------------------------------- #
AUTOSCROLL_JS = """() => new Promise((resolve) => {
  let total = 0; const step = 700;
  const timer = setInterval(() => {
    window.scrollBy(0, step); total += step;
    if (total >= document.body.scrollHeight - window.innerHeight - step) { clearInterval(timer); resolve(); }
  }, 180);
  setTimeout(() => { clearInterval(timer); resolve(); }, 9000);
})"""

# Find chart graphics in document order, tag each container with data-bx-idx, and
# capture the nearest PRECEDING heading text (which carries the footnote [N]).
CHART_JS = """() => {
  const root = document.querySelector('article') || document.querySelector('main') || document.body;
  if (!root) return [];
  const out = [];
  let idx = 0;
  const seen = new Set();
  const heads = Array.from(root.querySelectorAll('h2, h3, h4'));
  function chartAncestor(el){
    let best = el, p = el.parentElement, hops = 0;
    while (p && p !== root && hops < 5){
      const c = ((p.className || '') + '');
      if (/chart|graphic|visual|exhibit|figure|highcharts|tableau|d3|datawrapper|flourish/i.test(c)) best = p;
      p = p.parentElement; hops++;
    }
    return best;
  }
  function nearHeading(el){
    let best = '';
    for (const h of heads){
      const pos = h.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING){
        const t = h.innerText.trim();
        if (t) best = t;
      } else {
        break;
      }
    }
    return best;
  }
  const graphics = root.querySelectorAll('svg, canvas');
  for (const g of graphics){
    const t = chartAncestor(g);
    if (seen.has(t)) continue;
    const r = t.getBoundingClientRect();
    if (r.width < 120 || r.height < 90) continue;
    if (t.getAttribute('data-bx-idx')) continue;
    seen.add(t);
    idx++;
    t.setAttribute('data-bx-idx', String(idx));
    out.push({idx: idx, caption: nearHeading(g)});
  }
  return out;
}"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def slugify(value: str) -> str:
    value = html.unescape(value or "")
    value = unquote(value)
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def short_hash(*parts: str) -> str:
    import hashlib
    base = "|".join(part or "" for part in parts)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:10]


def normalize_space(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def shorten(value: str, max_len: int = 420) -> str:
    value = normalize_space(value)
    if len(value) <= max_len:
        return value
    cut = value[:max_len].rsplit(" ", 1)[0].strip()
    return (cut or value[:max_len]).rstrip(".,;:!?") + "\u2026"


def strip_tags(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value or "", flags=re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(value)


def to_rfc822(value: str) -> str:
    if not value:
        return format_datetime(datetime.now(timezone.utc))
    v = value.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = parsedate_to_datetime(v)
        except Exception:
            dt = None
    if dt is None:
        return format_datetime(datetime.now(timezone.utc))
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return format_datetime(dt)


def human_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return ""
    return dt.strftime("%B %d, %Y")


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response.content


def article_slug(url: str) -> str:
    path = (urlparse(url).path or "").strip("/")
    leaf = unquote(path.split("/")[-1]) if path else ""
    base = slugify(leaf) if leaf else slugify(url)
    return f"{base}-{short_hash(url)}"


# --------------------------------------------------------------------------- #
# REST content.rendered -> clean reader HTML
# --------------------------------------------------------------------------- #
def img_html(node, base_url):
    src = node.get("src") or node.get("data-src") or ""
    if not src or src.startswith("data:"):
        ss = node.get("srcset") or node.get("data-srcset") or ""
        if ss:
            src = ss.split(",")[0].strip().split(" ")[0]
    if not src:
        return ""
    src = urljoin(base_url, src)
    if not src.startswith("http"):
        return ""
    alt = html.escape(node.get("alt") or "", quote=True)
    return f'<figure><img src="{html.escape(src, quote=True)}" alt="{alt}" loading="lazy"></figure>'


def serialize_node(node, base_url):
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    if name in DROP:
        return ""
    if name == "img":
        return img_html(node, base_url)
    if name == "br":
        return "<br>"
    inner = "".join(serialize_node(c, base_url) for c in node.children)
    if name == "a":
        href = node.get("href") or ""
        href = urljoin(base_url, href) if href and not href.startswith("#") else ""
        if not inner.strip():
            return ""
        return f'<a href="{html.escape(href, quote=True)}">{inner}</a>' if href.startswith("http") else inner
    if name in INLINE_OK:
        return f"<{name}>{inner}</{name}>" if inner.strip() else ""
    if name in BLOCK_OK:
        tag = "h2" if name == "h1" else ("h4" if name in {"h5", "h6"} else name)
        return f"<{tag}>{inner}</{tag}>" if inner.strip() else ""
    return inner


def serialize_children(node, base_url):
    return "\n".join(filter(None, (serialize_node(c, base_url) for c in node.children)))


def clean_rest_content(content_html, base_url, title):
    soup = BeautifulSoup(content_html or "", "html.parser")
    for tag in soup.find_all(list(DROP)):
        tag.decompose()
    body = serialize_children(soup, base_url)
    body = re.sub(r"\n{2,}", "\n", body)
    # Drop a leading heading that just repeats the article title.
    t_esc = re.escape(html.escape(normalize_space(title)))
    body = re.sub(r'^\s*<h2>\s*' + t_esc + r'\s*</h2>\s*', "", body, count=1, flags=re.I)
    # Mark chart slots: an <h4> whose text contains a footnote bracket like [ 1 ].
    def _mark(m):
        seg = m.group(0)
        fm = re.search(r"\[\s*(\d+)\s*\]", seg)
        return seg + ("<!--BXCHART:%d-->" % int(fm.group(1)) if fm else "")
    body = re.sub(r"<h4>.*?</h4>", _mark, body, flags=re.S)
    return body.strip()


def _figure_html(src, caption):
    cap = normalize_space(re.sub(r"\[\s*\d+\s*\]", "", caption or ""))
    cap_html = "<figcaption>%s</figcaption>" % html.escape(cap) if cap else ""
    return '<figure><img src="%s" alt="%s" loading="lazy">%s</figure>' % (
        html.escape(src, quote=True), html.escape(cap or "chart", quote=True), cap_html)


def inject_charts(body, chart_map, img_prefix):
    used = set()

    def _repl(m):
        no = int(m.group(1))
        info = chart_map.get(no)
        if info and info.get("file"):
            used.add(no)
            return _figure_html(img_prefix + info["file"], info.get("caption"))
        return ""

    out = re.sub(r"<!--BXCHART:(\d+)-->", _repl, body)
    leftovers = [info for key, info in sorted(chart_map.items(), key=lambda kv: kv[0])
                 if key not in used and info.get("file")]
    if leftovers:
        figs = "".join(_figure_html(img_prefix + i["file"], i.get("caption")) for i in leftovers)
        out += "\n<h3>Exhibits</h3>\n" + figs
    return out


# --------------------------------------------------------------------------- #
# JSON fetch (requests, with browser fallback that bypasses bot protection)
# --------------------------------------------------------------------------- #
async def _browser_get_json(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, context = await _new_context(p)
        try:
            resp = await context.request.get(url, timeout=NAV_TIMEOUT * 1000)
            text = await resp.text()
            return json.loads(text)
        finally:
            await browser.close()


def fetch_json(url, session):
    try:
        r = session.get(url, headers={**HEADERS, "Accept": "application/json"},
                        timeout=REQUEST_TIMEOUT, allow_redirects=True)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and "json" in ctype:
            return r.json()
        print(f"INFO: REST via requests -> status={r.status_code} type={ctype}")
    except Exception as exc:
        print(f"INFO: REST via requests failed: {exc}")
    if BROWSER_ENABLED:
        try:
            return asyncio.run(_browser_get_json(url))
        except Exception as exc:
            print(f"WARN: REST via browser failed: {exc}")
    return None


def fetch_insight_posts(session):
    # IMPORTANT: do NOT request `content` through _fields here. For the `insight`
    # post type the REST API returns an EMPTY content.rendered whenever the
    # response is narrowed with _fields. Discovery therefore stays metadata-only
    # and each article's body is fetched separately via fetch_full_content().
    fields = "id,link,date,date_gmt,modified_gmt,title,excerpt"
    url = f"{REST_BASE}?per_page={MAX_ITEMS}&orderby=date&order=desc&_fields={fields}"
    data = fetch_json(url, session)
    if isinstance(data, list):
        print(f"INFO: REST returned {len(data)} insight posts")
        return data
    print("WARN: REST insight list unavailable or not a list")
    return []


def fetch_full_content(post_id, session=None):
    """Fetch one insight's FULL object (NO _fields) to get content.rendered.

    Blackstone's `insight` endpoint returns an empty content.rendered when the
    response is narrowed with _fields, but the complete article body (headings,
    paragraphs, tables, and footnote/disclosure text) is present when the full
    object is requested. So we fetch each article individually here.
    """
    if not post_id:
        return ""
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    data = fetch_json(f"{REST_BASE}/{post_id}", session)
    if isinstance(data, dict):
        return (data.get("content") or {}).get("rendered", "") or ""
    return ""


def discover_articles(session):
    posts = fetch_insight_posts(session)
    posts_by_url, urls = {}, []
    for post in posts:
        link = (post.get("link") or "").split("?")[0].split("#")[0]
        m = ARTICLE_RE.match(link)
        if not m:
            continue
        link = m.group(0)
        if link in posts_by_url:
            continue
        posts_by_url[link] = post
        urls.append(link)
        if len(urls) >= MAX_ITEMS:
            break
    return urls, posts_by_url


# --------------------------------------------------------------------------- #
# Browser chart screenshots (best-effort)
# --------------------------------------------------------------------------- #
async def _new_context(p):
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        viewport={"width": 1280, "height": 2000},
        device_scale_factor=2,
    )
    return browser, context


async def _dismiss_gate(page):
    selectors = [
        "text=I am a United States Resident",
        "text=I am not a United States Resident",
        "text=Accept All",
        "text=Accept all",
        "text=Accept",
        "text=I Agree",
        "text=Agree",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def _capture_charts(jobs):
    from playwright.async_api import async_playwright
    maps = {}
    async with async_playwright() as p:
        browser, context = await _new_context(p)
        page = await context.new_page()
        try:
            for job in jobs:
                url, out_dir = job["url"], job["out_dir"]
                cmap = {}
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                    await _dismiss_gate(page)
                    await page.wait_for_timeout(2000)
                    try:
                        await page.evaluate(AUTOSCROLL_JS)
                        await page.evaluate("window.scrollTo(0, 0)")
                        await page.wait_for_timeout(700)
                    except Exception:
                        pass
                    charts = await page.evaluate(CHART_JS)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    for old in out_dir.glob("fig-*.png"):
                        try:
                            old.unlink()
                        except Exception:
                            pass
                    fig_i, neg = 0, 0
                    for c in charts or []:
                        if fig_i >= MAX_FIGS:
                            break
                        sel = '[data-bx-idx="' + str(c.get("idx")) + '"]'
                        el = await page.query_selector(sel)
                        if not el:
                            continue
                        try:
                            await el.scroll_into_view_if_needed(timeout=4000)
                            await page.wait_for_timeout(450)
                            fig_i += 1
                            name = f"fig-{fig_i:03d}.png"
                            (out_dir / name).write_bytes(await el.screenshot(timeout=9000))
                            cap = normalize_space(c.get("caption") or "")
                            fm = re.search(r"\[\s*(\d+)\s*\]", cap)
                            if fm:
                                key = int(fm.group(1))
                            else:
                                neg -= 1
                                key = neg
                            cmap[key] = {"file": name, "caption": cap}
                        except Exception as exc:
                            print(f"WARN: chart screenshot failed {url}: {exc}")
                    print(f"INFO: {url} -> charts captured={fig_i}")
                except Exception as exc:
                    print(f"WARN: chart render failed for {url}: {exc}")
                maps[url] = cmap
        finally:
            await browser.close()
    return maps


def process_jobs(jobs):
    if not jobs:
        return {}
    chart_maps = {}
    if BROWSER_ENABLED:
        try:
            chart_maps = asyncio.run(_capture_charts(jobs))
        except Exception as exc:
            print(f"WARN: browser charts unavailable ({exc}); shipping text-only")
    session = requests.Session()
    session.headers.update(HEADERS)
    results = {}
    for job in jobs:
        url, post = job["url"], job["post"]
        out_dir, local_url = job["out_dir"], job["local_url"]
        title = normalize_space(strip_tags((post.get("title") or {}).get("rendered", ""))) or "Untitled"
        content_html = (post.get("content") or {}).get("rendered", "") or ""
        if not content_html.strip():
            # The discovery list omits content (REST returns it empty under
            # _fields), so fetch the article's full object for its real body.
            content_html = fetch_full_content(post.get("id"), session)
        cmap = chart_maps.get(url, {})
        clean = clean_rest_content(content_html, url, title)
        if not clean.strip() and not cmap:
            print(f"WARN: empty REST content for {url}; skipping")
            continue
        page_html = inject_charts(clean, cmap, "")
        feed_html = inject_charts(clean, cmap, local_url)
        plain = strip_tags(page_html)
        excerpt = normalize_space(strip_tags((post.get("excerpt") or {}).get("rendered", "")))
        summary = excerpt or shorten(plain)
        date_raw = post.get("date_gmt") or post.get("date") or ""
        n_figs = sum(1 for v in cmap.values() if v.get("file"))
        print(f"INFO: {url} -> text={len(plain)}chars figs={n_figs}")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(
            build_local_page(title, url, page_html, human_date(date_raw)),
            encoding="utf-8",
        )
        results[url] = {
            "title": title,
            "summary": summary,
            "date_raw": date_raw,
            "feed_html": feed_html,
            "plain": plain,
            "source_url": url,
        }
    return results


# --------------------------------------------------------------------------- #
# Local page
# --------------------------------------------------------------------------- #
PAGE_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; color: #1a1a1a; background: #fff; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 18px 72px; line-height: 1.7; }
  h1 { font-size: 1.7em; line-height: 1.25; margin: 0 0 8px; }
  h2 { font-size: 1.3em; margin: 1.5em 0 0.5em; }
  h3 { font-size: 1.1em; margin: 1.2em 0 0.4em; }
  h4 { font-size: 1.0em; margin: 1.1em 0 0.3em; color: #444; }
  p { margin: 0.7em 0; }
  ul, ol { margin: 0.6em 0 0.6em 1.2em; }
  figure { margin: 1.2em 0; text-align: center; }
  figure img { max-width: 100%; height: auto; border: 1px solid #eee; border-radius: 4px; }
  figcaption { font-size: 0.85em; color: #666; margin-top: 6px; }
  .tablewrap { overflow-x: auto; margin: 1.1em 0; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
  thead th { background: #f5f5f5; }
  blockquote { margin: 1em 0; padding: 0.4em 1em; border-left: 3px solid #ccc; color: #444; }
  .meta { color: #777; font-size: 0.9em; margin-bottom: 14px; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 24px; }
  .btn { display: inline-block; padding: 8px 13px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; background: #fff; font-size: 0.9em; }
  .btn.primary { background: #111; color: #fff; border-color: #111; }
"""


def build_local_page(title, source_url, content_html, date_human):
    body = content_html or "<p>Content could not be extracted from this article.</p>"
    meta = f'<div class="meta">{html.escape(date_human)}</div>' if date_human else ""
    view_btn = ""
    if source_url and source_url.startswith("http") and "blackstone.com" in source_url:
        view_btn = (
            f'      <a class="btn primary" href="{html.escape(source_url, quote=True)}" '
            f'target="_blank" rel="noopener">View on Blackstone</a>'
        )
    actions = f'    <div class="actions">\n{view_btn}\n    </div>' if view_btn else ""
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        f'  <meta name="render-version" content="{RENDER_VERSION}">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f'  <link rel="canonical" href="{html.escape(source_url, quote=True)}">',
        f"  <title>{html.escape(title)}</title>",
        f"  <style>{PAGE_CSS}</style>",
        "</head>",
        "<body>",
        '  <article class="wrap">',
        f"    <h1>{html.escape(title)}</h1>",
        f"    {meta}",
        actions,
        body,
        "  </article>",
        "</body>",
        "</html>",
    ])


# --------------------------------------------------------------------------- #
# Incremental state
# --------------------------------------------------------------------------- #
def parse_existing_feed(xml_path):
    root = ET.parse(xml_path).getroot()
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        slug = None
        parts = [p for p in urlparse(link).path.split("/") if p]
        if "item" in parts:
            idx = parts.index("item")
            if len(parts) > idx + 2 and parts[idx + 1] == FEED_NAME:
                slug = parts[idx + 2]
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "guid": (item.findtext("guid") or "").strip(),
            "pub_date": (item.findtext("pubDate") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
            "content_html": (item.findtext(CONTENT_ENCODED) or ""),
            "slug": slug,
        })
    return items


def restore_live_feed(public_base, site_dir):
    feed_url = f"{public_base.rstrip('/')}/{OUTPUT_FILE}"
    output_path = site_dir / OUTPUT_FILE
    try:
        xml_bytes = fetch_bytes(feed_url, timeout=30)
    except Exception:
        return False
    output_path.write_bytes(xml_bytes)
    try:
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return True
        local_prefix = public_base.rstrip("/") + f"/item/{FEED_NAME}/"
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            if not link.startswith(local_prefix):
                continue
            try:
                item_bytes = fetch_bytes(link, timeout=30)
            except Exception:
                continue
            item_dir = site_dir / urlparse(link).path.lstrip("/")
            item_dir.mkdir(parents=True, exist_ok=True)
            (item_dir / "index.html").write_bytes(item_bytes)
            html_text = item_bytes.decode("utf-8", errors="ignore")
            for ref in set(re.findall(r'(?:src|href)="([^"]+)"', html_text)):
                if not ref or ref.startswith(("http://", "https://", "#", "data:")):
                    continue
                asset_url = urljoin(link, ref)
                try:
                    asset_bytes = fetch_bytes(asset_url, timeout=30)
                except Exception:
                    continue
                asset_path = site_dir / urlparse(asset_url).path.lstrip("/")
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_bytes(asset_bytes)
    except Exception:
        pass
    return True


def load_existing_items(site_dir, public_base):
    output_path = site_dir / OUTPUT_FILE
    if not output_path.exists():
        restore_live_feed(public_base, site_dir)
    if not output_path.exists():
        return []
    try:
        return parse_existing_feed(output_path)
    except Exception:
        return []


def local_render_version(index_path):
    try:
        txt = index_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    m = re.search(r'name="render-version"\s+content="(\d+)"', txt)
    return int(m.group(1)) if m else 0


def _slug_from_link(link):
    parts = [p for p in urlparse(link or "").path.split("/") if p]
    if "item" in parts:
        idx = parts.index("item")
        if len(parts) > idx + 2 and parts[idx + 1] == FEED_NAME:
            return parts[idx + 2]
    return None


def _human_from_rfc822(value):
    try:
        return parsedate_to_datetime(value).strftime("%B %d, %Y")
    except Exception:
        return ""


def ensure_local_pages(item_root, items):
    """Guarantee every feed item has a local reader page on disk.

    Local item pages live only inside the deployed Pages artifact (not committed),
    so a run that keeps an existing feed without re-rendering pages (e.g. REST
    discovery was blocked) could ship a feed whose local links 404. Rebuild any
    missing page directly from the feed's content:encoded HTML so links always work.
    """
    healed = 0
    for it in items:
        link = it.get("link") or ""
        slug = it.get("slug") or _slug_from_link(link)
        if not slug:
            continue
        idx_path = item_root / slug / "index.html"
        if idx_path.exists():
            continue
        content_html = it.get("content_html") or it.get("feed_html") or ""
        if not content_html.strip():
            continue
        title = it.get("title") or slug
        source_url = it.get("source_url") or ""
        date_human = it.get("date_human") or _human_from_rfc822(it.get("pub_date") or "")
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(
            build_local_page(title, source_url, content_html, date_human),
            encoding="utf-8",
        )
        healed += 1
    if healed:
        print(f"INFO: self-healed {healed} missing local {FEED_NAME} page(s)")
    return healed


# --------------------------------------------------------------------------- #
# Feed assembly
# --------------------------------------------------------------------------- #
def build_feed(site_dir, public_base):
    public_base = public_base.rstrip("/")
    session = requests.Session()
    session.headers.update(HEADERS)
    item_root = site_dir / "item" / FEED_NAME
    item_root.mkdir(parents=True, exist_ok=True)
    output_path = site_dir / OUTPUT_FILE

    existing_items = load_existing_items(site_dir, public_base)
    existing_slug_map = {it["slug"]: it for it in existing_items if it.get("slug")}

    article_urls, posts_by_url = discover_articles(session)
    if not article_urls:
        if output_path.exists():
            ensure_local_pages(item_root, existing_items)
            print("no article URLs discovered; kept existing feed and self-healed local pages")
            return
        raise RuntimeError("could not discover any Blackstone Insights article URLs")
    print(f"INFO: {len(article_urls)} candidate articles")

    jobs, reuse_map = [], {}
    for url in article_urls:
        slug = article_slug(url)
        local_index = item_root / slug / "index.html"
        local_url = f"{public_base}/item/{FEED_NAME}/{slug}/"
        existing = existing_slug_map.get(slug)
        if existing and not FORCE_REBUILD and local_index.exists() and local_render_version(local_index) >= RENDER_VERSION:
            reuse_map[url] = existing
            continue
        jobs.append({
            "url": url,
            "slug": slug,
            "out_dir": item_root / slug,
            "local_url": local_url,
            "post": posts_by_url[url],
        })
    print(f"INFO: rendering {len(jobs)} new/updated articles (reusing {len(reuse_map)})")

    results = process_jobs(jobs)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = FEED_DESC
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "Blackstone Insights REST builder"

    output_items = []
    processed_count = 0

    for url in article_urls:
        slug = article_slug(url)
        local_url = f"{public_base}/item/{FEED_NAME}/{slug}/"
        if url in results:
            res = results[url]
            output_items.append({
                "title": res["title"],
                "link": local_url,
                "guid": local_url,
                "slug": slug,
                "pub_date": to_rfc822(res["date_raw"]),
                "description": res["summary"] or shorten(res["plain"]),
                "content_html": res["feed_html"],
                "source_url": res.get("source_url") or url,
                "date_human": human_date(res.get("date_raw") or ""),
            })
            processed_count += 1
            continue
        existing = reuse_map.get(url) or existing_slug_map.get(slug)
        if existing:
            output_items.append({
                "title": existing.get("title") or url,
                "link": existing.get("link") or local_url,
                "guid": existing.get("guid") or local_url,
                "slug": slug,
                "pub_date": existing.get("pub_date") or to_rfc822(""),
                "description": existing.get("description") or "",
                "content_html": existing.get("content_html") or "",
                "source_url": url,
            })
        else:
            print(f"WARN: no result and no existing copy for {url}; skipping")

    def _sort_key(item):
        try:
            return parsedate_to_datetime(item["pub_date"])
        except Exception:
            return datetime.now(timezone.utc)

    output_items.sort(key=_sort_key, reverse=True)

    # Self-heal: make sure every item we are about to publish has a local page on
    # disk so the feed's links never 404, even on runs that reused feed entries.
    ensure_local_pages(item_root, output_items)

    for item in output_items:
        rss_item = ET.SubElement(channel, "item")
        ET.SubElement(rss_item, "title").text = item["title"]
        ET.SubElement(rss_item, "link").text = item["link"]
        guid_el = ET.SubElement(rss_item, "guid")
        guid_el.set("isPermaLink", "true")
        guid_el.text = item["guid"]
        ET.SubElement(rss_item, "pubDate").text = item["pub_date"]
        ET.SubElement(rss_item, "description").text = item.get("description") or ""
        if item.get("content_html"):
            ET.SubElement(rss_item, CONTENT_ENCODED).text = item["content_html"]

    if processed_count == 0 and output_path.exists() and existing_items:
        print(f"no new {FEED_NAME} items; kept existing feed and pages")
        return
    if not output_items:
        print(f"WARN: produced 0 items for {FEED_NAME}")
        if output_path.exists():
            return

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    output_path.write_bytes(xml_bytes)
    print(f"Saved {output_path} (items={len(output_items)}, processed={processed_count})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_blackstone_insights_feed.py <site_dir> <public_base>")
        sys.exit(1)
    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    build_feed(site_dir, sys.argv[2])
