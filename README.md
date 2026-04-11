# GitHub Pages RSS Bundle

This bundle contains the first-pass GitHub Pages static RSS setup.

## Included feeds
- pantheonmacro
- Trivium Finance Regs
- Barclays Weekly Insights
- blackrock_weekly_commentary
- carlyle_insights
- pitchbook_reports
- yardeni_morning_briefing
- gsam

## How to use
1. Create a new GitHub repo.
2. Upload everything in this bundle to the repo root.
3. Go to Settings -> Pages -> Source -> GitHub Actions.
4. Run the workflow `.github/workflows/update-rss.yml` once manually.
5. After deploy, your RSS files will be available under:
   `https://<user>.github.io/<repo>/`

## Notes
- All live feeds are intended to be rewritten to static local item pages under `/item/<feed_name>/<slug>/` so readers like Readwise do not need to follow upstream article links.
- `gsam` is built from Goldman Sachs Asset Management's official search JSON and rewritten to local item pages.
- This bundle intentionally excludes FetchRSS BlackRock and MCP.
