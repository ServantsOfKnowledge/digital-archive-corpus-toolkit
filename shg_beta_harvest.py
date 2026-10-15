#!/usr/bin/env python3
"""Metadata harvest from Shodhganga's beta portal (betasg.inflibnet.ac.in).

The main portal serves an Anubis bot challenge to scripts; the beta portal is a
DSpace 9 instance whose anonymous discover API returns full item metadata
(100 items/request, ~20 s/page; pages parallelise well). Only metadata is
collected here - no PDFs are fetched.

  python3 shg_beta_harvest.py probe
  python3 shg_beta_harvest.py harvest --dir shg_beta_corpus --workers 6
  python3 shg_beta_harvest.py status  --dir shg_beta_corpus
  python3 shg_beta_harvest.py build   --dir shg_beta_corpus      # metadata.jsonl

Harvest writes one chunk per page under <dir>/pages/ (atomic writes; re-running
skips completed pages). `build` merges chunks into metadata.jsonl, deduplicated
by uuid. Touch <dir>/harvest.stop to halt a running harvest gracefully.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("shg_beta_harvest")

BASE = "https://betasg.inflibnet.ac.in/server/api"
DISCOVER = f"{BASE}/discover/search/objects"
PAGE_SIZE = 100          # the API clamps larger page sizes to 100
DEFAULT_DIR = "shg_beta_corpus"
STOP_FILE = "harvest.stop"
PAGES_DIR = "pages"
COUNTS_FILE = "counts.jsonl"
SOURCE_FILE = "source.json"
OUTPUT_FILE = "metadata.jsonl"
UA = "digital-archive-corpus-toolkit/shg-beta-harvest (research metadata collection)"

RETRY_STATUS = {429, 500, 502, 503, 504}
ITEM_FIELDS = ("uuid", "handle", "id", "name", "lastModified", "withdrawn",
               "type", "entityType", "metadata")

_local = threading.local()
_counts_lock = threading.Lock()


class ApiError(RuntimeError):
    """The discover API stayed unavailable after retries."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        _local.session = s
    return s


def api_get(url: str, params: dict = None, timeout: int = 120, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        try:
            r = _session().get(url, params=params, timeout=timeout)
            if r.status_code in RETRY_STATUS:
                wait_s = int(r.headers.get("Retry-After") or 0) or min(60, 2 ** (attempt + 2))
                last = f"HTTP {r.status_code}"
                log.warning("HTTP %d from %s; retry in %ds (%d/%d)",
                            r.status_code, url, wait_s, attempt + 1, retries)
                time.sleep(wait_s)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = str(e)
            time.sleep(min(60, 2 ** (attempt + 2)))
    raise ApiError(f"GET {url} failed after {retries} attempts: {last}")


def discover_page(page: int, size: int = PAGE_SIZE, timeout: int = 120) -> dict:
    return api_get(DISCOVER, params={"query": "", "size": size, "page": page},
                   timeout=timeout)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def search_result(doc: dict) -> dict:
    return doc["_embedded"]["searchResult"]


def page_total(doc: dict) -> int:
    return int(search_result(doc).get("page", {}).get("totalElements") or 0)


def page_objects(doc: dict) -> list:
    return search_result(doc).get("_embedded", {}).get("objects", [])


def extract_items(doc: dict) -> list:
    out = []
    for wrapper in page_objects(doc):
        it = (wrapper.get("_embedded") or {}).get("indexableObject")
        if it and str(it.get("type", "")).lower() == "item":
            out.append({k: it[k] for k in ITEM_FIELDS if k in it})
    return out


def first_value(metadata: dict, field: str, default: str = "") -> str:
    vals = metadata.get(field) or []
    if vals and isinstance(vals[0], dict):
        return vals[0].get("value") or default
    return default


def make_record(item: dict) -> dict:
    md = item.get("metadata", {}) or {}
    return {
        "handle": item.get("handle", ""),
        "uuid": item.get("uuid", ""),
        "title": first_value(md, "dc.title") or item.get("name", ""),
        "last_modified": item.get("lastModified", ""),
        "withdrawn": bool(item.get("withdrawn")),
        "metadata": md,
    }


# ---------------------------------------------------------------------------
# Chunk storage
# ---------------------------------------------------------------------------

def page_path(corpus: Path, page: int) -> Path:
    return corpus / PAGES_DIR / f"page-{page:05d}.json"


def write_page(corpus: Path, page: int, total: int, items: list):
    path = page_path(corpus, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"page": page, "total": total, "items": items}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_page(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def done_pages(corpus: Path) -> set:
    out = set()
    pages = corpus / PAGES_DIR
    if pages.exists():
        for p in pages.glob("page-*.json"):
            m = re.fullmatch(r"page-(\d+)\.json", p.name)
            if m:
                out.add(int(m.group(1)))
    return out


def record_count(corpus: Path, page: int, n: int):
    with _counts_lock:
        with open(corpus / COUNTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"page": page, "items": n}) + "\n")


def load_counts(corpus: Path) -> dict:
    latest = {}
    path = corpus / COUNTS_FILE
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    latest[int(row["page"])] = int(row["items"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    return latest


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_source(corpus: Path, total: int):
    path = corpus / SOURCE_FILE
    prev = _load_json(path) or {}
    data = {
        "base": BASE,
        "page_size": PAGE_SIZE,
        "total_first_seen": prev.get("total_first_seen") or total,
        "total_last_seen": total,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args) -> int:
    t0 = time.time()
    try:
        doc = discover_page(0, size=5, timeout=args.timeout)
    except ApiError as e:
        log.error("%s", e)
        return 1
    total = page_total(doc)
    print(f"API:         {DISCOVER}")
    print(f"Items:       {total:,}")
    print(f"Latency:     {time.time() - t0:.1f}s for one 5-item page")
    print(f"Pages:       {math.ceil(total / PAGE_SIZE):,} of {PAGE_SIZE} items")
    for wrapper in page_objects(doc)[:5]:
        it = (wrapper.get("_embedded") or {}).get("indexableObject") or {}
        md = it.get("metadata", {})
        print(f"  {it.get('handle', '?'):16s} {first_value(md, 'dc.title', '(no title)')[:70]}")
    items = extract_items(doc)
    if items:
        md = items[0].get("metadata", {})
        print(f"Metadata:    {len(md)} fields, abstract={'yes' if 'dc.description.abstract' in md else 'no'}")
    return 0


# ---------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------

def fetch_page(corpus: Path, page: int, timeout: int, stop: threading.Event) -> int:
    if stop.is_set():
        return 0
    doc = discover_page(page, timeout=timeout)
    items = extract_items(doc)
    write_page(corpus, page, page_total(doc), items)
    record_count(corpus, page, len(items))
    return len(items)


def cmd_harvest(args) -> int:
    corpus = Path(args.dir)
    corpus.mkdir(parents=True, exist_ok=True)
    stop_path = corpus / STOP_FILE
    stop_path.unlink(missing_ok=True)
    if args.workers > 12:
        log.warning("--workers %d is hard on the API; 4-8 is the tested range", args.workers)

    log.info("Probing %s ...", BASE)
    try:
        doc = discover_page(0, size=1, timeout=args.timeout)
    except ApiError as e:
        log.error("%s", e)
        return 1
    total = page_total(doc)
    if total <= 0:
        log.error("API reported no items")
        return 1
    pages = math.ceil(total / PAGE_SIZE)
    save_source(corpus, total)

    done = done_pages(corpus)
    todo = [p for p in range(pages) if p not in done]
    if args.limit_pages:
        todo = todo[:args.limit_pages]
    print(f"Items on source: {total:,}")
    print(f"Pages:           {pages:,} total | {len(done):,} already fetched | {len(todo):,} to fetch")
    if not todo:
        print("Nothing to do.")
        return 0

    stop = threading.Event()
    failures, items_total, fetched = [], 0, 0
    start = time.time()
    last_log = start
    interrupted = False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_page, corpus, p, args.timeout, stop): p for p in todo}
        pending = set(futures)
        try:
            while pending:
                finished, pending = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
                if stop_path.exists():
                    stop.set()
                for f in finished:
                    page = futures[f]
                    exc = f.exception()
                    if exc is not None:
                        failures.append(page)
                        log.warning("page %d failed: %s", page, str(exc)[:160])
                        if len(failures) >= args.max_failures:
                            log.error("%d failed pages: stopping", len(failures))
                            stop.set()
                    else:
                        fetched += 1
                        items_total += f.result()
                now = time.time()
                if now - last_log >= 30 or not pending:
                    last_log = now
                    rate = fetched / max(now - start, 1) * 60
                    remaining = len(todo) - fetched - len(failures)
                    eta = remaining / rate if rate else 0
                    log.info("pages: %d/%d fetched, %d failed | %.1f pages/min | ETA %.0f min",
                             fetched, len(todo), len(failures), rate, eta)
                if stop.is_set():
                    for f in pending:
                        f.cancel()
                    break
        except KeyboardInterrupt:
            interrupted = True
            log.warning("Interrupted: cancelling queued pages")
            for f in pending:
                f.cancel()

    print(f"Fetched {fetched:,} pages ({items_total:,} items) in {(time.time() - start) / 60:.1f} min")
    if failures:
        failures.sort()
        print(f"Failed pages: {len(failures)} (first: {failures[:10]}) - re-run 'harvest' to retry")
    if interrupted:
        return 2
    return 0 if not failures and fetched == len(todo) else 2


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    corpus = Path(args.dir)
    source = _load_json(corpus / SOURCE_FILE)
    if source is None:
        log.error("No %s in %s: run 'harvest' first", SOURCE_FILE, corpus)
        return 1
    done = done_pages(corpus)
    counts = load_counts(corpus)
    expected = math.ceil(source["total_last_seen"] / PAGE_SIZE)
    print(f"Source:          {source['base']}")
    print(f"Last seen total: {source['total_last_seen']:,} items (at {source['updated']})")
    print(f"Pages fetched:   {len(done):,} / {expected:,}")
    print(f"Items collected: {sum(counts.values()):,}")
    missing = sorted(set(range(expected)) - done)
    if missing:
        print(f"Missing pages:   {len(missing)} (first: {missing[:10]})")
    out = corpus / OUTPUT_FILE
    if out.exists():
        with open(out, encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip())
        print(f"Built output:    {out} ({n:,} records, "
              f"{out.stat().st_size / 1e9:.2f} GB, {datetime.fromtimestamp(out.stat().st_mtime):%Y-%m-%d %H:%M})")
    else:
        print(f"Built output:    not built yet (run 'build')")
    if not args.offline:
        try:
            live = page_total(discover_page(0, size=1, timeout=args.timeout))
            live_pages = math.ceil(live / PAGE_SIZE)
            print(f"Live total:      {live:,} items ({live_pages:,} pages; "
                  f"{max(live_pages - len(done), 0):,} not yet fetched)")
        except ApiError as e:
            log.warning("Live probe failed: %s", e)
    return 0


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def iter_records(corpus: Path):
    for page in sorted(done_pages(corpus)):
        data = read_page(page_path(corpus, page))
        for item in data.get("items", []):
            yield make_record(item)


def cmd_build(args) -> int:
    corpus = Path(args.dir)
    source = _load_json(corpus / SOURCE_FILE)
    if source is None:
        log.error("No %s in %s: run 'harvest' first", SOURCE_FILE, corpus)
        return 1
    expected = math.ceil(source["total_last_seen"] / PAGE_SIZE)
    done = done_pages(corpus)
    missing = sorted(set(range(expected)) - done)
    if missing and not args.partial:
        log.error("%d of %d pages missing (first: %s). Finish 'harvest' or pass --partial.",
                  len(missing), expected, missing[:10])
        return 1

    out = corpus / OUTPUT_FILE
    tmp = out.with_name(f"{out.name}.tmp.{os.getpid()}")
    seen = set()
    written = dupes = 0
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in iter_records(corpus):
                uid = rec["uuid"] or rec["handle"]
                if uid in seen:
                    dupes += 1
                    continue
                seen.add(uid)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
                if written % 100_000 == 0:
                    log.info("wrote %,d records", written)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    size = out.stat().st_size
    print(f"Wrote {written:,} records ({dupes:,} duplicates dropped) to {out} ({size / 1e9:.2f} GB)")
    if missing:
        print(f"Note: built from {len(done):,} of {expected:,} pages (--partial)")
    return 0


# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--dir", default=DEFAULT_DIR,
                        help=f"Corpus directory (default: {DEFAULT_DIR})")
        sp.add_argument("--timeout", type=int, default=120,
                        help="Per-request timeout seconds (default 120; pages take ~20s)")

    sp = sub.add_parser("probe", help="Check the API, total item count and a sample")
    common(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("harvest", help="Fetch all item metadata pages (resumable)")
    common(sp)
    sp.add_argument("--workers", type=int, default=6,
                    help="Concurrent pages (default 6; tested up to 12)")
    sp.add_argument("--limit-pages", type=int, help="Fetch at most N pages (trial runs)")
    sp.add_argument("--max-failures", type=int, default=25,
                    help="Stop after this many failed pages (default 25)")
    sp.set_defaults(func=cmd_harvest)

    sp = sub.add_parser("status", help="Show pages fetched, item counts and live total")
    common(sp)
    sp.add_argument("--offline", action="store_true", help="Skip the live API probe")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("build", help="Merge page chunks into metadata.jsonl (dedup by uuid)")
    common(sp)
    sp.add_argument("--partial", action="store_true",
                    help="Build even when some pages are missing")
    sp.set_defaults(func=cmd_build)
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
