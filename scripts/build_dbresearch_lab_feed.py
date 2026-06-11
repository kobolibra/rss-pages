#!/usr/bin/env python3
"""EXPERIMENTAL DB Research builder: PDF -> structured, figure-preserving HTML.

This is a SEPARATE copy of build_dbresearch_feed.py for researching better PDF
body-text extraction. It writes a different feed (dbresearch_lab.xml) and a
different item directory (item/dbresearch_lab/...), so the production
`dbresearch` feed and pages are never touched.

Strategy (CPU-only / CI-friendly), per page:
  1. find_tables()        -> real <table> HTML, bbox suppresses duplicate prose
  2. images + drawings    -> cropped <figure> PNGs embedded in context
  3. get_text("dict")     -> reading-order text, heading levels from font size
  4. near-empty page      -> single full-page raster fallback (optional OCR hook)

See scripts/README_dbresearch_lab.md for details and heavier alternatives.
"""
import asyncio
import hashlib
import html
import io
import os
import re
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
REQUEST_TIMEOUT = int(os.environ.get("DBRESEARCH_LAB_TIMEOUT", "60"))
FIG_SCALE = float(os.environ.get("DBRESEARCH_LAB_FIG_SCALE", "2.0"))
MIN_PAGE_TEXT = int(os.environ.get("DBRESEARCH_LAB_MIN_PAGE_TEXT", "40"))
RASTER_FALLBACK = os.environ.get("DBRESEARCH_LAB_RASTER_FALLBACK", "1") != "0"
MAX_PAGES = int(os.environ.get("DBRESEARCH_LAB_MAX_PAGES", "80"))
USER_AGENT = os.environ.get(
    "DBRESEARCH_LAB_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT}
BROWSER_FALLBACK_ENABLED = os.environ.get("DBRESEARCH_LAB_BROWSER_FALLBACK", "1") != "0"


# --------------------------------------------------------------------------- #
# Small helpers (shared shape with production script)
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
# PDF fetching (mirrors production: direct -> page scrape -> browser capture)
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
    captured: list[bytes] = []
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
# Structured extraction (the experiment)
# --------------------------------------------------------------------------- #
def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _body_font_size(doc) -> float:
    sizes: list[float] = []
    for page in doc[: min(len(doc), 5)]:
        try:
            data = page.get_text("dict")
        except Exception:
            continue
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if (span.get("text") or "").strip():
                        sizes.append(round(float(span.get("size", 0)), 1))
    return _median(sizes) or 10.0


def _table_to_html(tab) -> str:
    try:
        rows = tab.extract()
    except Exception:
        return ""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    out = ['<table class="tbl">']
    for ri, row in enumerate(rows):
        tag = "th" if ri == 0 else "td"
        cells = "".join(f"<{tag}>{html.escape((c or '').strip())}</{tag}>" for c in row)
        out.append(f"  <tr>{cells}</tr>")
    out.append("</table>")
    return "\n".join(out)


def _figure_rects(page) -> list[tuple]:
    page_rect = page.rect
    page_area = max(page_rect.width * page_rect.height, 1.0)
    rects: list[tuple] = []
    # embedded raster images
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            for r in page.get_image_rects(xref):
                rects.append((r.x0, r.y0, r.x1, r.y1))
    except Exception:
        pass
    # clustered vector drawings (charts)
    try:
        for r in page.cluster_drawings():
            rects.append((r.x0, r.y0, r.x1, r.y1))
    except Exception:
        pass
    cleaned: list[tuple] = []
    for r in rects:
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 40 or h < 40:
            continue
        if (w * h) / page_area > 0.92:  # whole-page => likely text, skip
            continue
        cleaned.append(r)
    # merge overlapping figure rects
    merged: list[list] = []
    for r in sorted(cleaned, key=lambda x: (x[1], x[0])):
        placed = False
        for m in merged:
            if _overlap(r, m):
                m[0], m[1] = min(m[0], r[0]), min(m[1], r[1])
                m[2], m[3] = max(m[2], r[2]), max(m[3], r[3])
                placed = True
                break
        if not placed:
            merged.append(list(r))
    return [tuple(m) for m in merged]


def _text_block_html(block, body_size: float):
    text_lines, sizes, bold = [], [], False
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            t = span.get("text", "")
            if t:
                parts.append(t)
                sizes.append(float(span.get("size", body_size)))
                if int(span.get("flags", 0)) & 16:  # bold flag
                    bold = True
        line_text = normalize_space("".join(parts))
        if line_text:
            text_lines.append(line_text)
    text = normalize_space(" ".join(text_lines))
    if not text or is_junk_line(text):
        return None
    max_size = max(sizes) if sizes else body_size
    if max_size >= body_size * 1.45:
        return ("h2", text, f"<h2>{html.escape(text)}</h2>")
    if max_size >= body_size * 1.18 or (bold and len(text) < 90):
        return ("h3", text, f"<h3>{html.escape(text)}</h3>")
    return ("p", text, f"<p>{html.escape(text)}</p>")


def extract_pdf_structured(
    pdf_bytes: bytes, out_dir: Path, asset_prefix: str = ""
) -> tuple[str, list[str]]:
    """Return (html_body, plain_paragraphs). plain_paragraphs feed the RSS desc."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    body_size = _body_font_size(doc)
    html_parts: list[str] = []
    plain: list[str] = []
    seen_text: set[str] = set()
    try:
        total = min(len(doc), MAX_PAGES)
        for pi in range(total):
            page = doc.load_page(pi)
            blocks: list[tuple[float, str]] = []  # (y, html)
            table_rects: list[tuple] = []
            try:
                finder = page.find_tables()
                for tab in getattr(finder, "tables", []) or []:
                    thtml = _table_to_html(tab)
                    if thtml:
                        table_rects.append(tuple(tab.bbox))
                        blocks.append((tab.bbox[1], thtml))
            except Exception:
                pass

            fig_rects = _figure_rects(page)
            fig_rects = [fr for fr in fig_rects
                         if not any(_overlap(fr, tr) for tr in table_rects)]
            for fi, fr in enumerate(fig_rects):
                try:
                    clip = fitz.Rect(fr)
                    pix = page.get_pixmap(matrix=fitz.Matrix(FIG_SCALE, FIG_SCALE),
                                          clip=clip, alpha=False)
                    name = f"p{pi + 1:03d}-fig{fi + 1:02d}.png"
                    (out_dir / name).write_bytes(pix.tobytes("png"))
                    blocks.append((fr[1],
                        f'<figure class="fig">'
                        f'<img src="{asset_prefix}{name}" alt="Figure {pi+1}.{fi+1}" loading="lazy">'
                        f'<figcaption>Figure {pi + 1}.{fi + 1}</figcaption></figure>'))
                except Exception as exc:
                    print(f"WARN: figure crop failed p{pi+1} f{fi+1}: {exc}")

            page_text_len = 0
            try:
                data = page.get_text("dict")
            except Exception:
                data = {"blocks": []}
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                bbox = block.get("bbox")
                if not bbox:
                    continue
                if any(_overlap(bbox, tr) for tr in table_rects):
                    continue
                if any(_overlap(bbox, fr) for fr in fig_rects):
                    continue
                result = _text_block_html(block, body_size)
                if not result:
                    continue
                kind, text, bhtml = result
                page_text_len += len(text)
                key = text.lower()
                if kind == "p":
                    if key in seen_text:
                        continue
                    seen_text.add(key)
                    plain.append(text)
                blocks.append((bbox[1], bhtml))

            # image-only / scanned page fallback: render whole page once
            if page_text_len < MIN_PAGE_TEXT and not fig_rects and RASTER_FALLBACK:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                    name = f"p{pi + 1:03d}-full.png"
                    (out_dir / name).write_bytes(pix.tobytes("png"))
                    blocks = [(0.0,
                        f'<figure class="page">'
                        f'<img src="{asset_prefix}{name}" alt="Page {pi + 1}" loading="lazy">'
                        f'<figcaption>Page {pi + 1} (image)</figcaption></figure>')]
                except Exception as exc:
                    print(f"WARN: page raster fallback failed p{pi+1}: {exc}")

            blocks.sort(key=lambda x: x[0])
            if blocks:
                html_parts.append(f'<section class="pg" data-page="{pi + 1}">')
                html_parts.extend("  " + b[1] for b in blocks)
                html_parts.append("</section>")
    finally:
        doc.close()
    return "\n".join(html_parts), plain


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #
PAGE_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #111; background: #fff; }
  .wrap { max-width: 880px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { line-height: 1.25; margin: 0 0 12px; }
  h2 { margin: 32px 0 10px; font-size: 1.4em; }
  h3 { margin: 22px 0 8px; font-size: 1.15em; color: #222; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 24px; }
  .btn { display: inline-block; padding: 9px 14px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; }
  .btn.primary { background: #111; color: #fff; border-color: #111; }
  .content { line-height: 1.7; font-size: 16px; }
  .content p { margin: 0 0 1em; }
  .pg { margin: 0 0 8px; }
  figure.fig, figure.page { margin: 18px 0; border: 1px solid #e3e3e3; border-radius: 10px; overflow: hidden; background: #fafafa; }
  figure.fig img, figure.page img { display: block; width: 100%; height: auto; background: #fff; }
  figure figcaption { padding: 7px 12px; font-size: 13px; color: #666; border-top: 1px solid #eee; }
  table.tbl { border-collapse: collapse; width: 100%; margin: 18px 0; font-size: 14px; }
  table.tbl th, table.tbl td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
  table.tbl th { background: #f4f4f4; }
"""


def build_local_page(title: str, source_link: str, description: str,
                     html_body: str, pdf_href: str | None) -> str:
    open_href = html.escape(pdf_href or source_link)
    body = html_body or '<p>Full-text extraction is currently unavailable for this item.</p>'
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        f"  <title>{html.escape(title)}</title>",
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        f"  <style>{PAGE_CSS}</style>",
        "</head>",
        "<body>",
        '  <div class="wrap">',
        f"    <h1>{html.escape(title)}</h1>",
        '    <div class="actions">',
        f'      <a class="btn primary" href="{open_href}" target="_blank" rel="noopener">Open PDF</a>',
        f'      <a class="btn" href="{open_href}" download>Download PDF</a>',
        f'      <a class="btn" href="{html.escape(source_link)}" target="_blank" rel="noopener">Original source</a>',
        '    </div>',
        '    <div class="content">',
        body,
        '    </div>',
        '  </div>',
        "</body>",
        "</html>",
    ])


# --------------------------------------------------------------------------- #
# Feed assembly
# --------------------------------------------------------------------------- #
def entry_slug(title: str, link: str, guid: str) -> str:
    path = (urlparse(link).path or "").strip("/")
    leaf = unquote(path.split("/")[-1]) if path else ""
    base = slugify(leaf) if leaf else slugify(title)
    return f"{base}-{short_hash(guid, link, title)}"


def build_feed(site_dir: Path, public_base: str):
    print(f"Fetching source feed: {SOURCE_FEED_URL}")
    parsed = feedparser.parse(SOURCE_FEED_URL)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"failed to parse source feed: {getattr(parsed, 'bozo_exception', 'unknown')}")

    public_base = public_base.rstrip("/")
    item_root = site_dir / "item" / FEED_NAME
    item_root.mkdir(parents=True, exist_ok=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = (parsed.feed.get("title") or "DB Research") + " (lab)"
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = "Experimental DB Research feed: structured, figure-preserving full-text pages"
    ET.SubElement(channel, "language").text = parsed.feed.get("language") or "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "DBResearch lab structured localizer"

    pdf_count = 0
    for entry in parsed.entries[:MAX_ITEMS]:
        title = normalize_space(entry.get("title", "Untitled")) or "Untitled"
        link = entry.get("link", "").strip()
        description = normalize_space(entry.get("summary", "") or entry.get("description", ""))
        guid = (entry.get("id") or entry.get("guid") or link or title).strip()
        pub_date = parse_pub_date(entry.get("published", "") or entry.get("updated", ""))

        final_link, final_guid, is_permalink = link, guid, bool(guid == link and link.startswith("http"))

        if is_pdf_url(link):
            slug = entry_slug(title, link, guid)
            out_dir = item_root / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            local_url = f"{public_base}/item/{FEED_NAME}/{slug}/" if public_base else link

            pdf_bytes = None
            existing_pdf = out_dir / "original.pdf"
            try:
                pdf_bytes = fetch_pdf_bytes(link)
                existing_pdf.write_bytes(pdf_bytes)
            except Exception as exc:
                print(f"WARN: fetch PDF failed for {link}: {exc}")
                if existing_pdf.exists() and existing_pdf.stat().st_size > 1000:
                    pdf_bytes = existing_pdf.read_bytes()

            html_body, plain = "", []
            if pdf_bytes:
                try:
                    html_body, plain = extract_pdf_structured(pdf_bytes, out_dir, asset_prefix="")
                except Exception as exc:
                    print(f"WARN: structured extract failed for {link}: {exc}")

            if not description:
                description = shorten(plain[0] if plain else title)

            (out_dir / "index.html").write_text(
                build_local_page(title, link, description, html_body,
                                  "original.pdf" if pdf_bytes else None),
                encoding="utf-8",
            )
            final_link = final_guid = local_url
            is_permalink = True
            pdf_count += 1

        rss_item = ET.SubElement(channel, "item")
        ET.SubElement(rss_item, "title").text = title
        ET.SubElement(rss_item, "link").text = final_link
        guid_el = ET.SubElement(rss_item, "guid")
        guid_el.set("isPermaLink", "true" if is_permalink else "false")
        guid_el.text = final_guid
        ET.SubElement(rss_item, "pubDate").text = pub_date
        ET.SubElement(rss_item, "description").text = description

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    (site_dir / OUTPUT_FILE).write_bytes(xml_bytes)
    print(f"Saved {site_dir / OUTPUT_FILE} (pdf_localized={pdf_count})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dbresearch_lab_feed.py <site_dir> <public_base>")
        sys.exit(1)
    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    build_feed(site_dir, sys.argv[2])
