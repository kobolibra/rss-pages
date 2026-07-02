# Blackstone Insights feed

`build_blackstone_insights_feed.py` turns Blackstone Insights articles into a
local, reader-friendly RSS feed.

## Why

The official feed `https://www.blackstone.com/insights/feed/` can't be ingested
by read-later apps such as Readwise Reader: the `/feed/` endpoint is
bot-protected (returns an unexpected content type / 500), so subscribers get
items with **no article body**.

This builder sidesteps that by fetching each *article page* directly (those are
server-rendered and reachable), extracting the body, and republishing it as
local pages + a local feed.

## What it produces

- `site/blackstone_insights.xml` — RSS feed whose items link to the local reader
  page and carry a short summary in `<description>`. To keep the feed
  lightweight, the full article body is **not** embedded in `<content:encoded>`;
  the complete article (with charts) lives on the local item page that each item
  links to.
- `site/item/blackstone_insights/<slug>/index.html` — one clean semantic page
  per article: headings, paragraphs, lists, tables, and inline `<img>` charts
  (images use absolute Blackstone URLs), plus a “View on Blackstone” button.

## How it works

1. Discover article URLs from the source feed; fall back to scraping
   `https://www.blackstone.com/insights/` when the feed is blocked.
2. Fetch each article page with `requests`.
3. Extract the main content with a readability-style scorer (BeautifulSoup),
   then re-serialize only safe semantic tags with absolute image/link URLs.
4. Write the local page and assemble the feed.

## Incremental model

Same as the other builders: the previously published feed and item pages are
restored from live Pages, already-localized articles are reused, and only newly
added articles are fetched and rendered. Bump `RENDER_VERSION` to force
published pages to regenerate on the next run.

## Config (env)

- `BLACKSTONE_MAX_ITEMS` (default 25)
- `BLACKSTONE_FORCE_REBUILD=1` to re-render every article
- `BLACKSTONE_SOURCE_FEED_URL`, `BLACKSTONE_LIST_URL`, `BLACKSTONE_USER_AGENT`,
  `BLACKSTONE_TIMEOUT`

## CLI

```bash
python scripts/build_blackstone_insights_feed.py site "https://kobolibra.github.io/rss-pages"
```

Subscribe URL: `https://kobolibra.github.io/rss-pages/blackstone_insights.xml`
