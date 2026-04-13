import sys
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python restore_live_pages_feed.py <base_url> <site_dir> <feed1,feed2,...> [--xml-only]")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    site_dir = Path(sys.argv[2])
    feed_names = [x.strip() for x in sys.argv[3].split(",") if x.strip()]
    xml_only = "--xml-only" in sys.argv[4:]

    for feed_name in feed_names:
        feed_url = f"{base_url}/{feed_name}.xml"
        print(f"restoring {feed_url}")
        r = requests.get(feed_url, timeout=30)
        r.raise_for_status()
        xml_path = site_dir / f"{feed_name}.xml"
        xml_path.write_bytes(r.content)

        if xml_only:
            continue

        parsed = feedparser.parse(r.content)
        for entry in parsed.entries:
            item_link = entry.get("link", "")
            if not item_link.startswith(base_url + "/item/"):
                continue
            item_resp = requests.get(item_link, timeout=30)
            item_resp.raise_for_status()
            rel = urlparse(item_link).path.lstrip("/")
            out_dir = site_dir / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_bytes(item_resp.content)
            print(f"restored item {item_link}")
