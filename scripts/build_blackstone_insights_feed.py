#!/usr/bin/env python3
"""Blackstone Insights builder: render article pages -> reader-friendly HTML.

WHY A BROWSER IS REQUIRED
-------------------------
Blackstone has aggressive bot protection (Akamai-style), client-side (JS)
rendering, and a residency-confirmation interstitial. A plain `requests` fetch
from CI gets blocked / gated / served a JS shell with no article body. The only
reliable way to get the content is to drive a real headless browser, dismiss the
gate, let the page render, scroll to load everything, then read the DOM.

TEXT vs CHARTS
--------------
* Text: we walk the rendered article in document order and emit semantic HTML
  for every heading / paragraph / list / blockquote / table -> full article.
* Charts: Blackstone charts are drawn with JS (SVG/canvas), so they are NOT
  text and NOT <img> files -- no readability extractor can ever capture them.
  We therefore SCREENSHOT each chart/graphic element to a PNG and embed it as an
  inline <img>, interleaved in the correct position.

Output:
* site/blackstone_insights.xml : items link to local pages and carry the full
  article in <content:encoded> (absolute image URLs).
* site/item/blackstone_insights/<slug>/index.html (+ fig-NNN.png chart shots).

Incremental: restore the published feed + item pages from live Pages, reuse
already-localized articles, only render newly added ones. Bump RENDER_VERSION
to force regeneration.

Usage: python scripts/build_blackstone_insights_feed.py <site_dir> <public_base>
"""
import asyncio
import html
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.dom import minidom
from xml.etree import ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
CONTENT_ENCODED = "{" + CONTENT_NS + "}encoded"
ET.register_namespace("content", CONTENT_NS)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SITE_BASE = "https://www.blackstone.com"
SOURCE_FEED_URL = os.environ.get("BLACKSTONE_SOURCE_FEED_URL", "https://www.blackstone.com/insights/feed/")
LIST_URL = os.environ.get("BLACKSTONE_LIST_URL", "https://www.blackstone.com/insights/")
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ARTICLE_RE = re.compile(r"https://www\.blackstone\.com/insights/article/[a-z0-9\-]+/", re.I)
GATE_MARKERS = ("i am not a united states resident", "i am a united states resident")

# Bump when rendering changes so published pages regenerate on the next run.
RENDER_VERSION = 3

BLOCK_OK = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "blockquote",
    "figure", "figcaption", "table", "thead", "tbody", "tfoot", "tr", "th",
    "td", "caption",
}
INLINE_OK = {"strong", "em", "b", "i", "u", "a", "br", "sup", "sub", "code"}
DROP = {"script", "style", "noscript", "svg", "form", "button", "iframe", "nav", "aside", "header", "footer"}
TABLE_OK = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption"}

# --------------------------------------------------------------------------- #
# Browser-side JS
# --------------------------------------------------------------------------- #
META_JS = """() => {
  const pick = (s) => { const e = document.querySelector(s); return e ? (e.content || '') : ''; };
  return {
    title: pick('meta[property=\"og:title\"]') || document.title || '',
    desc: pick('meta[property=\"og:description\"]') || pick('meta[name=\"description\"]') || '',
    date: pick('meta[property=\"article:published_time\"]') || pick('meta[property=\"article:modified_time\"]') || ''
  };
}"""

WALK_JS = """() => {
  const root = document.querySelector('article') || document.querySelector('main') || document.body;
  if (!root) return [];
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','HEADER','ASIDE','FORM','BUTTON','INPUT','SELECT','TEXTAREA']);
  const out = [];
  let idx = 0;
  function chartAncestor(el){
    let best = el, p = el.parentElement, hops = 0;
    while (p && p !== root && hops < 4){
      const c = (p.className || '') + '';
      if (/chart|graphic|visual|exhibit|figure|highcharts|tableau|d3/i.test(c)) best = p;
      p = p.parentElement; hops++;
    }
    return best;
  }
  function tagGraphic(el, caption){
    const t = chartAncestor(el);
    const r = t.getBoundingClientRect();
    if (r.width < 80 || r.height < 60) return false;
    if (t.getAttribute('data-bx-idx')) return true;
    idx++; t.setAttribute('data-bx-idx', String(idx));
    out.push({type:'graphic', idx: idx, caption: caption || ''});
    return true;
  }
  function walk(node){
    for (const el of node.children){
      const tag = el.tagName;
      if (skip.has(tag)) continue;
      let st = null;
      try { st = window.getComputedStyle(el); } catch (e) {}
      if (st && (st.display === 'none' || st.visibility === 'hidden')) continue;
      if (tag === 'SVG' || tag === 'svg' || tag === 'CANVAS'){ tagGraphic(el, ''); continue; }
      if (tag === 'IMG'){
        const src = el.currentSrc || el.src || '';
        const r = el.getBoundingClientRect();
        if (src && r.width >= 80 && r.height >= 50) out.push({type:'img', src: src, alt: el.alt || ''});
        continue;
      }
      if (tag === 'FIGURE'){
        const cf = el.querySelector('figcaption');
        const cap = cf ? cf.innerText.trim() : '';
        const g = el.querySelector('svg, canvas');
        if (g){ if (tagGraphic(g, cap)) continue; }
        const im = el.querySelector('img');
        if (im){ const src = im.currentSrc || im.src || ''; if (src){ out.push({type:'img', src: src, alt: im.alt || cap}); continue; } }
        walk(el); continue;
      }
      if (tag === 'TABLE'){ out.push({type:'table', html: el.outerHTML}); continue; }
      if (tag === 'UL' || tag === 'OL'){
        const items = Array.from(el.querySelectorAll(':scope > li')).map(li => li.innerText.trim()).filter(Boolean);
        if (items.length) out.push({type:'list', ordered: tag === 'OL', items: items});
        continue;
      }
      if (tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'H5' || tag === 'H6'){
        const txt = el.innerText.trim();
        if (txt) out.push({type:'heading', level: Math.min(parseInt(tag[1]), 4), text: txt});
        continue;
      }
      if (tag === 'P' || tag === 'BLOCKQUOTE'){
        const txt = el.innerText.trim();
        if (txt) out.push({type: tag === 'P' ? 'p' : 'blockquote', text: txt});
        continue;
      }
      walk(el);
    }
  }
  walk(root);
  return out;
}"""

AUTOSCROLL_JS = """() => new Promise((resolve) => {
  let total = 0; const step = 700;
  const timer = setInterval(() => {
    window.scrollBy(0, step); total += step;
    if (total >= document.body.scrollHeight - window.innerHeight - step) { clearInterval(timer); resolve(); }
  }, 180);
  setTimeout(() => { clearInterval(timer); resolve(); }, 9000);
})"""


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
    value = re.sub(r"<[^>]+>", " ", value or "")
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


def sanitize_table(html_str: str) -> str:
    try:
        soup = BeautifulSoup(html_str or "", "html.parser")
    except Exception:
        return ""
    table = soup.find("table")
    if not table:
        return ""
    for tag in table.find_all(True):
        if tag.name in TABLE_OK:
            tag.attrs = {}
        else:
            tag.unwrap()
    table.attrs = {}
    text = strip_tags(str(table))
    if len(text) < 4:
        return ""
    return f'<div class="tablewrap">{table}</div>'


def looks_like_gate(plain: str, raw_lower: str) -> bool:
    hits = sum(1 for m in GATE_MARKERS if m in raw_lower)
    return hits >= 1 and len(plain) < 600


# --------------------------------------------------------------------------- #
# Block -> HTML
# --------------------------------------------------------------------------- #
def blocks_to_html(blocks, img_prefix: str) -> str:
    parts = []
    for b in blocks:
        if b.get("drop"):
            continue
        t = b.get("type")
        if t == "heading":
            lvl = max(2, min(4, int(b.get("level", 2) or 2)))
            tx = normalize_space(b.get("text", ""))
            if tx:
                parts.append(f"<h{lvl}>{html.escape(tx)}</h{lvl}>")
        elif t == "p":
            tx = normalize_space(b.get("text", ""))
            if tx:
                parts.append(f"<p>{html.escape(tx)}</p>")
        elif t == "blockquote":
            tx = normalize_space(b.get("text", ""))
            if tx:
                parts.append(f"<blockquote>{html.escape(tx)}</blockquote>")
        elif t == "list":
            tag = "ol" if b.get("ordered") else "ul"
            lis = "".join(
                f"<li>{html.escape(normalize_space(x))}</li>"
                for x in b.get("items", []) if normalize_space(x)
            )
            if lis:
                parts.append(f"<{tag}>{lis}</{tag}>")
        elif t == "table":
            tb = sanitize_table(b.get("html", ""))
            if tb:
                parts.append(tb)
        elif t == "img":
            src = b.get("src", "")
            if src.startswith("http"):
                alt = html.escape(b.get("alt") or "", quote=True)
                parts.append(f'<figure><img src="{html.escape(src, quote=True)}" alt="{alt}" loading="lazy"></figure>')
        elif t == "graphic" and b.get("file"):
            src = img_prefix + b["file"]
            cap = html.escape(normalize_space(b.get("caption") or ""))
            cap_html = f"<figcaption>{cap}</figcaption>" if cap else ""
            parts.append(f'<figure><img src="{html.escape(src, quote=True)}" alt="{cap or "chart"}" loading="lazy">{cap_html}</figure>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Browser discovery + rendering/extraction/screenshots
# --------------------------------------------------------------------------- #
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


async def _discover_browser():
    from playwright.async_api import async_playwright
    urls, seen = [], set()
    async with async_playwright() as p:
        browser, context = await _new_context(p)
        page = await context.new_page()
        try:
            await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
            await _dismiss_gate(page)
            await page.wait_for_timeout(2500)
            for _ in range(3):
                try:
                    more = await page.query_selector("text=Load More")
                    if not more:
                        break
                    await more.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    break
            hrefs = await page.eval_on_selector_all(
                'a[href*="/insights/article/"]', "els => els.map(e => e.href)"
            )
            for href in hrefs or []:
                m = ARTICLE_RE.match((href or "").split("?")[0].split("#")[0])
                if m and m.group(0) not in seen:
                    seen.add(m.group(0))
                    urls.append(m.group(0))
        finally:
            await browser.close()
    return urls[:MAX_ITEMS]


async def _process_async(jobs):
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        browser, context = await _new_context(p)
        page = await context.new_page()
        try:
            for job in jobs:
                url, out_dir, local_url = job["url"], job["out_dir"], job["local_url"]
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT * 1000)
                    await _dismiss_gate(page)
                    await page.wait_for_timeout(2000)
                    try:
                        await page.wait_for_selector("article, main, h1", timeout=8000)
                    except Exception:
                        pass
                    try:
                        await page.evaluate(AUTOSCROLL_JS)
                        await page.evaluate("window.scrollTo(0, 0)")
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass

                    meta = await page.evaluate(META_JS)
                    blocks = await page.evaluate(WALK_JS)

                    out_dir.mkdir(parents=True, exist_ok=True)
                    for old in out_dir.glob("fig-*.png"):
                        try:
                            old.unlink()
                        except Exception:
                            pass

                    fig_i = 0
                    for b in blocks:
                        if b.get("type") != "graphic":
                            continue
                        if fig_i >= MAX_FIGS:
                            b["drop"] = True
                            continue
                        sel = '[data-bx-idx="' + str(b.get("idx")) + '"]'
                        el = await page.query_selector(sel)
                        if not el:
                            b["drop"] = True
                            continue
                        try:
                            await el.scroll_into_view_if_needed(timeout=4000)
                            await page.wait_for_timeout(450)
                            fig_i += 1
                            name = f"fig-{fig_i:03d}.png"
                            buf = await el.screenshot(timeout=9000)
                            (out_dir / name).write_bytes(buf)
                            b["file"] = name
                        except Exception as exc:
                            print(f"WARN: chart screenshot failed {url} #{b.get('idx')}: {exc}")
                            b["drop"] = True

                    title = normalize_space(re.sub(r"\s*[-|]\s*Blackstone\s*$", "", meta.get("title") or "")) or "Untitled"
                    page_html = blocks_to_html(blocks, "")
                    feed_html = blocks_to_html(blocks, local_url)
                    plain = strip_tags(page_html)
                    n_figs = sum(1 for b in blocks if b.get("file"))
                    summary = normalize_space(meta.get("desc") or "") or shorten(plain)
                    date_raw = meta.get("date") or ""

                    print(f"INFO: {url} -> text={len(plain)}chars figs={n_figs} blocks={len(blocks)}")
                    if len(plain) < 60 and n_figs == 0:
                        print(f"WARN: empty extraction for {url}")
                        continue

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
                    }
                except Exception as exc:
                    print(f"WARN: processing failed for {url}: {exc}")
        finally:
            await browser.close()
    return results


# --------------------------------------------------------------------------- #
# Requests fallback (text-only; no charts) used if the browser is unavailable
# --------------------------------------------------------------------------- #
def meta_prop(soup, prop):
    tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag and tag.get("content") else ""


def meta_name(soup, name):
    tag = soup.find("meta", attrs={"name": name})
    return (tag.get("content") or "").strip() if tag and tag.get("content") else ""


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


def pick_content_node(soup):
    scores = defaultdict(float)
    for el in soup.find_all(["p", "li", "blockquote", "h2", "h3", "h4"]):
        text = el.get_text(" ", strip=True)
        if len(text) < 25:
            continue
        score = 1.0 + min(len(text) / 100.0, 5.0)
        node, weight = el.parent, 1.0
        for _ in range(3):
            if node is None or not isinstance(node, Tag):
                break
            scores[node] += score * weight
            weight *= 0.5
            node = node.parent
    if not scores:
        return soup.body or soup
    return max(scores, key=lambda n: scores[n])


def extract_article_requests(html_text, url):
    soup = BeautifulSoup(html_text, "html.parser")
    title = meta_prop(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    title = normalize_space(re.sub(r"\s*[-|]\s*Blackstone\s*$", "", title or "")) or "Untitled"
    summary = normalize_space(meta_prop(soup, "og:description") or meta_name(soup, "description"))
    date_raw = meta_prop(soup, "article:published_time") or meta_prop(soup, "article:modified_time")
    for tag in soup.find_all(list(DROP)):
        tag.decompose()
    node = pick_content_node(soup)
    content_html = serialize_children(node, url) if node else ""
    plain = strip_tags(content_html)
    if not summary:
        summary = shorten(plain)
    return {"title": title, "summary": summary, "date_raw": date_raw, "content_html": content_html, "plain": plain}


def fallback_process(session, jobs):
    results = {}
    for job in jobs:
        url, out_dir = job["url"], job["out_dir"]
        try:
            r = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            art = extract_article_requests(r.text, url)
        except Exception as exc:
            print(f"WARN: fallback fetch failed for {url}: {exc}")
            continue
        gated = looks_like_gate(art["plain"], (r.text or "").lower())
        print(f"INFO(fallback): {url} -> text={len(art['plain'])}chars gate={gated}")
        if gated or len(art["plain"]) < 120:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(
            build_local_page(art["title"], url, art["content_html"], human_date(art["date_raw"])),
            encoding="utf-8",
        )
        results[url] = {
            "title": art["title"],
            "summary": art["summary"],
            "date_raw": art["date_raw"],
            "feed_html": art["content_html"],
            "plain": art["plain"],
        }
    return results


def discover_articles(session):
    if BROWSER_ENABLED:
        try:
            urls = asyncio.run(_discover_browser())
            if urls:
                print(f"INFO: discovered {len(urls)} articles via browser")
                return urls
            print("WARN: browser discovered 0 articles; trying requests")
        except Exception as exc:
            print(f"WARN: browser discover unavailable ({exc}); trying requests")
    urls, seen = [], set()
    try:
        raw = session.get(SOURCE_FEED_URL, headers=HEADERS, timeout=30).text
        for entry in feedparser.parse(raw).entries:
            link = (entry.get("link") or "").strip().split("?")[0]
            if "/insights/article/" in link and link not in seen:
                seen.add(link)
                urls.append(link)
    except Exception as exc:
        print(f"INFO: requests feed discover failed: {exc}")
    if len(urls) < MAX_ITEMS:
        try:
            listing = session.get(LIST_URL, headers=HEADERS, timeout=30).text
            for match in ARTICLE_RE.findall(listing):
                if match not in seen:
                    seen.add(match)
                    urls.append(match)
        except Exception as exc:
            print(f"WARN: requests listing discover failed: {exc}")
    return urls[:MAX_ITEMS]


def process_jobs(session, jobs):
    if not jobs:
        return {}
    if BROWSER_ENABLED:
        try:
            res = asyncio.run(_process_async(jobs))
            if res:
                return res
            print("WARN: browser produced 0 results; trying requests fallback")
        except Exception as exc:
            print(f"WARN: browser processing unavailable ({exc}); trying requests fallback")
    return fallback_process(session, jobs)


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
        '    <div class="actions">',
        f'      <a class="btn primary" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noopener">View on Blackstone</a>',
        "    </div>",
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

    article_urls = discover_articles(session)
    if not article_urls:
        if output_path.exists():
            print("no article URLs discovered; kept existing feed and pages")
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
        jobs.append({"url": url, "slug": slug, "out_dir": item_root / slug, "local_url": local_url})
    print(f"INFO: rendering {len(jobs)} new/updated articles (reusing {len(reuse_map)})")

    results = process_jobs(session, jobs)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = FEED_DESC
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "Blackstone Insights browser builder"

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
                "pub_date": to_rfc822(res["date_raw"]),
                "description": res["summary"] or shorten(res["plain"]),
                "content_html": res["feed_html"],
            })
            processed_count += 1
            continue
        existing = reuse_map.get(url) or existing_slug_map.get(slug)
        if existing:
            output_items.append({
                "title": existing.get("title") or url,
                "link": existing.get("link") or local_url,
                "guid": existing.get("guid") or local_url,
                "pub_date": existing.get("pub_date") or to_rfc822(""),
                "description": existing.get("description") or "",
                "content_html": existing.get("content_html") or "",
            })
        else:
            print(f"WARN: no result and no existing copy for {url}; skipping")

    def _sort_key(item):
        try:
            return parsedate_to_datetime(item["pub_date"])
        except Exception:
            return datetime.now(timezone.utc)

    output_items.sort(key=_sort_key, reverse=True)

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
