import html
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

OUT_DIR = Path(os.environ.get("OUT_DIR", "site"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

FEEDS = {
    "pantheonmacro.xml": "https://politepaul.com/fd/ziIC6ajh5OfD.xml",
    "trivium_finance_regs.xml": "https://finance.triviumchina.com/feed",
    "barclays_weekly_insights.xml": "https://fetchrss.com/feed/1sjHuC3vADGU1vGC1u3opETq.rss",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GitHubActions-RSS-Mirror/1.0)"
}


def parse_xml(value: bytes | str):
    return ET.fromstring(value)


def item_title(item) -> str:
    return (item.findtext("title") or "").strip()


def item_link(item) -> str:
    return (item.findtext("link") or "").strip()


def is_legacy_trivium_date(value: str) -> bool:
    """The old mirror stored per-item processing timestamps, not date-only values."""
    if not value:
        return True
    try:
        dt = datetime.strptime(value.strip(), "%a, %d %b %Y %H:%M:%S %z")
        return not (dt.hour == 0 and dt.minute == 0 and dt.second == 0)
    except ValueError:
        return True


def format_trivium_date(value: str) -> str:
    dt = datetime.strptime(value.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def normalize_rss_date(value: str) -> str:
    """Keep only the source publication day and remove fetch-time seconds."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            dt = datetime.strptime(value.strip(), fmt).replace(hour=0, minute=0, second=0, microsecond=0)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            continue
    return value.strip()


def launch_browser(playwright):
    executable = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    options = {
        "headless": not bool(os.getenv("DISPLAY")),
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    if executable:
        options["executable_path"] = executable
    return playwright.chromium.launch(**options)


def new_browser_context(browser):
    context = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1365, "height": 900},
        locale="en-US",
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return context


def browser_fetch_text(url: str, page=None) -> str:
    """Fetch a page through Chromium and return the visible document text."""
    if page is not None:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            pre = page.locator("pre")
            is_xml = pre.count() > 0
            text = pre.first.inner_text(timeout=5000) if is_xml else page.locator("body").inner_text(timeout=5000)
            # Do not HTML-unescape RSS XML: entities such as &#38; must remain escaped.
            raw_text = text.strip()
            normalized = raw_text if "<rss" in raw_text else html.unescape(raw_text)
            if "Please wait while your request is being verified" not in normalized and (("<rss" in normalized and "</rss>" in normalized) or "Posted on " in normalized):
                return normalized
            page.wait_for_timeout(1000)
        raise RuntimeError(f"browser returned a verification page for {url}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = new_browser_context(browser)
        page = context.new_page()
        try:
            return browser_fetch_text(url, page)
        finally:
            browser.close()


def fetch_trivium_source(url: str) -> bytes:
    """Fetch the official RSS through a browser because requests gets Cloudflare HTML."""
    raw = browser_fetch_text(url)
    root = parse_xml(raw)
    if root.find("channel") is None:
        raise RuntimeError("Trivium response has no RSS channel")
    return raw.encode("utf-8")


def detail_dates_for_items(items, page) -> dict[str, str]:
    date_re = re.compile(
        r"Posted\s+on\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4})",
        re.IGNORECASE,
    )
    result = {}
    for item in items:
        link = item_link(item)
        if not link:
            continue
        try:
            text = browser_fetch_text(link, page)
            match = date_re.search(text)
            if not match:
                print(f"[trivium] no Posted on date found: {link}")
                continue
            result[link] = format_trivium_date(match.group(1))
            print(f"[trivium] {match.group(1)} <- {link}")
        except Exception as exc:
            print(f"[trivium] detail date lookup failed for {link}: {exc}")
    return result


def fetch_trivium(url: str, output_path: Path) -> None:
    previous_bytes = output_path.read_bytes() if output_path.exists() else None
    source_bytes = fetch_trivium_source(url)
    source_root = parse_xml(source_bytes)
    source_items = source_root.findall("./channel/item")
    if not source_items:
        raise RuntimeError("Trivium source RSS contains no items")

    previous_titles = set()
    legacy_dates = False
    if previous_bytes:
        try:
            previous_root = parse_xml(previous_bytes)
            previous_items = previous_root.findall("./channel/item")
            previous_titles = {item_title(item) for item in previous_items if item_title(item)}
            legacy_dates = any(is_legacy_trivium_date(item.findtext("pubDate") or "") for item in previous_items)
        except Exception as exc:
            print(f"[trivium] previous feed invalid; rebuilding: {exc}")
            previous_bytes = None

    new_items = [item for item in source_items if item_title(item) not in previous_titles]
    if previous_bytes is not None and not new_items and not legacy_dates:
        output_path.write_bytes(previous_bytes)
        print(f"[trivium] no new items; kept existing feed unchanged (items={len(source_items)})")
        return

    from playwright.sync_api import sync_playwright

    urls_to_date = source_items if legacy_dates else new_items
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = new_browser_context(browser)
        page = context.new_page()
        try:
            dates = detail_dates_for_items(urls_to_date, page)
        finally:
            browser.close()

    for item in source_items:
        link = item_link(item)
        pub_date = item.find("pubDate")
        if link in dates:
            if pub_date is None:
                pub_date = ET.SubElement(item, "pubDate")
            pub_date.text = dates[link]
        elif legacy_dates or item_title(item) not in previous_titles:
            if pub_date is None:
                pub_date = ET.SubElement(item, "pubDate")
            pub_date.text = normalize_rss_date(pub_date.text or "")
        elif previous_bytes is not None and item_title(item) in previous_titles:
            # If a detail page temporarily fails, retain the last known date.
            try:
                old_item = next(x for x in previous_root.findall("./channel/item") if item_title(x) == item_title(item))
                old_date = old_item.findtext("pubDate")
                if old_date:
                    if pub_date is None:
                        pub_date = ET.SubElement(item, "pubDate")
                    pub_date.text = old_date
            except StopIteration:
                pass

    output_path.write_bytes(ET.tostring(source_root, encoding="utf-8", xml_declaration=True))
    print(f"[trivium] saved {output_path}; source_items={len(source_items)} new_items={len(new_items)} repaired_legacy_dates={legacy_dates}")


def fetch_plain(filename: str, url: str) -> None:
    output_path = OUT_DIR / filename
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        output_path.write_bytes(response.content)
        print(f"Saved {filename}")
    except Exception as exc:
        print(f"WARN: failed to fetch {url}: {exc}")


def main():
    for filename, url in FEEDS.items():
        print(f"Fetching {url}")
        try:
            if filename == "trivium_finance_regs.xml":
                fetch_trivium(url, OUT_DIR / filename)
            else:
                fetch_plain(filename, url)
        except Exception as exc:
            print(f"WARN: failed to fetch {url}: {exc}")


if __name__ == "__main__":
    main()
