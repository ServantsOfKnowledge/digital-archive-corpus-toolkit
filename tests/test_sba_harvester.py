import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_direct_ia_pdf_url(self):
        self.assertTrue(sba.is_direct_ia_pdf_url(
            "https://archive.org/download/example/book%20one.pdf?download=1"
        ))
        self.assertFalse(sba.is_direct_ia_pdf_url("https://archive.org/details/example"))
        self.assertFalse(sba.is_direct_ia_pdf_url("https://example.org/book.pdf"))

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

    def test_download_uses_sba_compatible_accept_header(self):
        with tempfile.TemporaryDirectory() as directory:
            record = {
                "handle": "20.500.12497/1",
                "source_url": sba.BASE_URL + "/handle/20.500.12497/1",
                "title": "Title",
                "date": "2000",
                "classification": "download_candidate",
                "metadata": {"Title": "Title"},
            }
            payload = {"data": [{
                "name": "A.pdf", "internal_id": "abc", "sequenceId": 1,
                "islock": False,
                "html": '<iframe value=/dspace-mvc/bitstreamView?bitstream=abc&Type=application/pdf></iframe>',
            }]}
            response = Mock()
            response.headers = {"Content-Length": "3"}
            response.raise_for_status.return_value = None
            response.iter_content.return_value = [b"PDF"]
            with patch.object(sba, "get_json", return_value=payload), patch.object(
                sba, "session"
            ) as mocked:
                mocked.return_value.get.return_value = response
                states = sba.download_one(record, Path(directory), execute=True)
            request = mocked.return_value.get.call_args
            self.assertEqual(request.kwargs["headers"]["Accept"], "*/*")
            self.assertEqual(request.kwargs["headers"]["Referer"], record["source_url"])
            self.assertEqual(states[0]["status"], "downloaded")

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
            sba.make_ia_plan(db_path, output, sba.DEFAULT_IA_COLLECTION)
            plan = json.loads(output.read_text().strip())
            self.assertEqual(plan["ia_identifier"], "sba.1.1")
            self.assertEqual(plan["action"], "download_and_upload_candidate")
            self.assertEqual(plan["metadata"]["collection"], "ServantsOfKnowledge")

    def test_sba_identifier_and_legacy_identifier(self):
        record = {"handle": "20.500.12497/12700"}
        self.assertEqual(sba.sba_ia_identifier(record), "sba.12700.1")
        self.assertEqual(
            sba.sba_ia_identifier(record, sba.LEGACY_IA_IDENTIFIER_PREFIX),
            "apu.sba.12700.1",
        )

    def test_find_existing_ia_item_by_source_provenance(self):
        record = {
            "handle": "20.500.12497/12700",
            "source_url": "https://schoolbooksarchive.azimpremjiuniversity.edu.in/handle/20.500.12497/12700",
        }
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {"docs": [{"identifier": "older-custom-identifier"}]}
        }
        with patch.object(sba, "ia_exists", side_effect=[False, False]), patch.object(
            sba, "session"
        ) as mocked:
            mocked.return_value.get.return_value = response
            existing = sba.find_existing_ia_item(record)
        self.assertEqual(existing, "older-custom-identifier")
        params = mocked.return_value.get.call_args.kwargs["params"]
        self.assertIn('originalurl:"https://schoolbooksarchive', params["q"])
        self.assertIn('identifier-access:"20.500.12497/12700"', params["q"])

    def test_find_existing_ia_item_prefers_identifier_without_search(self):
        record = {
            "handle": "20.500.12497/12700",
            "source_url": "https://example/handle/20.500.12497/12700",
        }
        with patch.object(sba, "ia_exists", return_value=True), patch.object(
            sba, "session"
        ) as mocked:
            existing = sba.find_existing_ia_item(record)
        self.assertEqual(existing, "sba.12700.1")
        mocked.assert_not_called()

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

    def test_prepare_upload_files_merges_pdf_parts_in_sequence(self):
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            self.skipTest("pypdf is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "handle": "20.500.12497/1",
                "title": "Multipart",
                "classification": "download_candidate",
            }
            folder = sba.item_directory(root, record)
            folder.mkdir(parents=True)
            states = []
            for sequence, pages in ((1, 2), (2, 3)):
                path = folder / f"{sequence:03d}_part{sequence}.pdf"
                writer = PdfWriter()
                for _ in range(pages):
                    writer.add_blank_page(width=612, height=792)
                with path.open("wb") as stream:
                    writer.write(stream)
                writer.close()
                states.append({
                    "bitstream_id": str(sequence),
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": sba.sha256_file(path),
                    "status": "downloaded",
                })
            sba.write_json_atomic(folder / "manifest.json", {
                "complete": True, "files": states, "updated_at": sba.utc_now(),
            })

            upload_files, assembly = sba.prepare_upload_files(record, root)
            self.assertEqual([path.name for path in upload_files], ["sba.1.1.pdf"])
            self.assertEqual(len(PdfReader(str(upload_files[0])).pages), 5)
            self.assertEqual([item["name"] for item in assembly["inputs"]], [
                "001_part1.pdf", "002_part2.pdf",
            ])
            self.assertEqual(assembly["output"]["pages"], 5)
            self.assertTrue((folder / "001_part1.pdf").exists())
            self.assertTrue((folder / "002_part2.pdf").exists())

            # A second call validates and reuses the completed assembly.
            reused_files, reused = sba.prepare_upload_files(record, root)
            self.assertEqual(reused_files, upload_files)
            self.assertEqual(reused["output"]["sha256"], assembly["output"]["sha256"])

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

    def test_full_enrichment_refresh_resumes_on_plain_command(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            db = sba.connect_db(db_path)
            for number in range(1, 4):
                db.execute(
                    """INSERT INTO items
                       (handle, uuid, source_url, title, type, date, list_json,
                        metadata_json, bitstreams_json, ia_urls_json,
                        ia_identifiers_json, classification, listed_at, enriched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"20.500.12497/{number}", f"uuid-{number}",
                        f"https://example/{number}", f"Title {number}", "Book", "2000", "{}",
                        json.dumps({"Title": f"Old {number}"}), "[]", "[]", "[]",
                        "download_candidate", sba.utc_now(), "2000-01-01T00:00:00+00:00",
                    ),
                )
            db.commit()
            db.close()

            def refreshed(row):
                return {
                    "handle": row["handle"],
                    "metadata": {"Title": "Refreshed"},
                    "bitstreams": [], "ia_urls": [], "ia_identifiers": [],
                    "classification": "download_candidate",
                }

            with patch.object(sba, "fetch_details", side_effect=refreshed):
                sba.enrich(db_path, workers=1, limit=1, refresh=True)
                db = sba.connect_db(db_path)
                marker = sba.get_state(db, "enrich_refresh_started_at")
                self.assertTrue(marker)
                db.close()
                sba.enrich(db_path, workers=1, limit=1, refresh=False)
                sba.enrich(db_path, workers=1, limit=1, refresh=False)

            db = sba.connect_db(db_path)
            self.assertEqual(sba.get_state(db, "enrich_refresh_started_at"), "")
            refreshed_count = db.execute(
                "SELECT COUNT(*) FROM items WHERE metadata_json LIKE '%Refreshed%'"
            ).fetchone()[0]
            self.assertEqual(refreshed_count, 3)
            db.close()

    def test_status_separates_metadata_and_direct_pdf_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            db = sba.connect_db(db_path)
            fixtures = [
                (
                    1,
                    {"Source Website URL": "https://archive.org/details/source-one"},
                    [],
                    ["https://archive.org/details/source-one"],
                    ["source-one"],
                ),
                (
                    2,
                    {"Title": "Second"},
                    [{"uri": "https://archive.org/download/source-two/book.pdf"}],
                    ["https://archive.org/download/source-two/book.pdf"],
                    ["source-two"],
                ),
                (3, None, [], [], []),
            ]
            for number, metadata, bitstreams, urls, identifiers in fixtures:
                db.execute(
                    """INSERT INTO items
                       (handle, uuid, source_url, title, type, date, list_json,
                        metadata_json, bitstreams_json, ia_urls_json,
                        ia_identifiers_json, classification, listed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"20.500.12497/{number}", f"uuid-{number}",
                        f"https://example/{number}", f"Title {number}", "Book", "2000", "{}",
                        json.dumps(metadata) if metadata is not None else None,
                        json.dumps(bitstreams), json.dumps(urls), json.dumps(identifiers),
                        "record_only_ia_linked" if urls else "metadata_pending", sba.utc_now(),
                    ),
                )
            db.commit()
            db.close()
            stats = sba.collect_status(db_path)
            self.assertEqual(stats["inventory_items"], 3)
            self.assertEqual(stats["enriched_items"], 2)
            self.assertEqual(stats["archive_org_linked_items"], 2)
            self.assertEqual(stats["archive_org_metadata_source_items"], 1)
            self.assertEqual(stats["archive_org_bitstream_source_items"], 1)
            self.assertEqual(stats["direct_archive_org_pdf_items"], 1)
            self.assertEqual(stats["unique_archive_org_identifiers"], 2)


if __name__ == "__main__":
    unittest.main()
