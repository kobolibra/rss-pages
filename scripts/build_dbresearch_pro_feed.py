#!/usr/bin/env python3
"""DB Research PRO builder: reconstruct PDF content as a native web page.

v4 extraction:
  * Layout-aware reading order. Each page is classified as a true multi-column
    layout (independent side-by-side panels, e.g. a country grid) or a
    single-flow / label+content layout, by testing how many text blocks cross
    the page midline. Multi-column pages are read column-major (whole left
    column, then whole right column) within bands delimited by full-width
    spanning elements; everything else is read row-major (y, then x). This
    stops the left/right panels of dashboard PDFs from interleaving.
  * Box-bullet lists. DB's bullet glyph (❑ and friends) is recognized, and a
    bullet that wraps across several lines is merged into a single list item.
  * Inline legal/cert noise removal vs. end-matter hard stop. Footer
    disclaimers, "Analyst Certification" blocks, emails and bare URLs are
    dropped in place WITHOUT truncating the document; only the dedicated
    "Appendix 1 / Disclosures" end-matter (or its opening DB disclaimer
    sentence) stops extraction.
  * Author/sidebar de-interleaving, source-anchored chart cropping and
    content-driven figure bounding boxes (from v3) are retained.
  * Real-table guard keeps genuine data tables and rejects chart-axis soup.

Fetching is robust: direct binary -> viewer-page scrape (var pdfUrl / canonical
link) -> headless-browser capture, lazily running `playwright install chromium`
if the browser binary is missing.

Limits:
  * First run (no live feed yet): at most FIRST_RUN_MAX_ITEMS (=10) articles,
    of which at most MAX_PDFS_PER_RUN (=5) are localized PDFs.
  * Every run: localize at most MAX_PDFS_PER_RUN (=5) NEW (or version-upgraded)
    PDFs; already-built items at the current render version are reused.
  * PDFs beyond the budget keep their published version (or, if brand new, are
    deferred pointing at the original PDF) and are built on a later run.

Reader-optimized: clean semantic HTML is mirrored into <content:encoded> with
ABSOLUTE image URLs so Readwise Reader renders the full article directly.

Writes dbresearch_pro.xml + item/dbresearch_pro/<slug>/.
Usage: python build_dbresearch_pro_feed.py <site_dir> <public_base>
"""
import asyncio
import hashlib
import html
import os
import re
import statistics
import subprocess
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
CONTENT_ENCODED = "{" + CONTENT_NS + "}encoded"
ET.register_namespace("content", CONTENT_NS)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SOURCE_FEED_URL = os.environ.get(
    "DBRESEARCH_PRO_SOURCE_FEED_URL",
    "https://rssweball.top/feed/b706ef15-fa4f-45b2-a1fd-4cd8d037e91c.xml",
)
FEED_NAME = os.environ.get("DBRESEARCH_PRO_FEED_NAME", "dbresearch_pro")
OUTPUT_FILE = os.environ.get("DBRESEARCH_PRO_OUTPUT_FILE", f"{FEED_NAME}.xml")
MAX_ITEMS = int(os.environ.get("DBRESEARCH_PRO_MAX_ITEMS", "40"))
FIRST_RUN_MAX_ITEMS = int(os.environ.get("DBRESEARCH_PRO_FIRST_RUN_MAX_ITEMS", "10"))
MAX_PDFS_PER_RUN = int(os.environ.get("DBRESEARCH_PRO_MAX_PDFS", "5"))
MAX_PAGES = int(os.environ.get("DBRESEARCH_PRO_MAX_PAGES", "60"))
REQUEST_TIMEOUT = int(os.environ.get("DBRESEARCH_PRO_TIMEOUT", "60"))
FIG_SCALE = float(os.environ.get("DBRESEARCH_PRO_FIG_SCALE", "2.0"))
USER_AGENT = os.environ.get(
    "DBRESEARCH_PRO_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT}
BROWSER_FALLBACK_ENABLED = os.environ.get("DBRESEARCH_PRO_BROWSER_FALLBACK", "1") != "0"
FORCE_REBUILD = os.environ.get("DBRESEARCH_PRO_FORCE_REBUILD", "0") == "1"

# Bump when rendering changes so published pages regenerate (from cached PDF).
RENDER_VERSION = 7

FIG_CAP_RE = re.compile(r"^(figure|chart|exhibit)\s+\d+", re.I)
SOURCE_RE = re.compile(r"^\s*source\b", re.I)
STRONG_BULLET_RE = re.compile(
    r"^\s*[\u2022\u25aa\u25cf\u25e6\u2043\u2023\u2751\u2752\u274f\u25a1\u25a0\u2756\u2727\u2b1b\u2b1c]\s+"
)
WEAK_BULLET_RE = re.compile(r"^\s*(?:[\u2013\u2014\-\*]|\d+[.)])\s+")
AUTHOR_TITLE_HINTS = ("analyst", "strategist", "economist", "research", "specialist", "officer", "head of")

# End-matter: hard-stop extraction (everything after this is legal back matter).
ENDMATTER_HEADINGS = {
    "appendix 1",
    "appendix",
    "appendix 1: important disclosures",
    "disclaimer",
    "disclaimers",
    "disclosures",
    "important disclosures",
}
ENDMATTER_MARKERS = (
    "this material has been prepared by the deutsche bank research institute",
    "this material has been prepared by",
    "neither deutsche bank ag nor any of its affiliates makes any representation",
)
# Inline legal/cert noise: drop the block but keep going (no truncation).
LEGAL_NOISE_HEADINGS = {"analyst certification"}
LEGAL_NOISE_MARKERS = (
    "the views expressed above accurately reflect",
    "the views expressed in this report accurately reflect",
    "for other important disclosures please visit",
    "incomplete disclosure information may have been displayed",
    "prices are current as of the end of the previous trading session",
    "important research disclosures located in appendix",
)

_CHROMIUM_READY = False


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
    if re.fullmatch(r"(?:https?://|www\.)\S+", low):
        return True
    if re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", low) and len(low) < 70:
        return True
    if "@db.com" in low:
        return True
    if "db blue template" in low:
        return True
    if "deutsche bank" in low and len(low) < 90:
        return True
    if re.fullmatch(r"\d+\|\d+\|\d+", low):
        return True
    if re.search(r"\d{1,2}/\d{1,2}/\d{4}", low) and len(low) < 60:
        return True
    if low in {
        "deutsche bank research institute",
        "deutsche bank ag",
        "deutsche bank ag/london",
        "sensitivity: public",
        "capital markets blog",
        "source: deutsche bank research",
    }:
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


# --------------------------------------------------------------------------- #
# PDF fetching (direct -> page scrape -> browser capture)
# --------------------------------------------------------------------------- #
def ensure_chromium():
    global _CHROMIUM_READY
    if _CHROMIUM_READY:
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            timeout=300,
        )
    except Exception as exc:
        print(f"WARN: chromium ensure failed: {exc}")
    _CHROMIUM_READY = True


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
            ensure_chromium()
            return asyncio.run(_fetch_pdf_via_browser(url))
        except Exception as exc:
            print(f"WARN: browser PDF capture failed for {url}: {exc}")
    raise ValueError("could not resolve real PDF binary URL")


# --------------------------------------------------------------------------- #
# Structured extraction (semantic text + real tables + cropped charts)
# --------------------------------------------------------------------------- #
def _table_to_html(table) -> str:
    try:
        data = table.extract()
    except Exception:
        return ""
    rows = [[normalize_space(c or "") for c in row] for row in (data or [])]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
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


def _table_is_real(table) -> bool:
    """Reject chart gridlines / axis-label soup masquerading as a table."""
    try:
        data = table.extract()
    except Exception:
        return False
    rows = [[normalize_space(c or "") for c in row] for row in (data or [])]
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) < 2:
        return False
    ncols = max((len(r) for r in rows), default=0)
    if ncols < 2:
        return False
    for r in rows:
        for c in r:
            if len(c) > 80:
                return False
            numlike = sum(1 for tok in c.split() if re.search(r"\d", tok))
            if numlike >= 8:
                return False
    return True


def _detect_boilerplate(doc, total, sizes_out):
    """Scan margin zones and return a set of repeated header/footer lines."""
    margin_counts = {}
    for pi in range(total):
        page = doc.load_page(pi)
        H = page.rect.height
        top_y = page.rect.y0 + 0.10 * H
        bot_y = page.rect.y1 - 0.10 * H
        page_lines = set()
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                for sp in spans:
                    if sp.get("text", "").strip():
                        sizes_out.append(sp.get("size", 0.0))
                t = normalize_space("".join(sp.get("text", "") for sp in spans))
                if not t:
                    continue
                lb = fitz.Rect(ln.get("bbox"))
                if lb.y1 <= top_y or lb.y0 >= bot_y:
                    page_lines.add(t.lower())
        for t in page_lines:
            margin_counts[t] = margin_counts.get(t, 0) + 1
    thresh = max(3, int(0.4 * total))
    return {t for t, c in margin_counts.items() if c >= thresh and len(t) < 120}


def _pair_authors(lines):
    paired = []
    for ln in lines:
        low = ln.lower()
        if paired and any(k in low for k in AUTHOR_TITLE_HINTS) and len(ln) < 40:
            paired[-1] = f"{paired[-1]} \u2014 {ln}"
        else:
            paired.append(ln)
    return paired


def _bullet_split(lines):
    """Group lines into bullet items, merging wrapped continuation lines."""
    items = []
    cur = None
    n_bullets = 0
    for t in lines:
        m = STRONG_BULLET_RE.match(t) or WEAK_BULLET_RE.match(t)
        if m:
            n_bullets += 1
            if cur is not None:
                items.append(cur)
            cur = t[m.end():].strip()
        elif cur is None:
            cur = t.strip()
        else:
            cur = f"{cur} {t.strip()}".strip()
    if cur:
        items.append(cur)
    return [it for it in items if it], n_bullets


def extract_pdf_content(pdf_bytes: bytes, out_dir: Path, title: str = "", max_pages: int = MAX_PAGES):
    """Return (elements, plain). Layout-aware ordering; charts -> inline images."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    elements = []
    plain = []
    author_lines = []
    seen_block = set()
    fig_n = 0
    disclaimer_hit = False
    title_low = (title or "").strip().lower()
    try:
        total = min(len(doc), max_pages)
        sizes = []
        boilerplate = _detect_boilerplate(doc, total, sizes)
        median = statistics.median(sizes) if sizes else 10.0

        def block_info(b):
            line_texts, line_sizes, bold_flags = [], [], []
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                t = normalize_space("".join(sp.get("text", "") for sp in spans))
                if not t:
                    continue
                line_texts.append(t)
                line_sizes.append(max((sp.get("size", 0.0) for sp in spans), default=median))
                bold_flags.append(any((sp.get("flags", 0) & 16) for sp in spans))
            text = normalize_space(" ".join(line_texts))
            big = max(line_sizes) if line_sizes else median
            bold = bool(bold_flags) and sum(bold_flags) >= max(1, len(bold_flags) // 2)
            return line_texts, text, big, bold, fitz.Rect(b.get("bbox"))

        for pi in range(total):
            if disclaimer_hit:
                break
            page = doc.load_page(pi)
            R = page.rect
            H, Wp = R.height, R.width
            mid_x = (R.x0 + R.x1) / 2
            blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
            infos = [block_info(b) for b in blocks]

            draw_rects = []
            try:
                for d in page.get_drawings():
                    rr = fitz.Rect(d.get("rect"))
                    if not rr.is_empty and rr.width > 8 and rr.height > 8:
                        draw_rects.append(rr)
            except Exception:
                pass
            img_rects = []
            try:
                for img in page.get_images(full=True):
                    try:
                        for rr in page.get_image_rects(img[0]):
                            img_rects.append(fitz.Rect(rr))
                    except Exception:
                        pass
            except Exception:
                pass
            graphics = draw_rects + img_rects

            def column_of(bbox):
                if bbox.width / Wp > 0.6:
                    return "full"
                return "left" if (bbox.x0 + bbox.x1) / 2 < mid_x else "right"

            def in_col(x0, x1, col):
                if col == "full":
                    return True
                cx = (x0 + x1) / 2
                return cx < mid_x if col == "left" else cx >= mid_x

            def is_fw(bbox):
                return bbox.width > 0.62 * Wp and bbox.x0 < R.x0 + 0.15 * Wp

            def crosses_mid(bbox):
                return bbox.x0 < mid_x - 0.03 * Wp and bbox.x1 > mid_x + 0.03 * Wp

            # ---- author / sidebar capture (cover page) ----
            author_idxs = set()
            auth_i = next((i for i, inf in enumerate(infos)
                           if normalize_space(inf[1]).lower() in ("authors", "author")), None)
            if auth_i is not None:
                abbox = infos[auth_i][4]
                acol = column_of(abbox)
                if acol != "full":
                    page_authors = []
                    for i, (lt, text, big, bold, bbox) in enumerate(infos):
                        if bbox.y0 >= abbox.y0 - 2 and in_col(bbox.x0, bbox.x1, acol) and len(text) < 80:
                            author_idxs.add(i)
                            if i != auth_i:
                                for ln in lt:
                                    if not is_junk_line(ln):
                                        page_authors.append((bbox.y0, normalize_space(ln)))
                    page_authors.sort(key=lambda e: e[0])
                    author_lines.extend(t for _, t in page_authors if t)

            # ---- caption detection ----
            cap_idx = []
            head_spans = []
            for i, (lt, text, big, bold, bbox) in enumerate(infos):
                if not text or i in author_idxs:
                    continue
                if FIG_CAP_RE.match(text):
                    cap_idx.append(i)
                elif big >= 1.18 * median and len(text) < 80:
                    head_spans.append((bbox.x0, bbox.x1, bbox.y0))
            cap_spans = [(infos[i][4].x0, infos[i][4].x1, infos[i][4].y0) for i in cap_idx]

            # ---- per-figure cropping (source-anchored, content-driven bbox) ----
            fig_items = []  # (order_bbox, image_name_or_None, caption_text)
            fig_rects = []
            for i in cap_idx:
                lt, text, big, bold, cbbox = infos[i]
                col = column_of(cbbox)
                cands = [by0 for (bx0, bx1, by0) in (cap_spans + head_spans)
                         if by0 > cbbox.y1 + 2 and in_col(bx0, bx1, col)]
                y_limit = min(cands) if cands else (R.y1 - 0.06 * H)
                y_limit = min(y_limit, cbbox.y1 + 0.55 * H)
                gset = [g for g in graphics
                        if g.height > 8 and g.width > 8
                        and g.y1 > cbbox.y0 and g.y0 < y_limit
                        and in_col(g.x0, g.x1, col)]
                if not gset:
                    fig_items.append((cbbox, None, text))
                    continue
                gx0 = min(g.x0 for g in gset)
                gx1 = max(g.x1 for g in gset)
                gy1 = max(g.y1 for g in gset)
                src_y = None
                for j, (lt2, text2, big2, bold2, bbox2) in enumerate(infos):
                    if j in author_idxs or not text2:
                        continue
                    if (bbox2.y0 >= cbbox.y1 and bbox2.y0 < y_limit
                            and in_col(bbox2.x0, bbox2.x1, col) and SOURCE_RE.match(text2)):
                        src_y = bbox2.y1 if src_y is None else max(src_y, bbox2.y1)
                bottom = (src_y if src_y is not None else gy1) + 3
                bottom = min(bottom, y_limit - 1)
                top = cbbox.y1 + 1
                for (lt2, text2, big2, bold2, bbox2) in infos:
                    if (bbox2.y0 >= top - 2 and bbox2.y1 <= bottom + 2
                            and in_col(bbox2.x0, bbox2.x1, col)
                            and not SOURCE_RE.match(text2) and not FIG_CAP_RE.match(text2)
                            and len(text2) < 60):
                        gx0 = min(gx0, bbox2.x0)
                        gx1 = max(gx1, bbox2.x1)
                x0c = min(gx0, cbbox.x0) - 3
                x1c = max(gx1, cbbox.x1) + 5
                crop = fitz.Rect(x0c, top, x1c, bottom) & R
                order_bbox = fitz.Rect(crop.x0, cbbox.y0, crop.x1, crop.y1)
                if crop.is_empty or crop.height < 30 or crop.width < 60:
                    fig_items.append((cbbox, None, text))
                    continue
                try:
                    fig_n += 1
                    pix = page.get_pixmap(matrix=fitz.Matrix(FIG_SCALE, FIG_SCALE), clip=crop, alpha=False)
                    name = f"fig-{fig_n:03d}.png"
                    (out_dir / name).write_bytes(pix.tobytes("png"))
                    fig_items.append((order_bbox, name, text))
                    fig_rects.append(crop)
                except Exception:
                    fig_items.append((cbbox, None, text))

            # ---- real tables (skip chart-axis soup and figure overlaps) ----
            table_items, table_rects = [], []
            try:
                for t in page.find_tables().tables:
                    rr = fitz.Rect(t.bbox)
                    if any(rr.intersects(fr) and _area(rr & fr) > 0.3 * _area(rr) for fr in fig_rects):
                        continue
                    if not _table_is_real(t):
                        continue
                    th = _table_to_html(t)
                    if th:
                        table_items.append((rr, th))
                        table_rects.append(rr)
            except Exception:
                pass

            # ---- body text ----
            positioned = []  # (bbox, element)
            for idx, (lt, text, big, bold, bbox) in enumerate(infos):
                if not text or idx in author_idxs or FIG_CAP_RE.match(text):
                    continue
                if any(_covers(fr, bbox) for fr in fig_rects):
                    continue
                if any(_covers(rr, bbox) for rr in table_rects):
                    continue
                kept = [t for t in lt if not is_junk_line(t) and t.lower() not in boilerplate]
                if not kept:
                    continue
                btext = normalize_space(" ".join(kept))
                low = btext.lower()
                heading_like = (big >= 1.18 * median or bold) and len(btext) < 60
                if (heading_like and low in ENDMATTER_HEADINGS) or (
                    len(btext) > 80 and any(m in low for m in ENDMATTER_MARKERS)
                ):
                    disclaimer_hit = True
                    break
                if heading_like and low in LEGAL_NOISE_HEADINGS:
                    continue
                if any(m in low for m in LEGAL_NOISE_MARKERS):
                    continue
                if title_low and title_low in low and len(btext) < len(title_low) + 40:
                    continue
                if len(btext) < 80 and low in seen_block:
                    continue
                if len(btext) < 80:
                    seen_block.add(low)
                list_items, n_bullets = _bullet_split(kept)
                strong0 = bool(STRONG_BULLET_RE.match(kept[0]))
                if n_bullets >= 1 and (strong0 or n_bullets >= 2):
                    el = ("ul", list_items)
                elif big >= 1.45 * median and len(btext) < 200:
                    el = ("h2", btext)
                elif (big >= 1.18 * median or bold) and len(btext) < 160:
                    el = ("h3", btext)
                else:
                    el = ("p", btext)
                positioned.append((bbox, el))

            for rr, th in table_items:
                positioned.append((rr, ("table", th)))
            for ob, name, cap_text in fig_items:
                positioned.append((ob, ("figure", name, cap_text) if name else ("h3", cap_text)))

            # ---- layout-aware ordering ----
            nonfw = [b for b, _ in positioned if not is_fw(b)]
            n_cross = sum(1 for b in nonfw if crosses_mid(b))
            two_col = len(nonfw) >= 4 and n_cross <= 0.25 * len(nonfw)
            if two_col:
                divs = sorted(b.y0 for b, _ in positioned if is_fw(b))

                def band_of(y):
                    return sum(1 for d in divs if d <= y + 1)

                def order_key(item):
                    b, _el = item
                    if is_fw(b):
                        col = -1
                    else:
                        col = 0 if (b.x0 + b.x1) / 2 < mid_x else 1
                    return (band_of(b.y0), col, round(b.y0, 1), b.x0)
            else:
                def order_key(item):
                    b, _el = item
                    return (round(b.y0, 1), b.x0)
            positioned.sort(key=order_key)

            for _, el in positioned:
                elements.append(el)
                if el[0] in ("h2", "h3", "p") and len(el[1]) >= 24:
                    plain.append(el[1])
                elif el[0] == "ul":
                    plain.extend(x for x in el[1] if len(x) >= 24)
    finally:
        doc.close()

    if author_lines:
        elements = [("h3", "Authors"), ("ul", _pair_authors(author_lines))] + elements
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
            parts.append(f'<figure><img src="{src}" alt="{cap or "chart"}" loading="lazy">{cap_html}</figure>')
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
  figcaption { font-size: 0.9em; color: #444; margin-top: 6px; font-style: italic; }
  .tablewrap { overflow-x: auto; margin: 1.1em 0; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td { border: 1px solid #ddd; padding: 6px 9px; text-align: left; vertical-align: top; }
  thead th { background: #f5f5f5; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 24px; font-family: -apple-system, sans-serif; }
  .btn { display: inline-block; padding: 8px 13px; border-radius: 8px; text-decoration: none; border: 1px solid #ccc; color: #111; background: #fff; font-size: 0.9em; }
  .btn.primary { background: #111; color: #fff; border-color: #111; }
"""


def build_local_page(title: str, source_link: str, pdf_href, reader_html: str) -> str:
    open_href = html.escape(pdf_href or source_link)
    body = reader_html or "<p>Content could not be extracted from this PDF.</p>"
    lines = [
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
        "    </div>",
        '    <section class="reader-content">',
        body,
        "    </section>",
        "  </article>",
        "</body>",
        "</html>",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Incremental state (restore live feed + item pages + assets)
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
            "content_html": (item.findtext(CONTENT_ENCODED) or ""),
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

    is_bootstrap = len(existing_items) == 0
    item_budget = FIRST_RUN_MAX_ITEMS if is_bootstrap else MAX_ITEMS

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = (parsed.feed.get("title") or "DB Research") + " (pro)"
    ET.SubElement(channel, "link").text = f"{public_base}/{OUTPUT_FILE}" if public_base else SOURCE_FEED_URL
    ET.SubElement(channel, "description").text = "High-fidelity DB Research feed: PDF text, charts and tables rebuilt as native reader pages"
    ET.SubElement(channel, "language").text = parsed.feed.get("language") or "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "generator").text = "DBResearch pro high-fidelity builder"

    output_items = []
    processed_pdfs = 0
    pdf_count = 0
    new_or_built = 0

    for entry in parsed.entries:
        if len(output_items) >= item_budget:
            break

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
            continue

        if is_pdf_url(link) and slug and processed_pdfs >= MAX_PDFS_PER_RUN:
            existing_link = (existing_item.get("link") if existing_item else "") or ""
            if (existing_item and existing_link.startswith(f"{public_base}/item/{FEED_NAME}/")
                    and existing_item.get("content_html")):
                print(f"INFO: budget reached; keeping published version of {link}")
                output_items.append({
                    "title": existing_item.get("title") or title,
                    "link": existing_link,
                    "guid": existing_item.get("guid") or guid,
                    "pub_date": existing_item.get("pub_date") or pub_date,
                    "description": existing_item.get("description") or description or shorten(title),
                    "content_html": existing_item.get("content_html") or "",
                    "is_permalink": True,
                })
            else:
                print(f"INFO: deferring (per-run PDF budget reached) {link}")
                output_items.append({
                    "title": title,
                    "link": link,
                    "guid": guid,
                    "pub_date": pub_date,
                    "description": description or shorten(title),
                    "content_html": "",
                    "is_permalink": bool(guid == link and link.startswith("http")),
                })
                new_or_built += 1 if guid not in existing_guid_map else 0
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
                for old in (list(out_dir.glob("fig-*.png")) + list(out_dir.glob("page-*.png"))):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                try:
                    elements, plain = extract_pdf_content(pdf_bytes, out_dir, title=title)
                except Exception as exc:
                    print(f"WARN: extraction failed for {link}: {exc}")

            if not description:
                description = shorten(plain[0] if plain else title)

            reader_body = render_elements(elements, img_prefix="")
            reader_abs = render_elements(elements, img_prefix=local_url)
            content_html = reader_abs
            (out_dir / "index.html").write_text(
                build_local_page(title, link, "original.pdf" if pdf_bytes else None, reader_body),
                encoding="utf-8",
            )
            final_link = final_guid = local_url
            is_permalink = True
            processed_pdfs += 1
            pdf_count += 1
            new_or_built += 1
        elif not description:
            description = shorten(title)

        if guid not in existing_guid_map:
            new_or_built += 1

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
            ET.SubElement(rss_item, CONTENT_ENCODED).text = item["content_html"]

    if new_or_built == 0 and output_path.exists():
        print(f"no new {FEED_NAME} items; kept existing feed and pages")
        return

    xml_bytes = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    output_path.write_bytes(xml_bytes)
    print(f"Saved {output_path} (items={len(output_items)}, pdf_built={pdf_count}, budget={MAX_PDFS_PER_RUN}, bootstrap={is_bootstrap})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python build_dbresearch_pro_feed.py <site_dir> <public_base>")
        sys.exit(1)
    site_dir = Path(sys.argv[1])
    site_dir.mkdir(parents=True, exist_ok=True)
    build_feed(site_dir, sys.argv[2])
