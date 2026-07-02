# RSS Pages — Project Playbook

This is the maintenance playbook for this repo: how the pipeline fits together, what each script does, hard-won lessons from past incidents, and the checklist for adding a new feed. Read this before changing any builder.

## 1. Architecture

Every scheduled run (`.github/workflows/update-rss.yml`, Mon–Sat 08:00 & 15:00 Beijing time, or manual `workflow_dispatch`) does, in order:

1. **Checkout + install deps** (`requests pyyaml feedparser beautifulsoup4 playwright pypdf pymupdf`).
2. **Prepare dirs**: clean `site/` and `feeds/output/`.
3. **Mirror simple upstream feeds** — `scripts/mirror_feeds.py` copies feeds that need no transformation (pantheonmacro, trivium_finance_regs, barclays_weekly_insights raw mirror) straight into `site/`.
4. **Generic web-to-RSS feeds** — `web_to_rss.py` (root) is a config-driven scraper. Each config-based feed (blackrock_weekly_commentary, carlyle_insights, pitchbook_reports, yardeni_morning_briefing, natixis_insights) has a YAML file in `feeds/`, and is invoked through `scripts/build_with_live_fallback.py` so a bad run falls back to the last published copy instead of wiping the feed.
5. **Copy generated XML into `site/`.**
6. **Enrich / rewrite to local pages**:
   - `scripts/rewrite_local_item_feeds.py` turns items into local static reader pages (`/item/<feed>/<slug>/`) for the feeds above plus pantheonmacro and trivium_finance_regs.
   - Dedicated builders run for feeds whose source needs custom scraping/rendering: `build_dbresearch_feed.py` (paused), `build_barclays_feed.py`, `build_gsam_insights_feed.py`, `build_dws_cio_feed.py`, `build_citadel_market_insights_feed.py` — each wrapped in `build_with_live_fallback.py`.
7. **Preserve unchanged feeds** — `scripts/preserve_unchanged_live_feeds.py` keeps the live version for feeds unlikely to have real updates (blackrock_weekly_commentary, barclays_weekly_insights) if the new build looks unchanged/suspect.
8. **Validate** — `scripts/validate_feeds.py`; on regression, `scripts/restore_live_pages_feed.py` pulls the last known-good feed + pages straight from the live GitHub Pages site.
9. **Additive, non-fatal builders** (chart-heavy / experimental; wrapped with `|| echo "... failed"` so a failure never breaks the rest of the deploy): `build_blackstone_insights_feed.py`, `build_kkr_insights_feed.py`, `build_dbresearch_pro_feed.py` (high-fidelity PDF rendering, pypdf/pymupdf).
10. **Final touch-ups** — `scripts/add_source_links.py` guarantees every local item page shows a visible link back to the original source, and strips full body text from RSS `<description>` for feeds in `FULLTEXT_STRIP_FEEDS` (currently `kkr_insights`, `blackstone_insights` — keeps those two feeds lightweight: summary + link only, full text lives only on the local item page). `scripts/fix_blackrock_pages.py` is BlackRock-only: caps oversized chart images via injected CSS and rehosts remote images onto the Pages domain so Readwise Reader can load them.
11. **Add `site/index.html`**, then Configure/Upload/Deploy to GitHub Pages.

## 2. Script reference

| File | Role |
| --- | --- |
| `web_to_rss.py` (root) | Generic YAML-config-driven web scraper → RSS. Drives blackrock/carlyle/pitchbook/yardeni/natixis. |
| `scripts/mirror_feeds.py` | Straight mirror of upstream feeds needing no transform (pantheonmacro, trivium_finance_regs, barclays raw). |
| `scripts/build_with_live_fallback.py` | Wraps any builder command; on failure/bad output, restores the last published copy instead of wiping the feed. Use this wrapper for any new "must not break" builder. |
| `scripts/rewrite_local_item_feeds.py` | Rewrites RSS items to local full-text (or summary-only) reader pages under `/item/<feed>/<slug>/`. `SOURCE_LINK_FEEDS = {carlyle_insights, pitchbook_reports, natixis_insights}`; `SUMMARY_LOCAL_FEEDS = {pantheonmacro, trivium_finance_regs, yardeni_morning_briefing}`. |
| `scripts/build_dbresearch_feed.py` | dbresearch builder — currently **paused** via `DBRESEARCH_MAX_ITEMS=0` in the workflow; just restores the live feed/pages/images so the feed stays published without pulling new items. |
| `scripts/build_barclays_feed.py` | Fetches Barclays Weekly Insights via a Jina reader proxy, parses title/intro paragraph/bulleted takeaways into real `<h*>/<p>/<ul><li>` HTML, and renders a styled local item page (`ITEM_STYLE` constant). |
| `scripts/build_gsam_insights_feed.py` | Builds from Goldman Sachs Asset Management's official search JSON; local item pages with `ITEM_STYLE` constant. |
| `scripts/build_dws_cio_feed.py` | Builds directly from the DWS CIO archive page; full-text local item pages. |
| `scripts/build_citadel_market_insights_feed.py` | Sitemap discovery + article extraction for Citadel Securities; full-text local item pages. |
| `scripts/build_blackstone_insights_feed.py` | Pulls Blackstone's WP REST API (`wp-json/wp/v2/insight`), renders charts; additive/non-fatal; RSS body is summary-only (see `FULLTEXT_STRIP_FEEDS`). |
| `scripts/build_kkr_insights_feed.py` | KKR insights via a politepol.com mirror source, renders charts; additive/non-fatal; RSS body is summary-only. |
| `scripts/build_dbresearch_pro_feed.py` | Experimental high-fidelity PDF-rendering variant of dbresearch (pypdf/pymupdf); additive/non-fatal. |
| `scripts/build_yardeni_static_pages.py` | Small standalone helper (not currently called from `update-rss.yml`) — verify whether it's still needed before relying on it; the live yardeni feed is actually produced via `web_to_rss.py` + `feeds/yardeni_morning_briefing.yaml`. |
| `scripts/preserve_unchanged_live_feeds.py` | Keeps the live version of a feed when a fresh build looks unchanged or suspect. |
| `scripts/validate_feeds.py` | Sanity-checks generated feed XML; triggers restore-from-live on regression. |
| `scripts/restore_live_pages_feed.py` | Pulls last known-good feed + pages straight from the live GitHub Pages site. |
| `scripts/add_source_links.py` | Ensures every local item page has a visible original-source link; strips full body from RSS for `FULLTEXT_STRIP_FEEDS`. |
| `scripts/fix_blackrock_pages.py` | BlackRock-only: caps oversized chart images, rehosts remote images onto the Pages domain. |
| `scripts/README_blackstone_insights.md` | Notes specific to the Blackstone builder. |

## 3. Hard-won lessons (read before editing a builder)

1. **CSS inside an f-string is a trap.** If a builder returns page HTML via an f-string and embeds a `<style>` block with literal curly braces, don't write the CSS directly inside the f-string (you'd need to double every `{`/`}`, which is easy to get wrong and easy to silently corrupt on a big file rewrite). Instead, define the CSS as a **plain (non f-string) triple-quoted string constant** with single braces, e.g. `ITEM_STYLE = """body { ... }"""`, and interpolate the whole constant into the page f-string as `{ITEM_STYLE}`. This is the pattern used in `build_gsam_insights_feed.py` and `build_barclays_feed.py` — reuse it for any new builder with inline CSS.
2. **Always read the file back after writing it.** After any commit to a builder script, immediately re-fetch it and confirm the CSS braces / regex / structure actually landed as intended. Do not assume a write succeeded correctly just because the API call returned success.
3. **Never fabricate file contents or commit SHAs from memory.** Always fetch the current file content and its current `sha` immediately before editing, and pass that exact `sha` when committing — otherwise the write fails or silently clobbers unrelated changes.
4. **There is no git reset/revert tool here** — only create/update, delete, and push. "Undoing" a bad commit means manually re-pushing the correct content as a new commit, not reverting a ref.
5. **Workflow file edits need `workflow` scope.** Some connected GitHub tokens can edit normal `.py`/`.md` files but get a 403 on `.github/workflows/*`. Use the connection that has the `workflow` scope for any workflow YAML change.
6. **Wrap new/fragile builders defensively.** Production feeds are wrapped in `build_with_live_fallback.py` (auto-restore last good copy on failure). Experimental or chart/PDF-heavy builders (Blackstone, KKR, dbresearch_pro) are instead run bare with `|| echo "... failed"` so a crash never blocks the rest of the deploy. Pick whichever pattern matches how critical/stable the new feed is.
7. **Keep heavy feeds lightweight in the RSS body.** If a feed renders long-form content with charts (like Blackstone/KKR), don't embed the full HTML in the RSS `<description>` — add the feed name to `FULLTEXT_STRIP_FEEDS` in `add_source_links.py` so the RSS item stays a short summary + link, and the full content only lives on the local item page.

## 4. Checklist: adding a new feed source

1. Decide the integration path:
   - **Generic path** — source is simple static/paginated article listing pages: add `feeds/<name>.yaml` (use `feeds/_defaults.yaml` and an existing yaml as a template), then add a `build_with_live_fallback.py ... web_to_rss.py -c feeds/<name>.yaml -f -o <name>.xml` line to the "Build WebToRSS feeds" workflow step.
   - **Dedicated path** — source needs a JSON API, JS-rendered pages, PDFs, or custom chart rendering: create `scripts/build_<name>_feed.py` with a `main(site_dir, public_base)` CLI entry point, following the pattern of an existing dedicated builder (e.g. `build_barclays_feed.py`).
2. Have the builder write `<name>.xml` into `site/` and, if it should have local reader pages, write them under `site/item/<name>/<slug>/` (any inline CSS as a plain string constant — see lesson #1 above).
3. Wire it into `.github/workflows/update-rss.yml`: call it via `build_with_live_fallback.py` (stable/production feed) or `|| echo ...` (experimental/additive feed), matching the pattern for similar existing feeds.
4. If you want local full-text pages for a generic-path feed, add its name to the feed-name list passed to `scripts/rewrite_local_item_feeds.py` (and to `SOURCE_LINK_FEEDS` or `SUMMARY_LOCAL_FEEDS` depending on the desired link/summary behavior).
5. If the feed should ship summary-only RSS items (no full body), add its name to `FULLTEXT_STRIP_FEEDS` in `scripts/add_source_links.py`.
6. Add `<name>.xml` to the feed list in the "Add index page" step of the workflow.
7. Add the new feed to the "Included feeds" list in `README.md`.
8. Trigger the workflow manually (`workflow_dispatch`) once and confirm the new feed builds, validates, and renders correctly before relying on the schedule.
