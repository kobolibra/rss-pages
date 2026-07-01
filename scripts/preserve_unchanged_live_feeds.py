import html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import feedparser
import requests

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
CONTENT = "{" + CONTENT_NS + "}encoded"


def normalize(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def item_signature(item) -> tuple:
    return (
        normalize(item.findtext("title") or ""),
        normalize(item.findtext("link") or ""),
        normalize(item.findtext("guid") or ""),
        normalize(item.findtext("description") or ""),
        normalize(item.findtext(CONTENT) or ""),
    )


def feed_signature(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS missing channel")
    return [item_signature(item) for item in channel.findall("item")]


def restore_live_feed(base_url: str, site_dir: Path, feed_name: str, xml_bytes: bytes):
    xml_path = site_dir / f"{feed_name}.xml"
    xml_path.write_bytes(xml_bytes)

    parsed = feedparser.parse(xml_bytes)
    for entry in parsed.entries:
        item_link = entry.get("link", "")
        if not item_link.startswith(base_url + "/item/"):
            continue
        try:
            item_resp = requests.get(item_link, timeout=30)
            item_resp.raise_for_status()
        except Exception as exc:
            # A single item page being temporarily unfetchable must not abort the
            # whole preserve/restore; the feed XML itself is already written above.
            print(f"warn: could not restore item {item_link}: {exc}")
            continue
        # Strip the Pages base-url prefix (which may include /rss-pages) so the page
        # lands at site/item/<feed>/<slug>/index.html to match validate_feeds.py.
        rel = item_link[len(base_url):].lstrip("/")
        out_dir = site_dir / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_bytes(item_resp.content)
        print(f"restored item {item_link}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python preserve_unchanged_live_feeds.py <base_url> <site_dir> <feed1,feed2,...>")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    site_dir = Path(sys.argv[2])
    feed_names = [x.strip() for x in sys.argv[3].split(",") if x.strip()]

    for feed_name in feed_names:
        local_path = site_dir / f"{feed_name}.xml"
        if not local_path.exists():
            print(f"skip {feed_name}: local feed missing")
            continue

        live_url = f"{base_url}/{feed_name}.xml"
        print(f"checking {feed_name} against live {live_url}")
        try:
            resp = requests.get(live_url, timeout=30)
            if resp.status_code == 404:
                print(f"skip {feed_name}: live feed not found yet")
                continue
            resp.raise_for_status()
            live_bytes = resp.content
            local_bytes = local_path.read_bytes()

            live_sig = feed_signature(live_bytes)
            local_sig = feed_signature(local_bytes)
        except Exception as exc:
            # Preserve is only an optimization (restore the live copy when the newly
            # built feed is byte-for-byte equivalent, to avoid churn). A transient
            # network or parse error must never fail the build, so keep the freshly
            # built output and move on.
            print(f"skip {feed_name}: preserve check failed ({exc}); keeping newly built output")
            continue

        if live_sig == local_sig:
            print(f"unchanged {feed_name}: restoring live copy to preserve timestamps/build metadata")
            try:
                restore_live_feed(base_url, site_dir, feed_name, live_bytes)
            except Exception as exc:
                print(f"skip {feed_name}: live restore failed ({exc}); keeping newly built output")
        else:
            print(f"changed {feed_name}: keeping newly built output")
