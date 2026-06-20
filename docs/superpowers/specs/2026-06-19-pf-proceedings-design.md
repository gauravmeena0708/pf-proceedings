# pf-proceedings — EPFO Court Orders Search (Design)

Date: 2026-06-19

## Goal

An unofficial, static, client-side search engine over EPFO quasi-judicial
orders (7A, 7B, 7C, 14B & 7Q, 26B, 8PS), mirroring the existing `pf-circular`
and `pf-apptfr` research repos. No backend: a Python scraper produces JSON, a
single static HTML page searches it in the browser.

## Source

`POST https://eproceedings.epfindia.gov.in/epfo/public/judgments_report.php`

The page hosts several search modes (order date, diary no, party/establishment
name, presiding officer, case type). The **case type** mode with
`schemaname=all` returns every published order of that type across all offices,
so iterating the case types gives full coverage in 8 requests.

Request body (example): `case_type=12&schemaname=all`.

Case-type dropdown values → code:

| value(s)   | code          |
|------------|---------------|
| 12, 1, 15  | 7A            |
| 7          | 7C            |
| 3          | 26B (FUND)    |
| 11         | 8PS (PENSION) |
| 6          | 14B and 7Q    |
| 5          | 7B            |

Result grid is `table.example`. Each data row: `td[0]` est code, `td[1]` an
anchor whose `popsurety_pet_adv_name('<pdf url>')` JS call carries the order
PDF link plus the diary number text, `td[2]` establishment name (wrapped in
`<h7><font><center>`), `td[3]` order date (`dd/mm/yyyy`).

## Components

### 1. Scraper — `fetch.py` (pf-apptfr modern style)

- Retrying `requests.Session` (handles the portal's intermittent 5xx).
- `parse_records(html, case_type_id, code)` — pure function over the grid,
  unit-tested against fixtures.
- Iterate all case types, then `merge_records` de-duplicates by
  `(est_code, diary_no, pdf_link)` and unions `case_type_codes` (the same order
  appears under more than one 7A register).
- `validate_records` guards a minimum count before replacing the dataset.
- `plausible_year` discards the portal's malformed dates (e.g. `22022`, `2034`),
  falling back to the diary-number year, bounded to `[1995, current_year+1]`.
- **Sharding** (the portal holds ~450k orders, ~86% of them *14B & 7Q*): the
  main file carries only the ~63k judgment orders; pure *14B & 7Q* records are
  written to `shards/14b_7q-<year>.json`, one per year, loaded by the page on
  demand. Records are stored as compact arrays (column order in `fields`) with
  the shared `pdf_prefix` factored out.
- Atomic write of `proceedings-data.json`: `{ source_url, fetched_at,
  record_count, main_count, shard_count, with_pdf, case_type_counts,
  pdf_prefix, fields, shards, records }`. Shard files mirror the row schema.

### 2. Optional OCR indexer — `fetch.py --action index`

- Mirrors `pf-circular`: download PDF, render page 1 via PyMuPDF, OCR with
  Tesseract, store in `index-data.json` keyed by PDF URL.
- Incremental and capped (`--index-limit`, default 50). Heavy imports are lazy
  so the core fetch path and tests need no OCR dependencies. Off by default.

### 3. Frontend — `index.html`

- Adapts the `pf-circular` static search UI. Loads `proceedings-data.json`
  (+ `index-data.json` if present), rehydrates the compact rows and
  reconstructs PDF links from `pdf_prefix`, debounced client-side search with
  `"phrase"` / `+include` / `-exclude`, case-type and year filters, pagination,
  URL `?query=` sync, and OCR snippet highlighting when available.
- Selecting case type *14B & 7Q* requires a year; the matching shard is then
  fetched on demand and cached, and search runs within it.
- Columns: Establishment (+ est code) · Diary No · Order Date · Case Type ·
  Order (PDF). Carries the same "Unofficial" badging and trust note.

### 4. Automation & meta

- `.github/workflows/fetch.yaml` — weekly cron: tests → fetch → validate →
  commit on change.
- `.github/workflows/index.yaml` — manual-only OCR indexing (slow), commits
  `index-data.json`.
- `requirements.txt` (OCR deps commented), `.gitignore`, `README.md`, `LICENSE`.

## Decisions

- **Index depth:** metadata now + optional incremental OCR (not full-text from
  day one — too heavy for thousands of PDFs).
- **Volume:** shard *14B & 7Q* by year, loaded on demand, to keep the main page
  load small (~7 MB) while retaining full coverage.
- **Coverage:** case-type iteration with `schemaname=all`; no per-office crawl.
  Orders with no case type (date/name-only searches) are out of scope.
- **Office filter:** dropped — the all-offices response does not tag office and
  the est-code prefix is not a reliable office map. Raw `est_code` is kept and
  is searchable.

## Out of scope

Per-office crawling, multi-page order OCR, a server/API, and the non-case-type
search modes.
