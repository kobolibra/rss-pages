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

    failures = 0
    for feed_name in feed_names:
        feed_url = f"{base_url}/{feed_name}.xml"
        print(f"restoring {feed_url}")
        try:
            r = requests.get(feed_url, timeout=30)
            r.raise_for_status()
        except Exception as exc:
            # Isolate per-feed failures so one transient error does not block
            # restoring the remaining feeds. Still surfaced via non-zero exit below
            # so build_with_live_fallback keeps treating an unrecoverable feed as a
            # hard failure.
            print(f"ERROR: failed to restore feed {feed_name} from {feed_url}: {exc}")
            failures += 1
            continue
        xml_path = site_dir / f"{feed_name}.xml"
        xml_path.write_bytes(r.content)

        if xml_only:
            continue

        parsed = feedparser.parse(r.content)
        for entry in parsed.entries:
            item_link = entry.get("link", "")
            if not item_link.startswith(base_url + "/item/"):
                continue
            try:
                item_resp = requests.get(item_link, timeout=30)
                item_resp.raise_for_status()
            except Exception as exc:
                # The feed XML is already restored above; a single missing item page
                # should not abort the whole self-heal.
                print(f"warn: could not restore item {item_link}: {exc}")
                continue
            # Map the public item URL back to its on-disk location under site/.
            # The Pages base URL may include a repo subpath (e.g. /rss-pages);
            # validate_feeds.py looks for the page at site/item/<feed>/<slug>/index.html,
            # so strip the base-url prefix here instead of using the full URL path
            # (which would add an extra rss-pages/ segment and break the self-heal).
            rel = item_link[len(base_url):].lstrip("/")
            out_dir = site_dir / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_bytes(item_resp.content)
            print(f"restored item {item_link}")

    if failures:
        print(f"restore finished with {failures} feed failure(s)")
        sys.exit(1)
