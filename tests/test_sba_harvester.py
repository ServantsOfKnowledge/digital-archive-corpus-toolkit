import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sba_harvester as sba


class SbaHarvesterTests(unittest.TestCase):
    @staticmethod
    def listing_item(number):
        return {
            "itemurl": f"/handle/20.500.12497/{number}",
            "productName": f"uuid-{number}",
            "displayTitle": f"Title {number}",
            "displayType": "Book",
            "displayDate": "2000",
        }

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

    def test_inventory_resumes_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            first = {"data": [self.listing_item(1), self.listing_item(2)], "size": 4}
            second = {"data": [self.listing_item(3), self.listing_item(4)], "size": 4}
            with patch.object(sba, "get_json", return_value=first) as mocked:
                sba.inventory(db_path, page_size=2, max_items=2, delay=0)
                self.assertEqual(mocked.call_args.args[1]["start"], 0)
            db = sba.connect_db(db_path)
            self.assertEqual(sba.get_state(db, "inventory_next_start"), "2")
            db.close()
            with patch.object(sba, "get_json", return_value=second) as mocked:
                sba.inventory(db_path, page_size=2, max_items=2, delay=0)
                self.assertEqual(mocked.call_args.args[1]["start"], 2)
            db = sba.connect_db(db_path)
            self.assertEqual(sba.get_state(db, "inventory_next_start"), "0")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 4)
            db.close()

    def test_completed_manifest_is_not_selected_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "handle": "20.500.12497/1",
                "title": "Title",
                "classification": "download_candidate",
                "bitstreams": [{"internal_id": "abc", "islock": False}],
            }
            folder = sba.item_directory(root, record)
            file_path = folder / "A.pdf"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"PDF")
            sba.write_json_atomic(folder / "manifest.json", {
                "complete": True,
                "files": [{
                    "bitstream_id": "abc", "status": "downloaded",
                    "path": str(file_path), "size": 3,
                }],
            })
            self.assertTrue(sba.record_download_is_complete(record, root))
            file_path.write_bytes(b"broken")
            self.assertFalse(sba.record_download_is_complete(record, root))

    def test_download_limit_selects_next_incomplete_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "corpus"
            db_path = Path(directory) / "test.sqlite3"
            db = sba.connect_db(db_path)
            for number, bitstream_id in ((1, "done"), (2, "pending")):
                db.execute(
                    """INSERT INTO items
                       (handle, uuid, source_url, title, type, date, list_json,
                        metadata_json, bitstreams_json, ia_urls_json,
                        ia_identifiers_json, classification, listed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"20.500.12497/{number}", f"uuid-{number}",
                        f"https://example/{number}", f"Title {number}", "Book", "2000", "{}",
                        json.dumps({"Title": f"Title {number}"}),
                        json.dumps([{"internal_id": bitstream_id, "islock": False}]),
                        "[]", "[]", "download_candidate", sba.utc_now(),
                    ),
                )
            db.commit()
            first = sba.row_to_record(db.execute(
                "SELECT * FROM items WHERE handle='20.500.12497/1'"
            ).fetchone())
            db.close()
            folder = sba.item_directory(root, first)
            file_path = folder / "001_A.pdf"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"PDF")
            sba.write_json_atomic(folder / "manifest.json", {
                "complete": True,
                "files": [{
                    "bitstream_id": "done", "status": "downloaded",
                    "path": str(file_path), "size": 3,
                }],
            })
            with patch.object(
                sba, "download_one",
                return_value=[{"bitstream_id": "pending", "status": "planned"}],
            ) as mocked:
                sba.download_records(db_path, root, workers=1, limit=1, execute=False)
            selected = mocked.call_args.args[0]
            self.assertEqual(selected["handle"], "20.500.12497/2")


if __name__ == "__main__":
    unittest.main()
