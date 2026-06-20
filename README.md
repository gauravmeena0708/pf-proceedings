# EPFO Order Archive (pf-proceedings)

An **unofficial**, community-built search engine over the quasi-judicial orders
published on the EPFO e-proceedings portal
([`judgments_report.php`](https://eproceedings.epfindia.gov.in/epfo/public/judgments_report.php)).

It scrapes order metadata into a single JSON file and ships a static,
client-side search page (`index.html`) — no backend, no database. An optional
OCR step makes the order PDFs full-text searchable.

This project is **not affiliated with or endorsed by EPFO**. Always verify any
order against the official portal.

## What it covers

The portal lets you search orders by *case type* with the office set to *all*,
which returns every published order of that type across all EPFO offices. The
scraper iterates the case types and merges the results:

| Case type | Description            |
|-----------|------------------------|
| 7A        | Determination of dues  |
| 7B        | Review                 |
| 7C        | Interest on review     |
| 14B & 7Q  | Damages and interest   |
| 26B       | Fund                   |
| 8PS       | Pension                |

Each order record contains: establishment code, diary number,
establishment/employer name, order date, case type(s), derived year, and a
direct link to the order PDF when available.

> Coverage note: records are gathered via the case-type search. Orders that
> carry no case type on the portal (only reachable via the date / party-name /
> officer searches) are out of scope for this archive.

### A note on size and sharding

The portal holds **~450,000 orders**, and ~86% of them are routine *14B & 7Q*
damages/interest orders. Loading all of that into the browser at once would be
far too heavy, so the output is split:

- **`proceedings-data.json`** — the ~63k "judgment" orders (7A / 7B / 7C / 26B /
  8PS), ~7 MB, loaded up front.
- **`shards/14b_7q-<year>.json`** — the bulky *14B & 7Q* orders, one file per
  year. The page loads a single year's shard only when you filter to that case
  type and pick a year.

Records are stored as compact arrays (column order in the `fields` key) with the
shared PDF URL prefix factored out, keeping the files small.

## How it works

1. **Fetch (`fetch.py --action fetch`)** — POSTs `case_type=<id>&schemaname=all`
   for each case type, parses the `table.example` results grid, de-duplicates
   across case types (the same order can appear under more than one 7A
   register), validates a minimum record count, sanitizes obviously wrong dates,
   and writes `proceedings-data.json` plus the per-year shards atomically.
2. **Index (`fetch.py --action index`, optional)** — incrementally downloads
   each order PDF, OCRs its first page with Tesseract, and stores the text in
   `index-data.json`, keyed by the PDF URL. Capped per run
   (`--index-limit`, default 50) so it can be run repeatedly without
   hammering the server. Disabled by default; the archive works without it.
3. **Search (`index.html`)** — loads the JSON files in the browser and provides
   debounced full-text search with `"exact phrase"`, `+include`, and `-exclude`
   operators, plus case-type and year filters and pagination.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For the optional OCR indexer, also uncomment the OCR packages in
`requirements.txt` and install the Tesseract engine:

- **Ubuntu/Debian:** `sudo apt-get install -y tesseract-ocr`
- **macOS:** `brew install tesseract`
- **Windows:** installer from the [Tesseract releases](https://github.com/UB-Mannheim/tesseract/wiki)

## Usage

```bash
python fetch.py                       # fetch metadata -> proceedings-data.json
python fetch.py --action index        # OCR up to 50 new PDFs -> index-data.json
python fetch.py --action index --index-limit 200
python fetch.py --action all          # fetch then index
```

Serve the folder over HTTP and open it (the page fetches JSON, so `file://`
won't work):

```bash
python -m http.server 8000   # then visit http://localhost:8000
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The parser tests run against inline HTML fixtures that mirror the portal's
real markup — no network access required.

## Automation

`.github/workflows/fetch.yaml` refreshes `proceedings-data.json` weekly:
runs the tests, fetches, validates the JSON, and commits if anything changed.
OCR indexing is intentionally left as a manual / separate step because it is
slow and dependency-heavy.

## Data files

- **`proceedings-data.json`** — `{ source_url, fetched_at, record_count,
  main_count, shard_count, with_pdf, case_type_counts, pdf_prefix, fields,
  shards, records: [...] }`. `records` are compact arrays ordered by `fields`;
  `shards["14B and 7Q"]` maps each year to `{ file, count }`.
- **`shards/14b_7q-<year>.json`** — `{ case_type, year, count, fields,
  pdf_prefix, records: [...] }` for one year of *14B & 7Q* orders.
- **`index-data.json`** — `{ "<pdf_url>": { ocr_content, indexed_at }, ... }`
  (only present once the index action has run).

## License

MIT — see [LICENSE](LICENSE).
