# DB Research lab feed (experimental)

`build_dbresearch_lab_feed.py` is a **separate, experimental** copy of the
production DB Research builder. It is used to research a better
"reproduce the PDF on the web" experience **without touching** the production
`dbresearch` feed or its pages.

- Source feed: same DB Research source as production.
- Output feed: `site/dbresearch_lab.xml`
- Item pages: `site/item/dbresearch_lab/<slug>/index.html` (+ `original.pdf`, `page-NNN.svg`)
- Subscribe URL: `https://kobolibra.github.io/rss-pages/dbresearch_lab.xml`

The production feed (`dbresearch.xml`, `item/dbresearch/...`) is never modified.

## What this experiment changes vs. the first lab version

### 1. Incremental (only process new items)

It mirrors the production incrementality model:

1. Restore the already-published `dbresearch_lab.xml` plus each item page and its
   assets (including `original.pdf` and the page SVGs) from live GitHub Pages.
2. Match incoming entries against the restored feed by GUID and by slug.
3. **Skip** any item that is already localized at the current render version.
4. Only **fetch and render entries that are new** (or that need a render upgrade).
5. If nothing new was processed, the feed and pages are left untouched.

So each run does *not* re-download or re-render the whole feed; it only does work
for newly added articles.

### 2. Faithful web reproduction (real text, not a screenshot)

Instead of embedding a flat full-page image, every PDF page is rendered to its
own **SVG** with `page.get_svg_image(text_as_path=False)`. That means:

- The original **layout, charts, tables, colors and positioning are reproduced
  exactly** (vector, sharp at any zoom / width).
- The body **text stays real and selectable / searchable** — it is not flattened
  into a picture.
- Each page SVG is isolated in its own file and embedded responsively in the
  item page; a collapsible "Plain text" section is included for search and
  accessibility.

For born-digital PDFs (which DB Research reports are) this yields real,
selectable text. For truly scanned / image-only PDFs the page degrades to the
embedded raster inside the SVG; adding an OCR text layer is the natural next
research step.

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
| `DBRESEARCH_LAB_MAX_PAGES` | `60` | Max PDF pages rendered per item |
| `DBRESEARCH_LAB_FORCE_REBUILD` | `0` | `1` rebuilds all items (ignores cache/skip) |
| `DBRESEARCH_LAB_BROWSER_FALLBACK` | `1` | `0` disables the Playwright PDF capture fallback |

## CI

The `update-rss.yml` workflow runs this builder as an **additive, non-fatal**
step after the production feeds are built and validated (`... || echo`), so a lab
failure can never break production. No extra dependencies are required — it uses
the already-installed PyMuPDF.
