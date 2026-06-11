#!/usr/bin/env python3
"""EXPERIMENTAL DB Research builder: PDF -> reader-friendly semantic HTML.

Separate copy of build_dbresearch_feed.py used to research a better
PDF-on-the-web experience. Writes its own feed (dbresearch_lab.xml) and its own
item directory (item/dbresearch_lab/...); the production `dbresearch` feed and
pages are never touched.

Design notes
------------
RSS / read-later apps (Readwise Reader, etc.) do NOT render a page as-is: they
run a readability extractor that discards layout/styling and keeps only what it
recognizes as article text and images. Embedded SVG/<object> page reproductions
are therefore invisible to them. To make the content actually show up in a
reader we must emit *semantic* HTML:

  * real headings / paragraphs / lists (selectable, extractable text)
  * real HTML <table> elements
  * genuine charts / figures cropped to inline <img> (the only way charts
    survive a reader)

This builder produces that semantic HTML as the page body AND mirrors it into
<content:encoded> (with absolute image URLs) so readers receive the full article
including charts without depending on their own page parser.

Incremental model (same as production): restore the published feed + item pages
from live Pages, reuse already-localized items, and only fetch/render new ones.

See scripts/README_dbresearch_lab.md.
"""
import asyncio
import hashlib
import html
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse, urljoin
from xml.dom import minidom
from xml.etree import ElementTree as ET

import feedparser
import fitz  # PyMuPDF
import requests
from playwright.async_api import async_playwright

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", CONTENT_NS)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SOURCE_FEED_URL = os.environ.get(
    "DBRESEARCH_LAB_SOURCE_FEED_URL",
    "https://rssweball.top/feed/b706ef15-fa4f-45b2-a1fd-4cd8d037e91c.xml",
)
FEED_NAME = os.environ.get("DBRESEARCH_LAB_FEED_NAME", "dbresearch_lab")
OUTPUT_FILE = os.environ.get("DBRESEARCH_LAB_OUTPUT_FILE", f"{FEED_NAME}.xml")
MAX_ITEMS = int(os.environ.get("DBRESEARCH_LAB_MAX_ITEMS", "40"))
MAX_PAGES = int(os.environ.get("DBRESEARCH_LAB_MAX_PAGES", "80"))
REQUEST_TIMEOUT = int(os.environ.get("DBRESEARCH_LAB_TIMEOUT", "60"))
FIG_SCALE = float(os.environ.get("DBRESEARCH_LAB_FIG_SCALE", "2.0"))
USER_AGENT = os.environ.get(
    "DBRESEARCH_LAB_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT}
BROWSER_FALLBACK_ENABLED = os.environ.get("DBRESEARCH_LAB_BROWSER_FALLBACK", "1") != "0"
FORCE_REBUILD = os.environ.get("DBRESEARCH_LAB_FORCE_REBUILD", "0") == "1"

# Bump when rendering changes so published pages regenerate (from cached PDF).
RENDER_VERSION = 3


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def fetch_bytes(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.content


def slugify(value: str) -> str:
    value = html.unescape(value or "")
    value = unquote(value)
    value = re.sub(r"\.[Pp][Dd][Ff]$", "", value)
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def short_hash(*parts: str) -> str:
    base = "|".join(part or "" for part in parts)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:10]


def is_pdf_url(url: str) -> bool:
    return (urlparse(url).path or "").lower().endswith(".pdf")


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


def parse_pub_date(value: str) -> str:
    if not value:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = parsedate_to_datetime(value)
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def is_junk_line(line: str) -> bool:
    low = normalize_space(line).lower()
    if not low or len(low) <= 2:
        return True
    if re.fullmatch(r"\d{1,4}", low):
        return True
    if low.startswith("page ") or re.fullmatch(r"page\s+\d+", low):
        return True
    if low in {
        "deutsche bank research institute",
        "deutsche bank ag",
        "sensitivity: public",
        "source: deutsche bank research",
    }:
        return True
    if re.fullmatch(r"\d+\|\d+\|\d+", low):
        return True
    return False


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _area(r) -> float:
    return max(0.0, r.width) * max(0.0, r.height)


def _covers(big, small) -> bool:
    inter = big & small
    if inter.is_empty:
        return False
    return _area(inter) >= 0.6 * max(_area(small), 1.0)


def _merge_rects(rects):
    rects = [fitz.Rect(r) for r in rects if not fitz.Rect(r).is_empty]
    changed = True
    while changed:
        changed = False
        out = []
        while rects:
            r = rects.pop()
            merged = False
            for i, o in enumerate(out):
                if r.intersects(o):
                    out[i] = o | r
                    merged = True
                    changed = True
                    break
            if not merged:
                out.append(r)
        rects = out
    return rects


def collect_figure_rects(page, exclude_rects):
    parea = _area(page.rect)
    raw = []
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                for r in page.get_image_rects(xref):
                    raw.append(fitz.Rect(r))
            except Exception:
                pass
    except Exception:
        pass
    try:
        for r in page.cluster_drawings():
            raw.append(fitz.Rect(r))
    except Exception:
        pass
    cleaned = []
    for r in raw:
        r = fitz.Rect(r)
        if r.is_empty or r.width < 55 or r.height < 55:
            continue
        a = _area(r)
        if a > 0.92 * parea or a < 0.012 * parea:
            continue
        if any(_covers(ex, r) for ex in exclude_rects):
            continue
        cleaned.append(r)
    return _merge_rects(cleaned)


# --------------------------------------------------------------------------- #
# PDF fetching (direct -> page scrape -> browser capture; mirrors production)
# --------------------------------------------------------------------------- #
def try_fetch_binary_pdf(session: requests.Session, target_url: str, referer: str = "") -> bytes:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = session.get(target_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    ctype = (response.headers.get("content-type") or "").lower()
    if "pdf" in ctype and len(response.content) > 1000:
        return response.content
    raise ValueError(f"unexpected content-type for PDF binary: {ctype or 'unknown'}")


async def _fetch_pdf_via_browser(url: str) -> bytes:
    captured = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        async def handle_response(response):
            try:
                resp_url = response.url or ""
                ctype = (response.headers.get("content-type") or "").lower()
                if ".pdf" not in resp_url.lower() and "application/pdf" not in ctype:
                    return
                body = await response.body()
                if body and len(body) > 1000 and body[:4] == b"%PDF":
                    captured.append(body)
            except Exception:
                return

        page.on("response", handle_response)
        await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        await page.wait_for_timeout(3000)
        await browser.close()
    if captured:
        return max(captured, key=len)
    raise ValueError("browser fallback could not capture PDF response")


def fetch_pdf_bytes(url: str) -> bytes:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        return try_fetch_binary_pdf(session, url)
    except Exception:
        pass
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    page_html = response.text or ""
    m_pdf = re.search(r"var\s+pdfUrl\s*=\s*'([^']+)'", page_html)
    if m_pdf:
        direct = urljoin(response.url, html.unescape(m_pdf.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct, referer=response.url)
        except Exception:
            pass
    m_canon = re.search(r'<link\s+rel="canonical"\s+href="([^"]+\.pdf[^"]*)"', page_html, re.I)
    if m_canon:
        direct = urljoin(response.url, html.unescape(m_canon.group(1)))
        try:
            return try_fetch_binary_pdf(session, direct, referer=response.url)
        except Exception:
            pass
    if BROWSER_FALLBACK_ENABLED:
        try:
            print(f"INFO: browser PDF capture fallback for {url}")
            return asyncio.run(_fetch_pdf_via_browser(url))
        except Exception as exc:
            print(f"WARN: browser PDF capture failed for {url}: {exc}")
    raise ValueError("could not resolve real PDF binary URL")


# --------------------------------------------------------------------------- #
# Structured extraction: semantic elements (headings/paras/lists/tables/figs)
# --------------------------------------------------------------------------- #
def _table_to_html(table) -> str:
    try:
        data = table.extract()
    except Exception:
        return ""
    rows = [[normalize_space(c or "") for c in row] for row in (data or [])]
    rows = [r for r in rows if any(c for c in r)]
    if not rows or len(rows) < 1:
        return ""
    parts = ["<table>"]
    head, body = rows[0], rows[1:]
    parts.append("<thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in head) + "</tr></thead>")
    if body:
        parts.append("<tbody>")
        for r in body:
            parts.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
        parts.append("</tbody>")
    parts.append("</table>")
    return "".join(parts)


def extract_pdf_structured(pdf_bytes: bytes, out_dir: Path, max_pages: int = MAX_PAGES):
    """Return (elements, plain_paragraphs).

    elements is an ordered list of tuples used to render both the page body and
    the feed <content:encoded>:
        ("h2", text) ("h3", text) ("p", text) ("ul", [items])
        ("table", html) ("figure", filename, caption)
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    elements = []
    plain = []
    seen_block = set()
    fig_n = 0
    try:
        total = min(len(doc), max_pages)
        sizes = []
        for pi in range(total):
            for b in doc.load_page(pi).get_text("dict").get("blocks", []):
                for ln in b.get("lines", []):
                    for sp in ln.get("spans", []):
                        if sp.get("text", "").strip():
                            sizes.append(sp.get("size", 0.0))
        median = statistics.median(sizes) if sizes else 10.0

        for pi in range(total):
            page = doc.load_page(pi)

            table_items, table_rects = [], []
            try:
                for t in page.find_tables().tables:
                    th = _table_to_html(t)
                    if th:
                        rr = fitz.Rect(t.bbox)
                        table_items.append((rr, th))
                        table_rects.append(rr)
            except Exception:
                pass

            fig_items = []
            for r in collect_figure_rects(page, table_rects):
                try:
                    clip = r & page.rect
                    if clip.is_empty:
                        continue
                    fig_n += 1
                    pix = page.get_pixmap(matrix=fitz.Matrix(FIG_SCALE, FIG_SCALE), clip=clip, alpha=False)
                    name = f"fig-{fig_n:03d}.png"
                    (out_dir / name).write_bytes(pix.tobytes("png"))
                    fig_items.append((r, name))
                except Exception:
                    pass

            positioned = []
            for b in page.get_text("dict").get("blocks", []):
                if b.get("type") != 0:
                    continue
                bbox = fitz.Rect(b.get("bbox"))
                if any(_covers(rr, bbox) for rr in table_rects):
                    continue
                if any(_covers(rr, bbox) for rr, _ in fig_items):
                    continue
                line_texts, line_sizes, bold_flags = [], [], []
                for ln in b.get("lines", []):
                    spans = ln.get("spans", [])
                    t = normalize_space("".join(sp.get("text", "") for sp in spans))
                    if not t or is_junk_line(t):
                        continue
                    line_texts.append(t)
                    line_sizes.append(max((sp.get("size", 0.0) for sp in spans), default=median))
                    bold_flags.append(any((sp.get("flags", 0) & 16) for sp in spans))
                if not line_texts:
                    continue
                block_text = normalize_space(" ".join(line_texts))
                if len(block_text) < 80 and block_text.lower() in seen_block:
                    continue
                if len(block_text) < 80:
                    seen_block.add(block_text.lower())
                big = max(line_sizes) if line_sizes else median
                bold = sum(bold_flags) >= max(1, len(bold_flags) // 2)
                y0, x0 = bbox.y0, bbox.x0
                bullet = re.compile(r"^([\u2022\u25aa\u00b7\u2013\u2014\-\*]|\d+[.)])\s+")
                if big >= 1.45 * median and len(block_text) < 200:
                    positioned.append((y0, x0, ("h2", block_text)))
                elif (big >= 1.18 * median or bold) and len(block_text) < 160:
                    positioned.append((y0, x0, ("h3", block_text)))
                elif len(line_texts) >= 2 and all(bullet.match(t) for t in line_texts):
                    items = [bullet.sub("", t).strip() for t in line_texts]
                    positioned.append((y0, x0, ("ul", items)))
                else:
                    positioned.append((y0, x0, ("p", block_text)))

            for rr, th in table_items:
                positioned.append((rr.y0, rr.x0, ("table", th)))
            for rr, name in fig_items:
                positioned.append((rr.y0, rr.x0, ("figure", name, "")))

            positioned.sort(key=lambda e: (round(e[0], 1), e[1]))
            for _, _, el in positioned:
                elements.append(el)
                if el[0] in ("h2", "h3", "p") and len(el[1]) >= 24:
                    plain.append(el[1])
                elif el[0] == "ul":
                    plain.extend(x for x in el[1] if len(x) >= 24)
    finally:
        doc.close()
    return elements, plain


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_elements(elements: list, img_prefix: str = "") -> str:
    parts = []
    for el in elements:
        kind = el[0]
        if kind == "h2":
            parts.append(f"<h2>{html.escape(el[1])}</h2>")
        elif kind == "h3":
            parts.append(f"<h3>{html.escape(el[1])}</h3>")
        elif kind == "p":
            parts.append(f"<p>{html.escape(el[1])}</p>")
        elif kind == "ul":
            lis = "".join(f"<li>{html.escape(x)}</li>" for x in el[1])
            parts.append(f"<ul>{lis}</ul>")
        elif kind == "table":
            parts.append(f'<div class="tablewrap">{el[1]}</div>')
        elif kind == "figure":
            src = html.escape(img_prefix + el[1])
            cap = html.escape(el[2]) if len(el) > 2 and el[2] else ""
            cap_html = f"<figcaption>{cap}</figcaption>" if cap else ""
            parts.append(f'<figure><img src="{src}" alt="{cap or "figure"}" loading="lazy">{cap_html}</figure>')
    return "\n".join(parts)


PAGE_CSS = """
  body { font-family: Georgia, "Times New Roman", serif; margin: 0; color: #1a1a1a; background: #fff; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 18px 72px; line-height: 1.7; }
  h1 { font-size: 1.7em; line-height: 1.25; margin: 0 0 14px; }
  h2 { font-size: 1.3em; margin: 1.5em 0 0.5em; }
  h3 { font-size: 1.1em; margin: 1.2em 0 0.4em; }
  p { margin: 0.7em 0; }
  ul { margin: 0.6em 0 0.6em 1.2em; }
  figure { margin: 1.2em 0; text-align: center; }
  figure img { max-width: 100%; height: auto; border: 1px solid #eee; border-radius: 4px; }
  figcaption { font-size: 0.85em; color: #666; margin-top: 6px; }
  .tablewrap { overflow-x: auto; margin: 1.1em 0; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
  thead th { background: #f5f5f5; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 24px; font-family: -apple-system, sans-serif; }
  .btn { display: inline-block; padding: 8px 13px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; background: #fff; font-size: 0.9em; }
  .btn.primary { background: #111; color: #fff; border-color: #111; }
"""


def build_local_page(title: str, source_link: str, content_html: str, pdf_href) -> str:
    open_href = html.escape(pdf_href or source_link)
    body = content_html or "<p>Content could not be extracted from this PDF.</p>"
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        f'  <meta name="render-version" content="{RENDER_VERSION}">',
        f"  <title>{html.escape(title)}</title>",
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f"  <style>{PAGE_CSS}</style>",
        "</head>",
        "<body>",
        '  <article class="wrap">',
        f"    <h1>{html.escape(title)}</h1>",
        '    <div class="actions">',
        f'      <a class="btn primary" href="{open_href}" target="_blank" rel="noopener">Open original PDF</a>',
        f'      <a class="btn" href="{html.escape(source_link)}" target="_blank" rel="noopener">Source</a>',
        '    </div>',
        body,
        '  </article>',
        "</body>",
        "</html>",
    ])


# --------------------------------------------------------------------------- #
# Incremental state (restore live feed + item pages, same model as production)
# --------------------------------------------------------------------------- #
def entry_slug(title: str, link: str, guid: str) -> str:
    path = (urlparse(link).path or "").strip("/")
    leaf = unquote(path.split("/")[-1]) if path else ""
    base = slugify(leaf) if leaf else slugify(title)
    return f"{base}-{short_hash(guid, link, title)}"


def parse_existing_feed(xml_path: Path) -> list:
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
            "content_html": (item.findtext(f"{CONTENT_NS}encoded") or ""),
            "slug": slug,
        })
    return items


def restore_live_feed(public_base: str, site_dir: Path) -> bool:
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
            refs = set(re.findall(r'(?:src|href|data)="([^"]+)"', html_text))
            for ref in refs:
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


def load_existing_items(site_dir: Path, public_base: str) -> list:
    output_path = site_dir / OUTPUT_FILE
    if not output_path.exists():
        restore_live_feed(public_base, site_dir)
    if not output_path.exists():
        return []
    try:
        return parse_existing_feed(output_path)
    except Exception:
        return []


def local_render_version(index_path: Path) -> int:
    try:
        txt = index_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    m = re.search(r'name="render-version"\s+content="(\d+)"', txt)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------- #
# Feed assembly
# --------------------------------------------------------------------------- #
def build_feed(site_dir: Path, public_base: str):
    print(f"Fetching source feed: {SOURCE_FEED_URL}")
    parsed = feedparser.parse(SOURCE_FEED_URL)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"failed to parse source feed: {getattr(parsed, 'bozo_exception', 'unknown')}")

    public_base = public_base.rstrip("/")
    item_root = site_dir / "item" / FEED_NAME
    item_root.mkdir(parents=True, exist_ok=True)
    output_path = site_dir / OUTPUT_FILE

    existing_items = load_existing_items(site_dir, public_base)
    existing_guid_map = {it["guid"]: it for it in existing_items if it.get("guid")}
    existing_slug_map = {it["slug"]: it for it in existing_items if it.get("slug")}

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:content", CONTENT_NS)
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = (parsed.feed.get("title") or "DB Research") + " (lab)"
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = "Experimental DB Research feed: reader-friendly semantic HTML with inline charts"
    ET.SubElement(channel, "language").text = parsed.feed.get("language") or "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "DBResearch lab semantic builder"

    output_items = []
    total_count = 0
    processed_count = 0
    pdf_count = 0

    for entry in parsed.entries[:MAX_ITEMS]:
        title = normalize_space(entry.get("title", "Untitled")) or "Untitled"
        link = entry.get("link", "").strip()
        description = normalize_space(entry.get("summary", "") or entry.get("description", ""))
        guid = (entry.get("id") or entry.get("guid") or link or title).strip()
        pub_date = parse_pub_date(entry.get("published", "") or entry.get("updated", ""))

        slug = entry_slug(title, link, guid) if is_pdf_url(link) else None
        existing_item = existing_guid_map.get(guid)
        if not existing_item and slug:
            existing_item = existing_slug_map.get(slug)

        reuse = False
        if existing_item and not FORCE_REBUILD:
            if is_pdf_url(link) and slug:
                existing_link = (existing_item.get("link") or "").strip()
                local_index = item_root / slug / "index.html"
                if (existing_link.startswith(f"{public_base}/item/{FEED_NAME}/")
                        and local_index.exists()
                        and local_render_version(local_index) >= RENDER_VERSION):
                    reuse = True
                elif existing_link.startswith(f"{public_base}/item/{FEED_NAME}/") and local_index.exists():
                    print(f"INFO: upgrading {link} to render v{RENDER_VERSION} from cached PDF")
                else:
                    print(f"INFO: rebuilding missing local page for {link}")
            else:
                reuse = True

        if reuse:
            output_items.append({
                "title": existing_item.get("title") or title,
                "link": existing_item.get("link") or link,
                "guid": existing_item.get("guid") or guid,
                "pub_date": existing_item.get("pub_date") or pub_date,
                "description": existing_item.get("description") or description or shorten(title),
                "content_html": existing_item.get("content_html") or "",
                "is_permalink": bool((existing_item.get("link") or "").startswith("http")),
            })
            total_count += 1
            continue

        final_link, final_guid = link, guid
        is_permalink = bool(guid == link and link.startswith("http"))
        content_html = ""

        if is_pdf_url(link) and slug:
            out_dir = item_root / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            local_url = f"{public_base}/item/{FEED_NAME}/{slug}/" if public_base else link
            cached_pdf = out_dir / "original.pdf"

            pdf_bytes = None
            if cached_pdf.exists() and cached_pdf.stat().st_size > 1000:
                pdf_bytes = cached_pdf.read_bytes()
                print(f"INFO: reusing cached PDF (no download) for {link}")
            else:
                try:
                    pdf_bytes = fetch_pdf_bytes(link)
                    cached_pdf.write_bytes(pdf_bytes)
                except Exception as exc:
                    print(f"WARN: fetch PDF failed for {link}: {exc}")

            elements, plain = [], []
            if pdf_bytes:
                for old in (list(out_dir.glob("fig-*.png")) + list(out_dir.glob("page-*.svg"))
                            + list(out_dir.glob("page-*.png")) + list(out_dir.glob("p[0-9]*-*.png"))):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                try:
                    elements, plain = extract_pdf_structured(pdf_bytes, out_dir)
                except Exception as exc:
                    print(f"WARN: structured extraction failed for {link}: {exc}")

            if not description:
                description = shorten(plain[0] if plain else title)

            page_body = render_elements(elements, img_prefix="")
            content_html = render_elements(elements, img_prefix=local_url)
            (out_dir / "index.html").write_text(
                build_local_page(title, link, page_body, "original.pdf" if pdf_bytes else None),
                encoding="utf-8",
            )
            final_link = final_guid = local_url
            is_permalink = True
            pdf_count += 1
        elif not description:
            description = shorten(title)

        processed_count += 1
        total_count += 1
        output_items.append({
            "title": title,
            "link": final_link,
            "guid": final_guid,
            "pub_date": pub_date,
            "description": description,
            "content_html": content_html,
            "is_permalink": is_permalink,
        })

    for item in output_items:
        rss_item = ET.SubElement(channel, "item")
        ET.SubElement(rss_item, "title").text = item["title"]
        ET.SubElement(rss_item, "link").text = item["link"]
        guid_el = ET.SubElement(rss_item, "guid")
        guid_el.set("isPermaLink", "true" if item["is_permalink"] else "false")
        guid_el.text = item["guid"]
        ET.SubElement(rss_item, "pubDate").text = item["pub_date"]
        ET.SubElement(rss_item, "description").text = item.get("description") or ""
        if item.get("content_html"):
            ET.SubElement(rss_item, f"{CONTENT_NS}encoded").text = item["content_html"]

    if processed_count == 0 and output_path.exists():
        print(f"no new {FEED_NAME} items; kept existing feed and pages")
        return

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    output_path.write_bytes(xml_bytes)
    print(f"Saved {output_path} (items={total_count}, processed={processed_count}, pdf_localized={pdf_count})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dbresearch_lab_feed.py <site_dir> <public_base>")
        sys.exit(1)
    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    build_feed(site_dir, sys.argv[2])
