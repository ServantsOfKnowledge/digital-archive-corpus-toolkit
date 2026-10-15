import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import shg_beta_harvest as hb


def make_args(**kw):
    base = dict(dir=".", workers=2, limit_pages=None, max_failures=5, timeout=5,
                offline=True, partial=False)
    base.update(kw)
    return Namespace(**base)


def item(uuid, handle="10603/1", title="Thesis Title", **kw):
    base = {
        "uuid": uuid, "handle": handle, "name": title, "type": "item",
        "lastModified": "2026-01-01T00:00:00Z", "withdrawn": False,
        "metadata": {"dc.title": [{"value": title, "language": None}]},
    }
    base.update(kw)
    return base


def make_doc(items, total, page=0):
    return {"_embedded": {"searchResult": {
        "page": {"totalElements": total, "number": page},
        "_embedded": {"objects": [{"_embedded": {"indexableObject": it}} for it in items]},
    }}}


class ParsingTests(unittest.TestCase):
    def test_page_total_and_objects(self):
        doc = make_doc([item("a")], total=676009, page=3)
        self.assertEqual(hb.page_total(doc), 676009)
        self.assertEqual(len(hb.page_objects(doc)), 1)

    def test_extract_items_filters_non_items(self):
        docs = make_doc([item("a"), {"uuid": "c1", "type": "Collection"}], total=2)
        items = hb.extract_items(docs)
        self.assertEqual([i["uuid"] for i in items], ["a"])
        self.assertNotIn("_links", items[0])

    def test_make_record_title_and_fallback(self):
        rec = hb.make_record(item("a", title="From DC"))
        self.assertEqual(rec["title"], "From DC")
        self.assertEqual(rec["handle"], "10603/1")
        bare = {"uuid": "b", "name": "From Name", "metadata": {}}
        self.assertEqual(hb.make_record(bare)["title"], "From Name")

    def test_api_get_retries_on_500_then_succeeds(self):
        bad = MagicMock(status_code=500, headers={})
        ok = MagicMock(status_code=200, headers={})
        ok.json.return_value = {"ok": 1}
        sess = MagicMock()
        sess.get.side_effect = [bad, ok]
        with patch.object(hb, "_session", return_value=sess), \
             patch.object(hb.time, "sleep"):
            self.assertEqual(hb.api_get("http://x"), {"ok": 1})
        self.assertEqual(sess.get.call_count, 2)

    def test_api_get_raises_after_retries(self):
        bad = MagicMock(status_code=500, headers={})
        sess = MagicMock()
        sess.get.return_value = bad
        with patch.object(hb, "_session", return_value=sess), \
             patch.object(hb.time, "sleep"):
            with self.assertRaises(hb.ApiError):
                hb.api_get("http://x", retries=2)
        self.assertEqual(sess.get.call_count, 2)


class StorageTests(unittest.TestCase):
    def test_write_page_roundtrip_and_done_pages(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            hb.write_page(d, 0, 250, [item("a")])
            hb.write_page(d, 2, 250, [])
            (d / hb.PAGES_DIR / "page-00001.json.tmp.99").write_text("{")
            self.assertEqual(hb.done_pages(d), {0, 2})
            data = hb.read_page(hb.page_path(d, 0))
            self.assertEqual(data["total"], 250)
            self.assertEqual(data["items"][0]["uuid"], "a")

    def test_load_counts_keeps_latest_per_page(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            hb.record_count(d, 1, 100)
            hb.record_count(d, 1, 97)
            hb.record_count(d, 2, 100)
            counts = hb.load_counts(d)
            self.assertEqual(counts, {1: 97, 2: 100})


class HarvestTests(unittest.TestCase):
    def fake_discover(self, total, per_page=100, calls=None, fail_pages=()):
        def fake(page, size=100, timeout=120):
            if calls is not None:
                calls.append((page, size))
            # the initial probe uses size=1; only real page fetches can fail
            if size > 1 and page in fail_pages:
                raise hb.ApiError("boom")
            n = min(per_page, max(total - page * per_page, 0))
            items = [item(f"u-{page}-{i}", handle=f"10603/{page * 100 + i}")
                     for i in range(n)]
            return make_doc(items, total, page)
        return fake

    @staticmethod
    def fetched(calls):
        return sorted(p for p, size in calls if size > 1)

    def test_harvest_skips_completed_pages(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            hb.write_page(d, 0, 250, [item("u-0-0")])
            hb.record_count(d, 0, 1)
            calls = []
            fake = self.fake_discover(250, calls=calls)
            with patch.object(hb, "discover_page", side_effect=fake):
                rc = hb.cmd_harvest(make_args(dir=t))
            self.assertEqual(rc, 0)
            self.assertEqual(self.fetched(calls), [1, 2])  # page 0 not refetched
            self.assertEqual(hb.done_pages(d), {0, 1, 2})
            self.assertEqual(sum(hb.load_counts(d).values()), 100 + 50 + 1)

    def test_harvest_limit_pages(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            calls = []
            fake = self.fake_discover(1000, calls=calls)
            with patch.object(hb, "discover_page", side_effect=fake):
                rc = hb.cmd_harvest(make_args(dir=t, limit_pages=2))
            self.assertEqual(rc, 0)
            self.assertEqual(self.fetched(calls), [0, 1])
            self.assertEqual(hb.done_pages(d), {0, 1})

    def test_harvest_stops_after_max_failures(self):
        with tempfile.TemporaryDirectory() as t:
            fake = self.fake_discover(1000, fail_pages=range(100))
            with patch.object(hb, "discover_page", side_effect=fake):
                rc = hb.cmd_harvest(make_args(dir=t, max_failures=1))
            self.assertEqual(rc, 2)

    def test_harvest_clears_stale_stop_file(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / hb.STOP_FILE).write_text("")
            fake = self.fake_discover(100)
            with patch.object(hb, "discover_page", side_effect=fake):
                rc = hb.cmd_harvest(make_args(dir=t))
            self.assertEqual(rc, 0)
            self.assertFalse((d / hb.STOP_FILE).exists())


class BuildAndStatusTests(unittest.TestCase):
    def seed(self, d: Path, total: int, pages: dict):
        hb.save_source(d, total)
        for page, items in pages.items():
            hb.write_page(d, page, total, items)
            hb.record_count(d, page, len(items))

    def test_build_dedupes_by_uuid(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.seed(d, 200, {
                0: [item("a", handle="10603/1", title="First"),
                    item("b", handle="10603/2")],
                1: [item("a", handle="10603/1", title="Repeat"),
                    item("c", handle="10603/3")],
            })
            with redirect_stdout(io.StringIO()):
                rc = hb.cmd_build(make_args(dir=t))
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in (d / hb.OUTPUT_FILE).read_text().splitlines()]
            self.assertEqual([r["uuid"] for r in rows], ["a", "b", "c"])
            self.assertEqual(rows[0]["title"], "First")  # first occurrence wins

    def test_build_refuses_missing_pages_unless_partial(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.seed(d, 300, {0: [item("a")]})
            self.assertEqual(hb.cmd_build(make_args(dir=t)), 1)
            self.assertEqual(hb.cmd_build(make_args(dir=t, partial=True)), 0)
            self.assertTrue((d / hb.OUTPUT_FILE).exists())

    def test_build_without_source_fails(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(hb.cmd_build(make_args(dir=t)), 1)

    def test_status_offline_reports_counts(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.seed(d, 250, {0: [item(f"u{i}") for i in range(3)],
                               1: [item("u3")]})
            hb.save_source(d, 250)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = hb.cmd_status(make_args(dir=t))
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("Items collected: 4", text)
            self.assertIn("Missing pages:", text)  # page 2 never fetched


if __name__ == "__main__":
    unittest.main()
