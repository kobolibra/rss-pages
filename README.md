# GitHub Pages RSS Bundle

This bundle contains the first-pass GitHub Pages static RSS setup.

## Included feeds
- pantheonmacro
- Trivium Finance Regs
- BlackRock Weekly Commentary Diffbot
- Barclays Weekly Insights
- blackrock_weekly_commentary
- carlyle_insights
- pitchbook_reports
- yardeni_morning_briefing

## How to use
1. Create a new GitHub repo.
2. Upload everything in this bundle to the repo root.
3. Go to Settings -> Pages -> Source -> GitHub Actions.
4. Run the workflow `.github/workflows/update-rss.yml` once manually.
5. After deploy, your RSS files will be available under:
   `https://<user>.github.io/<repo>/`

## Notes
- Yardeni also generates static item pages under `/item/yardeni_morning_briefing/<slug>/`.
- This bundle intentionally excludes FetchRSS BlackRock and MCP.
