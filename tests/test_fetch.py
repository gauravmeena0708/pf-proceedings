import json
import tempfile
import unittest
from pathlib import Path

from fetch import (
    FetchResult,
    FIELDS,
    is_main_record,
    merge_records,
    parse_records,
    pdf_filename,
    pdf_url,
    plausible_year,
    save_dataset,
)


# Mirrors the real ``table.example`` grid returned by judgments_report.php:
# td[0] est code, td[1] anchor with the PDF url in popsurety_pet_adv_name() plus
# the diary number, td[2] establishment wrapped in <h7><font><center>, td[3] date.
SAMPLE_HTML = """
<table class="example">
  <thead>
    <tr><th>Est Code</th><th>Diary No</th>
        <th>Establishment/Employer</th><th>Order Date</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>NEAGT1660182000</td>
      <td>
        <a href="javascript:popsurety_pet_adv_name('https://eproceedings.epfindia.gov.in/uploads/judgement/180610000122019-1.pdf');">
          12/2019</td>
      <td><h7><font color='red'><center>EPS INTERGLOBE PRIVATE LIMITED</center></font></h7></td>
      <td>26/04/2019</td>
    </tr>
    <tr>
      <td>NEAGT1073770000</td>
      <td>11/2019</td>
      <td><h7><font color='red'><center>ARISTO TEXCON PVT LTD</center></font></h7></td>
      <td>16/06/2020</td>
    </tr>
  </tbody>
</table>
"""


class ParseRecordsTests(unittest.TestCase):
    def test_extracts_fields_and_pdf_link(self):
        records = parse_records(SAMPLE_HTML, "12", "7A")
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first["est_code"], "NEAGT1660182000")
        self.assertEqual(first["diary_no"], "12/2019")
        self.assertEqual(first["establishment"], "EPS INTERGLOBE PRIVATE LIMITED")
        self.assertEqual(first["order_date"], "26/04/2019")
        self.assertEqual(first["year"], "2019")
        self.assertEqual(first["case_type_code"], "7A")
        self.assertTrue(first["pdf_link"].endswith("180610000122019-1.pdf"))

    def test_row_without_pdf_link(self):
        records = parse_records(SAMPLE_HTML, "12", "7A")
        self.assertIsNone(records[1]["pdf_link"])
        self.assertEqual(records[1]["diary_no"], "11/2019")

    def test_missing_table_returns_empty(self):
        self.assertEqual(parse_records("<html><body>nope</body></html>", "7", "7C"), [])


class YearAndPdfTests(unittest.TestCase):
    def test_year_prefers_order_date_then_diary(self):
        self.assertEqual(plausible_year("26/04/2021", "12/2019"), "2021")
        self.assertEqual(plausible_year("", "12/2019"), "2019")
        self.assertEqual(plausible_year("", ""), "")

    def test_year_rejects_implausible_dates(self):
        # Real portal typo: fall back to the diary year, not 8202.
        self.assertEqual(plausible_year("08/04/8202", "10/2024"), "2024")
        self.assertEqual(plausible_year("08/04/8202", ""), "")

    def test_pdf_filename_roundtrip(self):
        link = "https://eproceedings.epfindia.gov.in/uploads/judgement/abc-1.pdf"
        self.assertEqual(pdf_filename(link), "abc-1.pdf")
        self.assertEqual(pdf_url(pdf_filename(link)), link)
        self.assertEqual(pdf_filename(None), "")
        self.assertEqual(pdf_url("https://other/x.pdf"), "https://other/x.pdf")


class MergeAndSaveTests(unittest.TestCase):
    def test_merge_dedupes_and_unions_case_types(self):
        merged = merge_records([
            FetchResult("12", "7A", parse_records(SAMPLE_HTML, "12", "7A")),
            FetchResult("1", "7A", parse_records(SAMPLE_HTML, "1", "7A")),
            FetchResult("7", "7C", parse_records(SAMPLE_HTML, "7", "7C")),
        ])
        self.assertEqual(len(merged), 2)
        first = next(r for r in merged if r["est_code"] == "NEAGT1660182000")
        self.assertEqual(first["case_type_codes"], ["7A", "7C"])

    def test_main_vs_sharded_split(self):
        only_damages = {"case_type_codes": ["14B and 7Q"]}
        also_7a = {"case_type_codes": ["14B and 7Q", "7A"]}
        self.assertFalse(is_main_record(only_damages))
        self.assertTrue(is_main_record(also_7a))

    def test_save_dataset_writes_main_and_year_shards(self):
        records = merge_records([
            FetchResult("12", "7A", parse_records(SAMPLE_HTML, "12", "7A")),
            FetchResult("6", "14B and 7Q", [
                {"id": "a", "est_code": "X", "diary_no": "1/2022",
                 "establishment": "ALPHA", "order_date": "01/01/2022",
                 "year": "2022", "case_type_code": "14B and 7Q",
                 "case_type_id": "6", "pdf_link": None,
                 "case_type_codes": ["14B and 7Q"]},
                {"id": "b", "est_code": "Y", "diary_no": "2/2023",
                 "establishment": "BETA", "order_date": "01/01/2023",
                 "year": "2023", "case_type_code": "14B and 7Q",
                 "case_type_id": "6", "pdf_link": None,
                 "case_type_codes": ["14B and 7Q"]},
            ]),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "proceedings-data.json"
            shards = Path(tmp) / "shards"
            payload = save_dataset(records, out, shards)

            self.assertEqual(payload["record_count"], 4)
            self.assertEqual(payload["main_count"], 2)   # the two 7A rows
            self.assertEqual(payload["shard_count"], 2)  # the two damages rows
            self.assertEqual(payload["fields"], FIELDS)
            self.assertEqual(payload["case_type_counts"]["7A"], 2)
            self.assertEqual(payload["case_type_counts"]["14B and 7Q"], 2)

            manifest = payload["shards"]["14B and 7Q"]
            self.assertEqual(set(manifest), {"2022", "2023"})
            shard = json.loads((Path(tmp) / manifest["2022"]["file"]).read_text())
            self.assertEqual(shard["count"], 1)
            self.assertEqual(shard["records"][0][FIELDS.index("establishment")], "ALPHA")

            on_disk = json.loads(out.read_text())
            self.assertEqual(len(on_disk["records"]), 2)


if __name__ == "__main__":
    unittest.main()
