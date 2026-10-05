import json
import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

import anuvada_harvester as ah


OAI_RECORD = """<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header><identifier>oai:anuvadasampada.azimpremjiuniversity.edu.in:65</identifier>
    <datestamp>2024-01-01T00:00:00Z</datestamp></header>
  <metadata><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/"
    xmlns:d="http://purl.org/dc/elements/1.1/">
    <d:title>ಶಿಕ್ಷಕ ತರಬೇತಿಯೇ ಕೀಲಿಕೈ</d:title>
    <d:creator>ಪ್ರಸಾದ್, ಇಂದು</d:creator>
    <d:identifier>https://anuvadasampada.azimpremjiuniversity.edu.in/65/1/A.pdf</d:identifier>
  </dc></metadata>
</record>"""


class AnuvadaHarvesterTests(unittest.TestCase):
    def test_source_parts(self):
        self.assertEqual(
            ah.source_parts("https://anuvadasampada.azimpremjiuniversity.edu.in/2252/3/A.pdf"),
            (2252, 3),
        )
        self.assertEqual(ah.source_parts("https://example.org/item"), (None, None))

    def test_established_identifier_conventions(self):
        self.assertEqual(ah.intended_identifier("hin", 3131, 1), "apu.anuvadasampada.hin.3131.1")
        self.assertEqual(ah.intended_identifier("kan", 65, 1), "apu.anuvadasampada.kan.65")
        self.assertEqual(ah.intended_identifier("", 65, 1), "")

    def test_parse_oai_record(self):
        record = ah.parse_oai_record(ET.fromstring(OAI_RECORD))
        self.assertEqual(record["record_id"], 65)
        self.assertEqual(record["dc"]["title"], ["ಶಿಕ್ಷಕ ತರಬೇತಿಯೇ ಕೀಲಿಕೈ"])
        self.assertFalse(record["deleted"])

    def test_parse_deleted_oai_record(self):
        node = ET.fromstring("""<record xmlns="http://www.openarchives.org/OAI/2.0/">
          <header status="deleted"><identifier>oai:x:9</identifier><datestamp>2024-01-01</datestamp></header>
        </record>""")
        self.assertTrue(ah.parse_oai_record(node)["deleted"])

    def test_fetch_enrichment_uses_authoritative_language_and_document_position(self):
        response = Mock()
        response.json.return_value = {
            "document_language": [{"type": "hi"}],
            "documents": [{
                "pos": 3, "main": "किताब एक.pdf", "security": "public",
                "files": [{
                    "filename": "किताब एक.pdf", "mime_type": "application/pdf",
                    "filesize": 10, "hash_type": "MD5", "hash": "abc",
                }],
            }],
        }
        response.raise_for_status.return_value = None
        with patch.object(ah, "session") as mocked:
            mocked.return_value.get.return_value = response
            result = ah.fetch_enrichment(2252)
        self.assertEqual(result["language"], "hin")
        self.assertEqual(result["documents"][0]["document_number"], 3)
        self.assertIn("/2252/3/", result["documents"][0]["source_url"])
        self.assertNotIn(" ", result["documents"][0]["source_url"])

    def test_inventory_resumes_with_saved_oai_token(self):
        page_one = ET.fromstring(f"""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <ListRecords>{OAI_RECORD}<resumptionToken>next-token</resumptionToken></ListRecords>
        </OAI-PMH>""")
        page_two = ET.fromstring("""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <ListRecords><resumptionToken /></ListRecords></OAI-PMH>""")
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            with patch.object(ah, "oai_request", return_value=(page_one, "2024-01-02T00:00:00Z")):
                ah.inventory(db_path, full=True, restart=False, max_pages=1, delay=0)
            db = ah.connect_db(db_path)
            self.assertEqual(ah.get_state(db, "inventory_token"), "next-token")
            db.close()
            with patch.object(ah, "oai_request", return_value=(page_two, "2024-01-03T00:00:00Z")) as mocked:
                ah.inventory(db_path, full=False, restart=False, max_pages=None, delay=0)
            self.assertEqual(mocked.call_args.args[0]["resumptionToken"], "next-token")
            db = ah.connect_db(db_path)
            self.assertEqual(ah.get_state(db, "inventory_token"), "")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            db.close()

    def test_reconcile_marks_exact_id_and_only_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            db = ah.connect_db(db_path)
            for record_id in (65, 66):
                db.execute(
                    """INSERT INTO items
                       (record_id,oai_identifier,datestamp,deleted,dc_json,metadata_json,
                        language,source_url,enriched_datestamp,listed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (record_id, f"oai:x:{record_id}", "2024", 0, "{}", "{}", "kan",
                     f"https://anuvadasampada.azimpremjiuniversity.edu.in/{record_id}/", "2024", ah.utc_now()),
                )
                db.execute(
                    """INSERT INTO documents
                       (record_id,document_number,filename,source_url,ia_identifier,classification,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (record_id, 1, "A.pdf", f"https://anuvadasampada.azimpremjiuniversity.edu.in/{record_id}/1/A.pdf",
                     f"apu.anuvadasampada.kan.{record_id}", "pending_reconcile", ah.utc_now()),
                )
            db.commit()
            db.close()
            existing = [{
                "identifier": "apu.anuvadasampada.kan.65",
                "source": "https://anuvadasampada.azimpremjiuniversity.edu.in/65/",
                "source-url": "https://anuvadasampada.azimpremjiuniversity.edu.in/65/1/A.pdf",
            }]
            with patch.object(ah, "fetch_existing_ia", return_value=existing):
                ah.reconcile_ia(db_path)
            db = ah.connect_db(db_path)
            classes = {row["record_id"]: row["classification"] for row in db.execute("SELECT * FROM documents")}
            self.assertEqual(classes, {65: "existing_ia", 66: "missing_ia"})
            db.close()

    def test_download_selection_skips_existing_ia(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            db = ah.connect_db(db_path)
            db.execute(
                """INSERT INTO items
                   (record_id,oai_identifier,datestamp,deleted,dc_json,metadata_json,
                    language,source_url,enriched_datestamp,listed_at)
                   VALUES(1,'oai:x:1','2024',0,'{}','{}','hin','https://source/1/','2024',?)""",
                (ah.utc_now(),),
            )
            for number, classification in ((1, "existing_ia"), (2, "missing_ia")):
                db.execute(
                    """INSERT INTO documents
                       (record_id,document_number,filename,source_url,ia_identifier,classification,updated_at)
                       VALUES(1,?,?,?,?,?,?)""",
                    (number, f"{number}.pdf", f"https://source/1/{number}/A.pdf",
                     f"apu.anuvadasampada.hin.1.{number}", classification, ah.utc_now()),
                )
            db.commit()
            db.close()
            with patch.object(ah, "download_one", return_value={"status": "planned"}) as mocked:
                ah.download(db_path, Path(directory), workers=1, limit=None, execute=False)
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(mocked.call_args.args[1]["document_number"], 2)

    def test_export_includes_record_without_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            output = Path(directory) / "records.jsonl"
            db = ah.connect_db(db_path)
            db.execute(
                """INSERT INTO items
                   (record_id,oai_identifier,datestamp,deleted,dc_json,source_url,listed_at)
                   VALUES(1,'oai:x:1','2024',0,'{"title":["Metadata only"]}','https://source/1/',?)""",
                (ah.utc_now(),),
            )
            db.commit()
            db.close()
            ah.export(db_path, output, None)
            record = json.loads(output.read_text())
            self.assertEqual(record["record_id"], 1)
            self.assertEqual(record["dc"]["title"], ["Metadata only"])
            self.assertIsNone(record["document_number"])

    def test_download_resumes_partial_file_with_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = ah.connect_db(root / "test.sqlite3")
            db.execute(
                """INSERT INTO items
                   (record_id,oai_identifier,datestamp,deleted,dc_json,source_url,listed_at)
                   VALUES(1,'oai:x:1','2024',0,'{}','https://source/1/',?)""",
                (ah.utc_now(),),
            )
            db.execute(
                """INSERT INTO documents
                   (record_id,document_number,filename,source_url,expected_size,source_md5,
                    ia_identifier,classification,updated_at)
                   VALUES(1,1,'A.pdf','https://source/1/1/A.pdf',6,?,
                          'apu.anuvadasampada.hin.1.1','missing_ia',?)""",
                (hashlib.md5(b"abcdef").hexdigest(), ah.utc_now()),
            )
            row = db.execute("SELECT * FROM documents").fetchone()
            destination = ah.download_path(root, row)
            destination.parent.mkdir(parents=True)
            destination.with_suffix(".pdf.part").write_bytes(b"abc")
            response = Mock(status_code=206)
            response.raise_for_status.return_value = None
            response.iter_content.return_value = [b"def"]
            with patch.object(ah, "session") as mocked:
                mocked.return_value.get.return_value = response
                result = ah.download_one(root, row, execute=True)
            self.assertEqual(mocked.return_value.get.call_args.kwargs["headers"], {"Range": "bytes=3-"})
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertEqual(result["status"], "downloaded")
            db.close()

    def test_ia_metadata_supports_both_default_collections(self):
        with tempfile.TemporaryDirectory() as directory:
            db = ah.connect_db(Path(directory) / "test.sqlite3")
            db.execute(
                """INSERT INTO items
                   (record_id,oai_identifier,datestamp,deleted,dc_json,metadata_json,
                    language,source_url,enriched_datestamp,listed_at)
                   VALUES(1,'oai:x:1','2024',0,'{}',?,'hin','https://source/1/','2024',?)""",
                (json.dumps({"title": "Title"}), ah.utc_now()),
            )
            db.execute(
                """INSERT INTO documents
                   (record_id,document_number,filename,source_url,ia_identifier,
                    classification,updated_at)
                   VALUES(1,1,'A.pdf','https://source/1/1/A.pdf',
                          'apu.anuvadasampada.hin.1.1','missing_ia',?)""",
                (ah.utc_now(),),
            )
            item = db.execute("SELECT * FROM items").fetchone()
            document = db.execute("SELECT * FROM documents").fetchone()
            metadata = ah.ia_metadata(
                item, document, ["AzimPremjiUniversity", "ServantsOfKnowledge"]
            )
            self.assertEqual(
                metadata["collection"],
                ["AzimPremjiUniversity", "ServantsOfKnowledge"],
            )
            flags = ah.metadata_arguments(metadata)
            self.assertIn("--metadata=collection:AzimPremjiUniversity", flags)
            self.assertIn("--metadata=collection:ServantsOfKnowledge", flags)
            db.close()


if __name__ == "__main__":
    unittest.main()
