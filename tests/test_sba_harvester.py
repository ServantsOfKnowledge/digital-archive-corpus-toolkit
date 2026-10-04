import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sba_harvester as sba


class SbaHarvesterTests(unittest.TestCase):
    def test_clean_text_decodes_repeated_entities(self):
        self.assertEqual(sba.clean_text("A&amp;#x20;book&amp;#39;s"), "A book's")

    def test_extract_handle(self):
        self.assertEqual(
            sba.extract_handle("/handle/20.500.12497/12700"),
            "20.500.12497/12700",
        )

    def test_detect_ia_references(self):
        urls, identifiers = sba.detect_ia_references(
            {"Source Website URL": "https://archive.org/details/example-book"},
            [{"uri": "https://archive.org/download/example-book/book.pdf"}],
        )
        self.assertEqual(identifiers, ["example-book"])
        self.assertEqual(len(urls), 2)

    def test_normalize_bitstreams_records_stable_viewer_url(self):
        rows = sba.normalize_bitstreams(
            "20.500.12497/1",
            {"data": [{"name": "A.pdf", "internal_id": "abc", "html": "secret", "lock": "x"}]},
        )
        self.assertNotIn("html", rows[0])
        self.assertNotIn("lock", rows[0])
        self.assertIn("fileid=abc", rows[0]["viewer_url"])

    def test_normalize_bitstreams_extracts_download_url(self):
        rows = sba.normalize_bitstreams(
            "20.500.12497/1",
            {"data": [{
                "name": "A.pdf", "internal_id": "abc",
                "html": '<iframe value=/dspace-mvc/bitstreamView?bitstream=abc&Type=application/pdf></iframe>',
            }]},
        )
        self.assertEqual(
            rows[0]["download_url"],
            sba.BASE_URL + "/dspace-mvc/bitstreamView?bitstream=abc&Type=application/pdf",
        )

    def test_schema_and_ia_plan_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            output = Path(directory) / "plan.jsonl"
            db = sba.connect_db(db_path)
            db.execute(
                """INSERT INTO items
                   (handle, uuid, source_url, title, type, date, list_json,
                    metadata_json, bitstreams_json, ia_urls_json,
                    ia_identifiers_json, classification, listed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "20.500.12497/1", "uuid", "https://example/handle/1", "Title",
                    "Book", "2000", "{}", json.dumps({"Title": "Title"}), "[]",
                    "[]", "[]", "download_candidate", sba.utc_now(),
                ),
            )
            db.commit()
            db.close()
            sba.make_ia_plan(db_path, output, None)
            plan = json.loads(output.read_text().strip())
            self.assertEqual(plan["ia_identifier"], "apu.sba.1.1")
            self.assertEqual(plan["action"], "download_and_upload_candidate")


if __name__ == "__main__":
    unittest.main()
