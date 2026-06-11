# DB Research PDF extraction — experimental "lab" copy

This folder contains an **experimental, side-by-side copy** of the DB Research
feed builder used to research better PDF body-text extraction that **preserves
headings, tables and figures** and renders them as real web content instead of
full-page raster images.

Nothing here touches the production pipeline:

| | Production | Lab (experiment) |
|---|---|---|
| Script | `scripts/build_dbresearch_feed.py` | `scripts/build_dbresearch_lab_feed.py` |
| Feed file | `dbresearch.xml` | `dbresearch_lab.xml` |
| Item pages | `item/dbresearch/...` | `item/dbresearch_lab/...` |
| Branch | `main` (scheduled CI) | `dbresearch-lab-experiment` (manual only) |

The production `dbresearch` feed, its `item/dbresearch/` pages and the scheduled
GitHub Action are intentionally **not modified**.

## Why the current output is weak

The DB Research PDFs are **born-digital** (they have a real text layer), not
scanned. The current builder:

1. extracts a flat list of paragraphs with pypdf / PyMuPDF, then
2. rasterizes **every page** to a big PNG (`render_pdf_pages`) and stacks them.

So tables become prose, charts are lost from the text, and each item ships
20-80 PNGs of 200-700 KB. "Format preserved" only in the sense of a photo of
each page (not selectable, not responsive, huge).

## What the lab builder does instead

Primary strategy (CPU-only, CI-friendly — uses PyMuPDF, already a dependency):

1. **Reading-order structured parse** via `page.get_text("dict")`; heading
   levels are inferred from font size relative to the page's median body size
   (`<h2>/<h3>` vs `<p>`).
2. **Tables** are detected with `page.find_tables()` and emitted as real
   `<table>` HTML; their bounding boxes suppress duplicate prose.
3. **Figures/charts** are located from embedded raster images
   (`page.get_images` + `get_image_rects`) and clustered vector drawings
   (`page.cluster_drawings()`), then **cropped** to individual PNGs and embedded
   inline `<figure>` at their true position — much smaller than full-page rasters
   and placed in context.
4. **Image-only / scanned pages** (almost no extractable text) fall back to a
   single full-page raster for that page only, with an optional OCR hook.

The result is a semantic, responsive HTML article: selectable text, real tables,
in-context cropped charts, with the original PDF still linked/downloadable.

## Run it locally

```bash
pip install requests feedparser playwright pypdf pymupdf
python -m playwright install chromium   # only needed for the DB viewer fallback

python scripts/build_dbresearch_lab_feed.py site "https://kobolibra.github.io/rss-pages"
# Output: site/dbresearch_lab.xml + site/item/dbresearch_lab/<slug>/index.html
```

Env knobs (all optional): `DBRESEARCH_LAB_MAX_ITEMS`, `DBRESEARCH_LAB_FEED_NAME`,
`DBRESEARCH_LAB_FIG_SCALE`, `DBRESEARCH_LAB_MIN_PAGE_TEXT`,
`DBRESEARCH_LAB_RASTER_FALLBACK` (`1`/`0`).

## Higher-fidelity options (heavier, not enabled here)

If you later want the best possible fidelity (esp. complex tables / equations /
truly scanned PDFs), route the PDF through a document-AI model and keep this
structured HTML as the fast fallback:

- **Marker** (`marker-pdf`): PDF -> Markdown/HTML/JSON, extracts images, tables,
  equations; `--force_ocr` for scanned, `--use_llm` to boost accuracy.
- **MinerU**: multi-model fusion, complex tables as HTML, formulas as LaTeX,
  auto-detects scanned docs (84-language OCR). GPU recommended.
- **Mistral OCR API**: hosted SOTA OCR -> Markdown, auto-cuts images. No GPU.

These need large models / a GPU / an API key, so they are unsuitable for the
twice-daily free GitHub Actions runner without extra infrastructure; that is why
the default lab path is the lightweight PyMuPDF parser above.
