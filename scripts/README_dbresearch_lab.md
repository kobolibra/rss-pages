# DB Research lab feed (experimental)

`build_dbresearch_lab_feed.py` is a **separate, experimental** copy of the
production DB Research builder, used to research a better PDF-on-the-web
experience **without touching** the production `dbresearch` feed or its pages.

- Source feed: same DB Research source as production.
- Output feed: `site/dbresearch_lab.xml`
- Item pages: `site/item/dbresearch_lab/<slug>/index.html` (+ `original.pdf`, `fig-NNN.png`)
- Subscribe URL: `https://kobolibra.github.io/rss-pages/dbresearch_lab.xml`

The production feed (`dbresearch.xml`, `item/dbresearch/...`) is never modified.

## How content is produced (and why)

RSS / read-later apps such as **Readwise Reader** do not display a page as-is.
They run a *readability extractor* that discards layout and styling and keeps
only what it recognizes as article text and images. So:

- Embedding the PDF as a page image or as an `<object>`/SVG reproduction is
  invisible to the reader — it will only show whatever small bit of real text
  exists on the page.
- A faithful "same layout as the PDF" view is therefore **not achievable inside
  a reader** — the reader always reflows into its own clean view. Exact layout
  is only preserved when you open the page or the PDF in a browser.

The realistic best result in a reader is **clean real text + charts/tables as
inline images**. This builder produces exactly that:

- text blocks become real `<h2>/<h3>/<p>/<ul>` (extractable, selectable)
- tables become real HTML `<table>`
- genuine charts/figures are detected (raster images + vector drawing clusters,
  excluding whole-page and table regions) and cropped to inline `<img>`

That semantic HTML is written as the page body **and** mirrored into the feed's
`<content:encoded>` element with **absolute** image URLs, which is the most
reliable way to deliver the full article (with charts) into a reader regardless
of its own page parser.

For pixel-perfect fidelity, each item page also links to the original PDF.

Scanned/image-only PDFs would still need OCR to yield real text; the DB Research
reports are born-digital, so the text comes out real today. Adding an OCR pass is
the natural next research step for true image PDFs.

## Incremental (only process new items)

Mirrors the production model:

1. Restore the published `dbresearch_lab.xml` plus each item page and its assets
   (`original.pdf`, `fig-NNN.png`) from live GitHub Pages.
2. Match incoming entries by GUID and slug.
3. Skip items already localized at the current render version.
4. Only fetch + render entries that are new (or need a render upgrade).
5. If nothing new was processed, the feed and pages are left untouched.

## Render version / upgrades

`RENDER_VERSION` marks the rendering scheme. When it is bumped, already-published
pages are regenerated from their **cached** `original.pdf` (no re-download) the
next time they appear in the source feed, up to `DBRESEARCH_LAB_MAX_ITEMS` per
run. Set `DBRESEARCH_LAB_FORCE_REBUILD=1` to rebuild everything in one run.

## Run locally

```bash
python scripts/build_dbresearch_lab_feed.py site "https://kobolibra.github.io/rss-pages"
```

## Key environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DBRESEARCH_LAB_MAX_ITEMS` | `40` | Max source entries considered per run |
| `DBRESEARCH_LAB_MAX_PAGES` | `80` | Max PDF pages parsed per item |
| `DBRESEARCH_LAB_FIG_SCALE` | `2.0` | Resolution multiplier for cropped figures |
| `DBRESEARCH_LAB_FORCE_REBUILD` | `0` | `1` rebuilds all items (ignores cache/skip) |
| `DBRESEARCH_LAB_BROWSER_FALLBACK` | `1` | `0` disables the Playwright PDF capture fallback |

## CI

The `update-rss.yml` workflow runs this builder as an **additive, non-fatal**
step after the production feeds are built and validated (`... || echo`), so a lab
failure can never break production. No extra dependencies are required — it uses
the already-installed PyMuPDF.
