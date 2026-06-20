#!/usr/bin/env python3
"""Fetch EPFO quasi-judicial court order metadata (and optionally OCR the orders).

The EPFO e-proceedings portal exposes published orders (7A, 7B, 7C, 14B & 7Q,
26B, 8PS) through ``judgments_report.php``. Searching by *case type* with the
office set to ``all`` returns every published order of that type across all
EPFO offices, so iterating the handful of case types gives full coverage.

The portal holds ~450k orders, ~86% of them routine *14B & 7Q* damages/interest
orders. To keep the static search page fast, the output is split:

* ``proceedings-data.json`` -- the ~63k "judgment" orders
  (7A / 7B / 7C / 26B / 8PS), loaded up front.
* ``shards/14b_7q-<year>.json`` -- the bulky *14B & 7Q* orders, one file per
  year, loaded by the page on demand.

Records are stored as compact arrays (see ``FIELDS``) with the shared PDF URL
prefix factored out, to keep the files small.

Two actions:

* ``fetch`` (default) -- scrape order metadata into the JSON files above.
* ``index``           -- optional, incremental OCR of the order PDFs into
                         ``index-data.json`` (capped per run; off by default).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://eproceedings.epfindia.gov.in/epfo/public/judgments_report.php"
PDF_PREFIX = "https://eproceedings.epfindia.gov.in/uploads/judgement/"
DEFAULT_OUTPUT = Path("proceedings-data.json")
SHARD_DIR = Path("shards")
INDEX_OUTPUT = Path("index-data.json")
MIN_EXPECTED_RECORDS = 1_000
MAX_URLS_TO_INDEX_PER_RUN = 50
# Bound for sane years; the source contains typos like 22022 / 2034. Orders are
# never future-dated, so cap just past the current year.
MIN_YEAR = 1995
MAX_YEAR = datetime.now(timezone.utc).year + 1

# Case type that is bulky enough to be sharded out of the main file by year.
SHARDED_CASE_TYPE = "14B and 7Q"

# Compact row schema shared with the frontend.
FIELDS = ["establishment", "est_code", "diary_no", "order_date", "year",
          "case_type_codes", "pdf"]

# Case-type dropdown values on the portal -> human readable code.
# Note: 12, 1 and 15 are three distinct 7A sub-registers; the same order may
# legitimately appear under more than one id, so records are de-duplicated and
# their case-type codes merged.
CASE_TYPES: dict[str, str] = {
    "12": "7A",
    "1": "7A",
    "15": "7A",
    "7": "7C",
    "3": "26B (FUND)",
    "11": "8PS (PENSION)",
    "6": "14B and 7Q",
    "5": "7B",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
        "Gecko/20100101 Firefox/151.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://eproceedings.epfindia.gov.in",
    "Referer": BASE_URL,
}

DATE_PATTERN = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
PDF_FROM_JS = re.compile(r"popsurety_pet_adv_name\(\s*'([^']+)'")


@dataclass
class FetchResult:
    case_type_id: str
    case_type_code: str
    records: list[dict] = field(default_factory=list)


def build_session() -> requests.Session:
    """Create a retrying HTTP session suitable for EPFO's intermittent server."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def make_record_id(est_code: str, diary_no: str, pdf_link: str | None) -> str:
    fingerprint = "|".join((est_code, diary_no, pdf_link or ""))
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]


def plausible_year(order_date: str, diary_no: str) -> str:
    """A year for filtering/sorting, ignoring the portal's malformed dates.

    Prefers the order-date year, then the diary-number year, accepting only
    values inside a sane range (the source contains typos like ``08/04/8202``).
    """
    match = DATE_PATTERN.search(order_date or "")
    if match and MIN_YEAR <= int(match.group(3)) <= MAX_YEAR:
        return match.group(3)
    match = re.search(r"/(\d{4})\b", diary_no or "")
    if match and MIN_YEAR <= int(match.group(1)) <= MAX_YEAR:
        return match.group(1)
    return ""


def pdf_filename(pdf_link: str | None) -> str:
    """Store only the filename when the link uses the standard prefix."""
    if not pdf_link:
        return ""
    if pdf_link.startswith(PDF_PREFIX):
        return pdf_link[len(PDF_PREFIX):]
    return pdf_link


def pdf_url(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("http"):
        return stored
    return PDF_PREFIX + stored


def parse_records(html: str, case_type_id: str, case_type_code: str) -> list[dict]:
    """Parse the ``table.example`` results grid into order records."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="example")
    if table is None:
        return []

    body = table.find("tbody") or table
    records = []
    for row in body.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue

        est_code = normalize_space(cells[0].get_text(" ", strip=True))
        if not est_code:
            continue

        link = cells[1].find("a", href=True)
        pdf_link = None
        if link is not None:
            match = PDF_FROM_JS.search(link["href"])
            if match:
                pdf_link = urljoin(BASE_URL, match.group(1))
        diary_no = normalize_space(cells[1].get_text(" ", strip=True))

        establishment = normalize_space(cells[2].get_text(" ", strip=True))
        order_date = normalize_space(cells[3].get_text(" ", strip=True))

        records.append(
            {
                "id": make_record_id(est_code, diary_no, pdf_link),
                "est_code": est_code,
                "diary_no": diary_no,
                "establishment": establishment,
                "order_date": order_date,
                "year": plausible_year(order_date, diary_no),
                "case_type_code": case_type_code,
                "case_type_id": case_type_id,
                "pdf_link": pdf_link,
            }
        )

    return records


def fetch_case_type(
    session: requests.Session, case_type_id: str, case_type_code: str
) -> FetchResult:
    response = session.post(
        BASE_URL,
        data={"case_type": case_type_id, "schemaname": "all"},
        timeout=120,
    )
    response.raise_for_status()
    records = parse_records(response.text, case_type_id, case_type_code)
    print(f"case_type {case_type_id} ({case_type_code}): {len(records):,} rows")
    return FetchResult(case_type_id, case_type_code, records)


def merge_records(results: Iterable[FetchResult]) -> list[dict]:
    """De-duplicate orders across case types, merging their case-type codes."""
    merged: dict[str, dict] = {}
    for result in results:
        for record in result.records:
            existing = merged.get(record["id"])
            if existing is None:
                record = dict(record)
                record["case_type_codes"] = sorted({record["case_type_code"]})
                merged[record["id"]] = record
            else:
                codes = set(existing["case_type_codes"])
                codes.add(record["case_type_code"])
                existing["case_type_codes"] = sorted(codes)
    return list(merged.values())


def parse_date(value: str) -> datetime:
    match = DATE_PATTERN.search(value or "")
    if not match:
        return datetime.min
    try:
        dt = datetime.strptime(match.group(0), "%d/%m/%Y")
    except ValueError:
        return datetime.min
    return dt if MIN_YEAR <= dt.year <= MAX_YEAR else datetime.min


def record_to_row(record: dict) -> list:
    return [
        record["establishment"],
        record["est_code"],
        record["diary_no"],
        record["order_date"],
        record["year"],
        record["case_type_codes"],
        pdf_filename(record.get("pdf_link")),
    ]


def is_main_record(record: dict) -> bool:
    """A record stays in the main file unless it is *only* the sharded type."""
    return record["case_type_codes"] != [SHARDED_CASE_TYPE]


def write_json_atomic(payload: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary_name, output)
    except Exception:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise


def save_dataset(records: list[dict], output: Path, shard_dir: Path) -> dict:
    """Write the main file plus per-year shards for the bulky case type."""
    records = sorted(
        records, key=lambda item: parse_date(item.get("order_date", "")), reverse=True
    )
    main = [r for r in records if is_main_record(r)]
    sharded = [r for r in records if not is_main_record(r)]

    # Group the sharded type by year.
    by_year: dict[str, list[dict]] = {}
    for record in sharded:
        by_year.setdefault(record["year"] or "unknown", []).append(record)

    # Rebuild the shard directory from scratch so stale years disappear.
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)

    shard_manifest: dict[str, dict] = {}
    for year, year_records in sorted(by_year.items(), reverse=True):
        # Path on disk lives under shard_dir; the manifest path is site-relative
        # (relative to the main file) so the frontend can fetch it directly.
        relative = f"{shard_dir.name}/14b_7q-{year}.json"
        write_json_atomic(
            {
                "case_type": SHARDED_CASE_TYPE,
                "year": year,
                "count": len(year_records),
                "fields": FIELDS,
                "pdf_prefix": PDF_PREFIX,
                "records": [record_to_row(r) for r in year_records],
            },
            shard_dir / f"14b_7q-{year}.json",
        )
        shard_manifest[year] = {"file": relative, "count": len(year_records)}

    case_counts: dict[str, int] = {}
    for record in records:
        for code in record["case_type_codes"]:
            case_counts[code] = case_counts.get(code, 0) + 1

    payload = {
        "source_url": BASE_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "main_count": len(main),
        "shard_count": len(sharded),
        "with_pdf": sum(1 for r in records if r.get("pdf_link")),
        "case_type_counts": dict(sorted(case_counts.items())),
        "pdf_prefix": PDF_PREFIX,
        "fields": FIELDS,
        "shards": {SHARDED_CASE_TYPE: shard_manifest},
        "records": [record_to_row(r) for r in main],
    }
    write_json_atomic(payload, output)
    return payload


def validate_records(records: list[dict], minimum: int) -> list[dict]:
    if len(records) < minimum:
        raise RuntimeError(
            f"Parsed only {len(records):,} records; expected at least "
            f"{minimum:,}. Refusing to replace the existing dataset."
        )
    return records


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            print(f"Warning: could not decode {path}; starting fresh.")
    return {}


def fetch_all(output: Path, shard_dir: Path, minimum_records: int) -> dict:
    session = build_session()
    results = [
        fetch_case_type(session, case_type_id, code)
        for case_type_id, code in CASE_TYPES.items()
    ]
    records = merge_records(results)
    print(f"merged to {len(records):,} unique orders")
    validate_records(records, minimum_records)
    payload = save_dataset(records, output, shard_dir)
    print(
        f"Saved {payload['main_count']:,} main + {payload['shard_count']:,} "
        f"sharded orders ({len(payload['shards'][SHARDED_CASE_TYPE])} year shards)"
    )
    return payload


# --- Optional OCR indexing (mirrors pf-circular; lazy heavy imports) ---------
def iter_pdf_urls(output: Path, shard_dir: Path) -> Iterable[str]:
    """All order PDF URLs across the main file and every shard."""
    main = load_json(output)
    prefix = main.get("pdf_prefix", PDF_PREFIX)
    fields = main.get("fields", FIELDS)
    pdf_index = fields.index("pdf")
    for row in main.get("records", []):
        if row[pdf_index]:
            yield pdf_url(row[pdf_index])
    for shard_file in sorted(shard_dir.glob("*.json")) if shard_dir.exists() else []:
        shard = load_json(shard_file)
        for row in shard.get("records", []):
            if row[pdf_index]:
                yield pdf_url(row[pdf_index])


def ocr_first_page(session: requests.Session, url: str) -> str | None:
    """Download a PDF and OCR its first page. Returns text, or None on failure."""
    import io

    import fitz  # PyMuPDF
    import pytesseract
    from PIL import Image

    try:
        response = session.get(url, timeout=120)
        response.raise_for_status()
        document = fitz.open(stream=response.content, filetype="pdf")
        if document.page_count == 0:
            document.close()
            return None
        pixmap = document.load_page(0).get_pixmap(dpi=300)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        text = pytesseract.image_to_string(image, lang="eng")
        document.close()
        return text.strip()
    except Exception as error:  # network, PDF, or OCR error -- skip and retry later
        print(f"  OCR failed for {url}: {error}")
        return None


def update_index(output: Path, shard_dir: Path, index_file: Path, limit: int) -> None:
    index = load_json(index_file)
    session = build_session()
    processed = 0
    for url in iter_pdf_urls(output, shard_dir):
        if processed >= limit:
            print(f"Reached the per-run cap of {limit} PDFs.")
            break
        if url in index:
            continue
        print(f"Indexing {url}")
        text = ocr_first_page(session, url)
        processed += 1
        if text is not None:
            index[url] = {
                "ocr_content": text,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }

    if processed:
        write_json_atomic(index, index_file)
        print(f"Indexed {processed} new PDFs into {index_file}")
    else:
        print("Nothing new to index (run the fetch action first?).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=["fetch", "index", "all"],
        default="fetch",
        help="fetch metadata (default), OCR-index PDFs, or both.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shard-dir", type=Path, default=SHARD_DIR)
    parser.add_argument("--index-output", type=Path, default=INDEX_OUTPUT)
    parser.add_argument("--min-records", type=int, default=MIN_EXPECTED_RECORDS)
    parser.add_argument("--index-limit", type=int, default=MAX_URLS_TO_INDEX_PER_RUN)
    args = parser.parse_args()

    if args.action in ("fetch", "all"):
        fetch_all(args.output, args.shard_dir, args.min_records)
    if args.action in ("index", "all"):
        update_index(args.output, args.shard_dir, args.index_output, args.index_limit)


if __name__ == "__main__":
    main()
