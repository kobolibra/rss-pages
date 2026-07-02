# GitHub Pages RSS Bundle

This repo builds a set of static RSS feeds and their local reader pages, then
publishes them to GitHub Pages via GitHub Actions.

## Included feeds
- pantheonmacro
- Trivium Finance Regs
- Barclays Weekly Insights
- blackrock_weekly_commentary
- carlyle_insights
- pitchbook_reports
- yardeni_morning_briefing
- natixis_insights
- gsam
- dws_cio
- dbresearch (paused — kept published, no longer updated)
- dbresearch_pro (high-fidelity PDF rendering)
- citadel_market_insights
- blackstone_insights
- kkr_insights

## How to use
1. Create a new GitHub repo.
2. Upload everything in this bundle to the repo root.
3. Go to Settings -> Pages -> Source -> GitHub Actions.
4. Run the workflow `.github/workflows/update-rss.yml` once manually.
5. After deploy, your RSS files will be available under:
   `https://<user>.github.io/<repo>/`

The workflow also runs on a schedule (Mon-Sat, 08:00 and 15:00 Beijing time).

## Notes
- Most feeds are rewritten to static local item pages under
  `/item/<feed_name>/<slug>/` so readers like Readwise do not need to follow
  upstream article links.
- `pantheonmacro`, `trivium_finance_regs`, and `barclays_weekly_insights` are
  mirrored from upstream feeds by `scripts/mirror_feeds.py` (Barclays via
  FetchRSS); the remaining feeds are built by dedicated
  `scripts/build_*_feed.py` builders.
- `gsam` is built from Goldman Sachs Asset Management's official search JSON and
  rewritten to local item pages.
- `dws_cio` is built directly from the DWS CIO archive page and rewritten to
  local full-text item pages.
- `citadel_market_insights` is built from Citadel Securities sitemap discovery
  plus article extraction, then rewritten to local full-text item pages.
- `blackstone_insights` and `kkr_insights` are rendered to local reader pages
  (with charts). To keep these two feeds lightweight, their RSS items carry only
  a short summary plus the link to the local page — the full article body is
  **not** embedded in the feed XML.
- `dbresearch` is paused: the published feed and pages are preserved, but no new
  items are pulled. `dbresearch_pro` is the actively built high-fidelity variant.
- Each builder is incremental and falls back to the last-published live copy on
  failure, so a single bad run will not wipe a feed.
