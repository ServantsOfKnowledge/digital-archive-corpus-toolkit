#!/usr/bin/env python3
"""
Audit and repair Palmleaf (சுவடி) items on Internet Archive.

The original harvest treated the first gallery image as the item's "PDF", so
IA items only contain `..._001.jpg`. This script re-reads each TDL article page,
lists every gallery image, compares with the files on the IA item, and (with
--fix) downloads and uploads the missing ones into the existing item.

Usage:
  python tdl_palmleaf_fix.py --fetch-list --report palmleaf_report.json   # audit all (category 22)
  python tdl_palmleaf_fix.py --input tdl_corpus/listing_Palmleaf.json      # audit from listing
  python tdl_palmleaf_fix.py --url "https://tamildigitallibrary.in/Articles/..." --fix
  python tdl_palmleaf_fix.py --input listing.json --fix --workers 3        # upload missing images

Default is audit-only; nothing is uploaded without --fix. Items not yet on IA
are reported as `not_on_ia` and left to the normal harvest (which now fetches
the whole gallery).
"""

import argparse
import json
import logging
import shutil
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import tdl_downloader as dl
import tdl_upload as up

log = logging.getLogger("palmleaf_fix")
PROGRESS_FILE = Path(__file__).parent / "palmleaf_fix_progress.json"
_lock = threading.Lock()
_ia = requests.Session()


def ia_name(name: str) -> str:
    """File name as tdl_upload would send it (230-byte limit), NFC-normalised."""
    p = up.Path(name)
    raw = p.name.encode("utf-8")
    if len(raw) > 230:
        stem, ext = p.stem, p.suffix
        limit = 230 - len(ext.encode("utf-8")) - 1
        while len(stem.encode("utf-8")) > limit:
            stem = stem[:-1]
        name = stem + ext
    return unicodedata.normalize("NFC", name)


def identifier_for(data: dict) -> str:
    folder_name = f"{data['article_id']}_{dl.safe_filename(data['title'], 80)}"
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / folder_name
        folder.mkdir()
        meta = dl.transform_to_ia_metadata(data)
        (folder / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return up.make_identifier("Palmleaf", folder)


def ia_files(identifier: str):
    """Set of NFC file names on the IA item, or None if the item does not exist."""
    for attempt in range(3):
        try:
            r = _ia.get(f"https://archive.org/metadata/{identifier}", timeout=60)
            r.raise_for_status()
            d = r.json()
            if not d or "files" not in d:
                return None
            return {unicodedata.normalize("NFC", f["name"]) for f in d["files"]}
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
    return None


def load_progress() -> set:
    try:
        return set(json.loads(PROGRESS_FILE.read_text()).get("fixed", []))
    except (OSError, ValueError):
        return set()


def mark_fixed(identifier: str, done: set):
    with _lock:
        done.add(identifier)
        PROGRESS_FILE.write_text(json.dumps({"fixed": sorted(done)}, indent=2))


def process(url: str, fix: bool, done: set) -> dict:
    res = {"url": url, "status": "error"}
    try:
        data = dl.extract_article_data(url)
        images = data.get("image_urls") or []
        ident = identifier_for(data)
        res.update(identifier=ident, title=data["title"], expected=len(images))
        if not images:
            res["status"] = "no_images_on_page"
            return res
        if ident in done:
            res["status"] = "already_fixed"
            return res
        have = ia_files(ident)
        if have is None:
            res["status"] = "not_on_ia"
            return res
        missing = [u for u in images if ia_name(dl.gallery_filename(u)) not in have]
        res["missing"] = len(missing)
        if not missing:
            res["status"] = "complete"
            mark_fixed(ident, done)
            return res
        res["status"] = "incomplete"
        if not fix:
            return res

        tmp = Path(tempfile.mkdtemp(prefix="palmleaf_"))
        try:
            files = []
            for u in missing:
                dest = tmp / dl.gallery_filename(u)
                if not dl.download_file(u, dest, desc=dest.name[:50]) or dest.stat().st_size == 0:
                    res["status"] = "download_failed"
                    res["failed_url"] = u
                    return res
                files.append(up.shorten_filename(dest))
            r = up.run_with_retry(["ia", "upload", ident] + [str(f) for f in files])
            if r.returncode != 0:
                res["status"] = "upload_failed"
                res["error"] = (r.stderr.strip() or r.stdout.strip())[-500:]
                return res
            res["status"] = "fixed"
            res["uploaded"] = len(files)
            mark_fixed(ident, done)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:  # keep batch going
        res["error"] = str(e)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="listing JSON ({'articles': [...]}) or text file of article URLs")
    ap.add_argument("--fetch-list", action="store_true", help="list all Palmleaf (cat 22) articles from TDL")
    ap.add_argument("--url", action="append", default=[], help="single article URL (repeatable)")
    ap.add_argument("--fix", action="store_true", help="download and upload missing images (default: audit only)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, help="only process the first N URLs")
    ap.add_argument("--report", default="palmleaf_report.json")
    ap.add_argument("--reset", action="store_true", help="ignore palmleaf_fix_progress.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    urls = list(args.url)
    if args.input:
        urls += dl.load_article_urls(args.input)
    if args.fetch_list:
        urls += dl.list_articles(22)
    urls = list(dict.fromkeys(urls))
    if args.limit:
        urls = urls[: args.limit]
    if not urls:
        ap.error("no article URLs: use --input, --fetch-list or --url")

    done = set() if args.reset else load_progress()
    log.info("%d articles, mode=%s, workers=%d", len(urls), "FIX" if args.fix else "audit", args.workers)

    results, counts = [], {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(process, u, args.fix, done) for u in urls]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["status"] not in ("complete", "already_fixed"):
                log.info("[%d/%d] %s %s expected=%s missing=%s %s", i, len(urls), r["status"],
                         r.get("identifier", "?"), r.get("expected"), r.get("missing"), r.get("error", ""))
            Path(args.report).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("Summary: %s", json.dumps(counts))
    log.info("Report: %s", args.report)


if __name__ == "__main__":
    main()
