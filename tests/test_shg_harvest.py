import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import shg_harvest as sh


def make_args(**kw):
    base = dict(
        work_dir=None, collection="shodhganga", prefix="shg.", upload_slots=1,
        ia_retries=1, max_consecutive_failures=3, min_free_gb=0, keep_files=False,
        remove_placeholder=False, only=None, skip_failed=False, max_item_gb=5.0,
        limit=None,
    )
    base.update(kw)
    return Namespace(**base)


class HelperTests(unittest.TestCase):
    def test_clean_description_removes_newline_markers(self):
        raw = "consists\nnewlineof numerous mobile Sensor Nodes\nnewline(AoI). Done"
        out = sh.clean_description(raw)
        self.assertNotIn("newline", out)
        self.assertIn("consists of numerous", out)
        self.assertIn("(AoI). Done", out)

    def test_clean_description_keeps_real_words(self):
        self.assertEqual(sh.clean_description("the trailing newline character"),
                         "the trailing newline character")

    def test_parse_ia_record(self):
        hs, info = sh.parse_ia_record({
            "identifier": "shg.700200-some-title", "item_size": 34000,
            "format": ["Metadata", "Text"], "collection": ["shodhganga"],
        })
        self.assertEqual(hs, "700200")
        self.assertFalse(info["has_pdf"])
        _, info = sh.parse_ia_record({"identifier": "shg.1-x", "format": ["Text PDF"]})
        self.assertTrue(info["has_pdf"])
        self.assertIsNone(sh.parse_ia_record({"identifier": "shg.-vl.-s-02-e-01.720-hc"}))
        self.assertIsNone(sh.parse_ia_record({"identifier": "other-item"}))

    def test_valid_handle_suffix(self):
        self.assertTrue(sh.valid_handle_suffix("719131"))
        self.assertFalse(sh.valid_handle_suffix("shg_ae83c716"))
        self.assertFalse(sh.valid_handle_suffix(""))

    def test_size_bucket(self):
        self.assertEqual(sh.size_bucket(0), "0")
        self.assertEqual(sh.size_bucket(5_000_000), "<10MB")
        self.assertEqual(sh.size_bucket(2_000_000_000), ">=1GB")


class PlanAndSelectTests(unittest.TestCase):
    def write_manifest(self, d):
        f = lambda n: [{"url": f"u{n}", "filename": "a.pdf", "size_bytes": n}]
        items = [
            {"handle_suffix": "10", "item_url": "x/10", "title": "Big", "pdf_files": f(900)},
            {"handle_suffix": "11", "item_url": "x/11", "title": "Small", "pdf_files": f(100)},
            {"handle_suffix": "11", "item_url": "x/11", "title": "Dup", "pdf_files": f(100)},
            {"handle_suffix": "12", "item_url": "x/12", "title": "None", "pdf_files": []},
            {"handle_suffix": "shg_abc", "item_url": "x/null", "title": "", "pdf_files": []},
            {"handle_suffix": "13", "item_url": "x/13", "title": "Zero", "pdf_files": f(0)},
        ]
        (d / "manifest.json").write_text(json.dumps({"items": items}))

    def test_plan_builds_sorted_queue_and_skips_bad_entries(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.write_manifest(d)
            self.assertEqual(sh.cmd_plan(Namespace(dir=t)), 0)
            queue = sh.load_jsonl(d / sh.QUEUE_FILE)
            self.assertEqual([r["hs"] for r in queue], ["11", "10"])  # small first

    def test_select_queue_skips_done_ia_and_oversize(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            rows = [
                {"hs": "1", "bytes": 10, "files": 1, "title": "a", "url": "u"},
                {"hs": "2", "bytes": 10, "files": 1, "title": "b", "url": "u"},
                {"hs": "3", "bytes": 10, "files": 1, "title": "c", "url": "u"},
                {"hs": "4", "bytes": 10 * sh.GB, "files": 1, "title": "d", "url": "u"},
                {"hs": "5", "bytes": 10, "files": 1, "title": "e", "url": "u"},
            ]
            (d / sh.QUEUE_FILE).write_text("\n".join(json.dumps(r) for r in rows))
            results = {"1": {"hs": "1", "status": "uploaded"},
                       "5": {"hs": "5", "status": "download_failed"}}
            ia = {"2": {"identifier": "shg.2-b", "has_pdf": True},
                  "3": {"identifier": "shg.3-c", "has_pdf": False}}
            got = [r["hs"] for r in sh.select_queue(d, make_args(), results, ia)]
            self.assertEqual(got, ["3", "5"])  # failed retried, placeholder still pending
            got = [r["hs"] for r in sh.select_queue(d, make_args(skip_failed=True), results, ia)]
            self.assertEqual(got, ["3"])


class ProcessTests(unittest.TestCase):
    META = {"title": "T", "pdf_files": [{"url": "http://f/1", "filename": "a.pdf", "size_bytes": 8}],
            "researcher": "R", "handle": "10603/7", "abstract": "x\nnewlineY"}

    def harvest(self, d, ia_state=None):
        with patch.object(sh, "load_ia_state", return_value=ia_state or {}):
            h = sh.Harvest(make_args(work_dir=str(Path(d) / "work")), Path(d))
        h.work.mkdir(parents=True, exist_ok=True)
        return h

    def row(self):
        return {"hs": "7", "url": "http://x/7", "title": "T", "bytes": 8, "files": 1}

    @staticmethod
    def fake_download(url, dest, desc=""):
        dest.write_bytes(b"%PDF-1.4 data")
        return True

    def test_success_uploads_to_existing_placeholder_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t, {"7": {"identifier": "shg.7-t", "has_pdf": False}})
            with patch.object(sh.sg, "extract_metadata", return_value=dict(self.META)), \
                 patch.object(sh.sg, "download_file", side_effect=self.fake_download), \
                 patch.object(sh.Harvest, "upload", return_value=True) as up:
                h.process(self.row())
            res = sh.load_results(Path(t))["7"]
            self.assertEqual(res["status"], "uploaded")
            self.assertEqual(res["identifier"], "shg.7-t")
            self.assertTrue(up.call_args.kwargs["existing"])
            self.assertNotIn("newline", up.call_args.args[2]["description"])
            self.assertFalse((Path(t) / "work" / "7").exists())

    def test_on_ia_with_pdf_is_skipped_without_network(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t, {"7": {"identifier": "shg.7-t", "has_pdf": True}})
            with patch.object(sh.sg, "extract_metadata") as em:
                h.process(self.row())
            em.assert_not_called()
            self.assertEqual(sh.load_results(Path(t))["7"]["status"], "on_ia")

    def test_html_instead_of_pdf_is_a_download_failure_and_not_uploaded(self):
        def fake_html(url, dest, desc=""):
            dest.write_bytes(b"<html>consent</html>")
            return True
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t)
            with patch.object(sh.sg, "extract_metadata", return_value=dict(self.META)), \
                 patch.object(sh.sg, "download_file", side_effect=fake_html), \
                 patch.object(sh.Harvest, "upload") as up:
                h.process(self.row())
            up.assert_not_called()
            self.assertEqual(sh.load_results(Path(t))["7"]["status"], "download_failed")

    def test_merge_failure_does_not_upload_partial(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t)
            with patch.object(sh.sg, "extract_metadata", return_value=dict(self.META)), \
                 patch.object(sh.sg, "download_file", side_effect=self.fake_download), \
                 patch.object(sh.sg, "merge_pdfs", return_value=False), \
                 patch.object(sh.Harvest, "upload") as up:
                h.process(self.row())
            up.assert_not_called()
            self.assertEqual(sh.load_results(Path(t))["7"]["status"], "merge_failed")

    def test_consecutive_failures_trip_the_breaker(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t)
            for i in range(3):
                h.record(str(i), "download_failed")
            self.assertTrue(h.stop.is_set())

    def test_bot_challenge_is_blocked_not_no_pdf(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t)
            with patch.object(sh.sg, "extract_metadata", side_effect=sh.sg.BotChallengeError("x")):
                h.process(self.row())
            self.assertEqual(sh.load_results(Path(t))["7"]["status"], "blocked")
            self.assertTrue(h.stop.is_set())

    def test_stop_file_halts_processing(self):
        with tempfile.TemporaryDirectory() as t:
            h = self.harvest(t)
            (Path(t) / sh.STOP_FILE).write_text("")
            with patch.object(sh.sg, "extract_metadata") as em:
                h.process(self.row())
            em.assert_not_called()


if __name__ == "__main__":
    unittest.main()
