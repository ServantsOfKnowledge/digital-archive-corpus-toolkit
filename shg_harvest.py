#!/usr/bin/env python3
"""Automated Shodhganga -> Internet Archive harvest (plan, reconcile, run, status).

Works on the manifest built by ``shg_downloader.py manifest`` and processes each
thesis end to end: fetch metadata -> download parts -> merge -> upload -> delete.

Nothing is downloaded or uploaded unless ``run`` is given ``--execute``.

  python3 shg_harvest.py preflight --dir shg_corpus
  python3 shg_harvest.py plan --dir shg_corpus            # review manifest, build queue
  python3 shg_harvest.py reconcile-ia --dir shg_corpus    # what is already on IA
  python3 shg_harvest.py run --dir shg_corpus --workers 4 --limit 20          # preview
  python3 shg_harvest.py run --dir shg_corpus --workers 4 --limit 20 --execute
  python3 shg_harvest.py status --dir shg_corpus
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import shutil
import socket
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from pathlib import Path

import requests

import shg_downloader as sg

log = logging.getLogger("shg_harvest")

IA_SCRAPE_URL = "https://archive.org/services/search/v1/scrape"
IA_ID_RE = re.compile(r"^shg\.(\d+)-")
GB = 1024 ** 3

QUEUE_FILE = "harvest_queue.jsonl"
IA_STATE_FILE = "ia_state.json"
RESULTS_FILE = "harvest_results.jsonl"
STOP_FILE = "harvest.stop"

# Statuses that never need another attempt.
TERMINAL = {"uploaded", "on_ia", "no_pdf"}

IA_META_KEYS = {
    "title", "description", "subject", "creator", "publisher", "contributor",
    "date", "language", "type", "source", "collection", "mediatype",
    "licenseurl", "license", "original_url",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_description(text: str) -> str:
    """Shodhganga abstracts carry literal ``newline`` markers where line breaks were."""
    if not text:
        return text
    text = re.sub(r"\s*\n\s*newline", " ", text)
    text = re.sub(r"^newline", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def valid_handle_suffix(hs: str) -> bool:
    return bool(hs) and hs.isdigit()


def load_jsonl(path: Path) -> list:
    rows = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # torn last line after a crash
    return rows


def load_results(corpus: Path) -> dict:
    """Latest result per handle suffix."""
    latest = {}
    for row in load_jsonl(corpus / RESULTS_FILE):
        latest[row["hs"]] = row
    return latest


def load_ia_state(corpus: Path) -> dict:
    path = corpus / IA_STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text()).get("items", {})
        except (json.JSONDecodeError, OSError):
            log.warning("Unreadable %s; treating IA state as empty", path)
    return {}


def parse_ia_record(rec: dict):
    """Map an IA scrape record to (handle_suffix, info) or None."""
    m = IA_ID_RE.match(rec.get("identifier", ""))
    if not m:
        return None
    formats = rec.get("format") or []
    if isinstance(formats, str):
        formats = [formats]
    size = int(rec.get("item_size") or 0)
    has_pdf = any("pdf" in f.lower() for f in formats)
    return m.group(1), {
        "identifier": rec["identifier"],
        "size": size,
        "has_pdf": has_pdf,
        "collection": rec.get("collection", []),
    }


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def check_port(host: str, port: int, timeout: float = 10.0, attempts: int = 1) -> bool:
    for _ in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _challenge_check():
    try:
        sg.fetch(f"{sg.BASE_URL}/handle/10603/719131", timeout=20)
        return True, "item page served normally"
    except sg.BotChallengeError:
        return False, "site serves an Anubis bot challenge; automated access is blocked"
    except Exception as e:
        return False, f"item page not fetchable: {str(e)[:80]}"


def preflight(corpus: Path, need_upload: bool = True) -> dict:
    """Return {check: (ok, detail)}. PDF downloads need the 8443 proxy."""
    host = "shodhganga.inflibnet.ac.in"
    out = {}
    out["site_443"] = (check_port(host, 443, attempts=3), f"{host}:443 (metadata pages)")
    out["no_bot_challenge"] = _challenge_check()
    out["pdf_proxy_8443"] = (
        check_port(host, 8443),
        f"{host}:8443 (PDF proxy; firewalled outside INFLIBNET/India routes)",
    )
    has_merge = False
    try:
        import pypdf  # noqa: F401
        has_merge = True
    except ImportError:
        has_merge = shutil.which("pdfunite") is not None
    out["pdf_merge"] = (has_merge, "pypdf or pdfunite")
    if need_upload:
        try:
            import internetarchive  # noqa: F401
            from internetarchive.config import get_config
            s3 = get_config().get("s3", {})
            has_keys = bool(s3.get("access") and s3.get("secret"))
            out["ia_credentials"] = (has_keys, "internetarchive package + ia configure")
        except ImportError:
            out["ia_credentials"] = (False, "internetarchive package not installed")
    free = shutil.disk_usage(corpus if corpus.exists() else Path(".")).free
    out["disk_free"] = (free > 50 * GB, f"{free / GB:.0f} GB free")
    return out


def cmd_preflight(args):
    corpus = Path(args.dir)
    res = preflight(corpus)
    for name, (ok, detail) in res.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name:16s} {detail}")
    return 0 if all(ok for ok, _ in res.values()) else 1


# ---------------------------------------------------------------------------
# plan: review the manifest and build a compact queue
# ---------------------------------------------------------------------------

def size_bucket(n: int) -> str:
    for limit, label in ((1, "0"), (10e6, "<10MB"), (50e6, "<50MB"),
                         (200e6, "<200MB"), (1e9, "<1GB")):
        if n < limit:
            return label
    return ">=1GB"


def cmd_plan(args):
    corpus = Path(args.dir)
    manifest_path = corpus / "manifest.json"
    if not manifest_path.exists():
        log.error("Manifest not found: %s (run shg_downloader.py manifest)", manifest_path)
        return 1
    log.info("Loading manifest (large file, this takes a while)...")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items", [])

    seen = set()
    queue, skipped = [], collections.Counter()
    buckets = collections.Counter()
    for it in items:
        hs = it.get("handle_suffix", "")
        if not valid_handle_suffix(hs):
            skipped["invalid_handle"] += 1
            continue
        if hs in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(hs)
        files = it.get("pdf_files") or []
        size = sum(f.get("size_bytes", 0) for f in files)
        buckets[size_bucket(size)] += 1
        if not files:
            skipped["no_pdf_files"] += 1
            continue
        if size == 0:
            skipped["zero_size"] += 1
            continue
        queue.append({
            "hs": hs, "url": it.get("item_url", ""), "title": it.get("title", ""),
            "bytes": size, "files": len(files),
        })

    # Small first; sub-100 KB "PDFs" are usually stubs, so try them last.
    queue.sort(key=lambda r: (r["bytes"] < 100_000, r["bytes"], int(r["hs"])))
    tmp = corpus / (QUEUE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in queue:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(corpus / QUEUE_FILE)

    sizes = [r["bytes"] for r in queue]
    print(f"Manifest items:        {len(items):,}")
    print(f"Queued:                {len(queue):,}  -> {corpus / QUEUE_FILE}")
    print(f"Skipped:               {dict(skipped) or 0}")
    if sizes:
        print(f"Total size queued:     {sum(sizes) / 1e12:.2f} TB")
        print(f"Median / max item:     {statistics.median(sizes) / 1e6:.1f} MB / {max(sizes) / GB:.2f} GB")
    print(f"Size distribution:     {dict(sorted(buckets.items()))}")
    print(f"Part-count (files):    median {statistics.median(r['files'] for r in queue):.0f}"
          if queue else "")
    return 0


# ---------------------------------------------------------------------------
# reconcile-ia: what shg.* items already exist on archive.org
# ---------------------------------------------------------------------------

def cmd_reconcile_ia(args):
    corpus = Path(args.dir)
    corpus.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = sg.HEADERS["User-Agent"]
    params = {
        "q": "identifier:shg.*",
        "fields": "identifier,item_size,collection,format",
        "count": 10000,
    }
    found, skipped, total = {}, 0, None
    while True:
        for attempt in range(4):
            try:
                r = session.get(IA_SCRAPE_URL, params=params, timeout=120)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                log.warning("IA scrape retry %d: %s", attempt + 1, e)
                time.sleep(5 * (attempt + 1))
        else:
            log.error("IA scrape failed; not writing a partial snapshot")
            return 1
        total = total or data.get("total")
        for rec in data.get("items", []):
            parsed = parse_ia_record(rec)
            if parsed is None:
                skipped += 1
                continue
            hs, info = parsed
            prev = found.get(hs)
            # Several identifiers for one handle: prefer the one that has a PDF.
            if prev is None or (info["has_pdf"] and not prev["has_pdf"]):
                found[hs] = info
        cursor = data.get("cursor")
        log.info("IA records so far: %d / %s", len(found), total)
        if not cursor:
            break
        params["cursor"] = cursor

    out = {"generated": now_iso(), "total_scraped": total, "items": found}
    tmp = corpus / (IA_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(out), encoding="utf-8")
    tmp.replace(corpus / IA_STATE_FILE)
    with_pdf = sum(1 for v in found.values() if v["has_pdf"])
    print(f"shg.<handle> items on IA: {len(found):,}  (with PDF: {with_pdf:,}, "
          f"metadata-only: {len(found) - with_pdf:,}); ignored non-matching: {skipped}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

class Harvest:
    def __init__(self, args, corpus: Path):
        self.args = args
        self.corpus = corpus
        self.work = Path(args.work_dir) if args.work_dir else corpus / "work"
        self.ia_state = load_ia_state(corpus)
        self.results_lock = threading.Lock()
        self.disk_lock = threading.Lock()
        self.reserved = 0
        self.stop = threading.Event()
        self.consecutive_failures = 0
        self.upload_sem = threading.BoundedSemaphore(args.upload_slots)
        self.counts = collections.Counter()
        self.collection = args.collection
        self.prefix = args.prefix

    # -- state -------------------------------------------------------------
    def record(self, hs: str, status: str, **extra):
        row = {"hs": hs, "status": status, "time": now_iso(), **extra}
        with self.results_lock:
            with open(self.corpus / RESULTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.counts[status] += 1
            if status in ("uploaded", "on_ia", "no_pdf"):
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.args.max_consecutive_failures:
                    log.error("%d consecutive failures: stopping (check network/proxy/IA)",
                              self.consecutive_failures)
                    self.stop.set()

    # -- disk guard --------------------------------------------------------
    def reserve_disk(self, nbytes: int) -> bool:
        need = int(nbytes * 2.2)  # parts + merged copy
        floor = int(self.args.min_free_gb * GB)
        while not self.stop.is_set():
            with self.disk_lock:
                free = shutil.disk_usage(self.work).free
                if free - self.reserved - need >= floor:
                    self.reserved += need
                    return True
            log.info("Waiting for disk space (need %.1f GB, floor %d GB)", need / GB,
                     self.args.min_free_gb)
            self.stop.wait(30)
        return False

    def release_disk(self, nbytes: int):
        with self.disk_lock:
            self.reserved = max(0, self.reserved - int(nbytes * 2.2))

    # -- one item ----------------------------------------------------------
    def process(self, row: dict):
        hs = row["hs"]
        if self.stop.is_set() or (self.corpus / STOP_FILE).exists():
            self.stop.set()
            return
        ia = self.ia_state.get(hs)
        if ia and ia["has_pdf"]:
            self.record(hs, "on_ia", identifier=ia["identifier"])
            return
        if not self.reserve_disk(row["bytes"]):
            return
        try:
            self._process_reserved(row, ia)
        except Exception as e:  # keep the worker alive; item is retried next run
            log.exception("[%s] unexpected error", hs)
            self.record(hs, "error", error=str(e)[:300])
        finally:
            self.release_disk(row["bytes"])

    def _process_reserved(self, row: dict, ia):
        hs, title = row["hs"], row["title"]
        item_dir = self.work / hs
        item_dir.mkdir(parents=True, exist_ok=True)

        try:
            meta_full = sg.extract_metadata(row["url"])
        except sg.BotChallengeError as e:
            log.error("%s", e)
            self.record(hs, "blocked", error="bot challenge")
            self.stop.set()  # every further request would be challenged too
            return
        except Exception as e:
            self.record(hs, "metadata_failed", error=str(e)[:300])
            return
        title = meta_full.get("title") or title
        pdf_files = meta_full.get("pdf_files") or []
        if not pdf_files:
            self.record(hs, "no_pdf")
            shutil.rmtree(item_dir, ignore_errors=True)
            return

        identifier = ia["identifier"] if ia else sg.make_identifier(self.prefix, hs, title, item_dir)
        meta = sg.transform_to_ia_metadata(meta_full, self.prefix, self.collection)
        meta["description"] = clean_description(meta.get("description", ""))
        meta = {k: v for k, v in meta.items() if k in IA_META_KEYS}

        parts = []
        for i, pf in enumerate(pdf_files):
            dest = item_dir / f"{i:03d}_{sg.safe_filename(pf.get('filename', hs + '.pdf'), 120)}"
            ok = sg.download_file(pf["url"], dest, desc=f"{hs}/{i}")
            if not ok or not dest.exists() or not self._looks_like_pdf(dest):
                self.record(hs, "download_failed", identifier=identifier,
                            error=f"part {i} {pf.get('filename', '')}")
                return
            expected = pf.get("size_bytes", 0)
            if expected and dest.stat().st_size < expected * 0.9:
                self.record(hs, "download_failed", identifier=identifier,
                            error=f"part {i} truncated ({dest.stat().st_size} of ~{expected})")
                return
            parts.append(dest)

        merged = item_dir / f"{identifier}.pdf"
        if not sg.merge_pdfs(parts, merged) or not merged.exists() or merged.stat().st_size == 0:
            self.record(hs, "merge_failed", identifier=identifier)
            return

        if not self.upload(identifier, merged, meta, existing=bool(ia)):
            self.record(hs, "upload_failed", identifier=identifier)
            return
        size = merged.stat().st_size
        self.record(hs, "uploaded", identifier=identifier, bytes=size, parts=len(parts))
        if not self.args.keep_files:
            shutil.rmtree(item_dir, ignore_errors=True)

    @staticmethod
    def _looks_like_pdf(path: Path) -> bool:
        with open(path, "rb") as f:
            return f.read(1024).lstrip().startswith(b"%PDF")

    def upload(self, identifier: str, pdf: Path, meta: dict, existing: bool) -> bool:
        from internetarchive import get_item
        with self.upload_sem:
            for attempt in range(self.args.ia_retries):
                try:
                    item = get_item(identifier)
                    responses = item.upload(
                        files=[str(pdf)],
                        metadata=None if existing else meta,
                        retries=3, retries_sleep=10, queue_derive=True,
                        verbose=False,
                    )
                    if responses and all(getattr(r, "status_code", 0) == 200 for r in responses):
                        if existing:
                            item.modify_metadata({k: v for k, v in meta.items()
                                                  if k not in ("collection", "mediatype")})
                            if self.args.remove_placeholder:
                                self._remove_placeholder(item, identifier)
                        return True
                    log.warning("[%s] upload attempt %d: bad response %s",
                                identifier, attempt + 1, [getattr(r, "status_code", r) for r in responses or []])
                except Exception as e:
                    log.warning("[%s] upload attempt %d failed: %s", identifier, attempt + 1, e)
                time.sleep(10 * (2 ** attempt))
        return False

    @staticmethod
    def _remove_placeholder(item, identifier: str):
        """Delete the '...metadata-only record' .txt written by shg_ia_metadata_upload."""
        try:
            f = item.get_file(f"{identifier}.txt")
            if f is not None:
                f.delete(cascade_delete=True)
        except Exception as e:
            log.warning("[%s] could not remove placeholder: %s", identifier, e)


def select_queue(corpus: Path, args, results: dict, ia_state: dict) -> list:
    queue = load_jsonl(corpus / QUEUE_FILE)
    only = set(args.only or [])
    max_bytes = args.max_item_gb * GB
    todo = []
    for row in queue:
        hs = row["hs"]
        if only and hs not in only:
            continue
        res = results.get(hs)
        if res and res["status"] in TERMINAL:
            continue
        if res and args.skip_failed:
            continue
        if ia_state.get(hs, {}).get("has_pdf"):
            continue
        if row["bytes"] > max_bytes:
            continue
        todo.append(row)
    if args.limit:
        todo = todo[:args.limit]
    return todo


def cmd_run(args):
    corpus = Path(args.dir)
    if not (corpus / QUEUE_FILE).exists():
        log.error("No queue: run 'plan' first")
        return 1
    results = load_results(corpus)
    ia_state = load_ia_state(corpus)
    if not ia_state:
        log.warning("No %s: run 'reconcile-ia' first or items already on IA will be re-uploaded "
                    "(the upload step still refuses to replace an existing PDF)", IA_STATE_FILE)
    todo = select_queue(corpus, args, results, ia_state)
    total_bytes = sum(r["bytes"] for r in todo)
    print(f"Pending: {len(todo):,} items, {total_bytes / GB:,.1f} GB "
          f"(workers={args.workers}, max item {args.max_item_gb} GB, "
          f"{len(ia_state):,} shg items known on IA)")

    pre = preflight(corpus, need_upload=args.execute)
    for name, (ok, detail) in pre.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name:16s} {detail}")

    if not args.execute:
        for r in todo[:10]:
            ia = ia_state.get(r["hs"])
            print(f"  would process {r['hs']}  {r['bytes'] / 1e6:8.1f} MB  {r['files']:3d} files  "
                  f"{'-> add PDF to ' + ia['identifier'] if ia else '-> new item'}  {r['title'][:50]}")
        print("Preview only. Re-run with --execute to download and upload.")
        return 0

    failed = [n for n, (ok, _) in pre.items() if not ok and n != "disk_free"]
    if failed and not args.force:
        log.error("Preflight failed (%s). Fix it or pass --force.", ", ".join(failed))
        return 1
    if not todo:
        print("Nothing to do.")
        return 0

    harvest = Harvest(args, corpus)
    harvest.work.mkdir(parents=True, exist_ok=True)
    (corpus / STOP_FILE).unlink(missing_ok=True)
    start = time.time()
    pending = set()
    it = iter(todo)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # Bounded submission keeps the queue responsive to stop requests.
            while True:
                while len(pending) < args.workers * 2 and not harvest.stop.is_set():
                    row = next(it, None)
                    if row is None:
                        break
                    pending.add(pool.submit(harvest.process, row))
                if not pending:
                    break
                done, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                if (corpus / STOP_FILE).exists():
                    harvest.stop.set()
                if harvest.stop.is_set() and not pending:
                    break
                done_n = sum(harvest.counts.values())
                if done:
                    rate = done_n / max(time.time() - start, 1) * 60
                    log.info("progress: %s | %.1f items/min", dict(harvest.counts), rate)
    except KeyboardInterrupt:
        log.warning("Interrupted: finishing in-flight items (Ctrl+C again to abort)")
        harvest.stop.set()
    print(f"Done: {dict(harvest.counts)}")
    return 1 if harvest.stop.is_set() and harvest.consecutive_failures >= args.max_consecutive_failures else 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args):
    corpus = Path(args.dir)
    queue = load_jsonl(corpus / QUEUE_FILE)
    results = load_results(corpus)
    ia_state = load_ia_state(corpus)
    by_status = collections.Counter(r["status"] for r in results.values())
    up_bytes = sum(r.get("bytes", 0) for r in results.values() if r["status"] == "uploaded")
    on_ia_pdf = sum(1 for v in ia_state.values() if v["has_pdf"])
    queued_hs = {r["hs"] for r in queue}
    done_hs = {h for h, r in results.items() if r["status"] in TERMINAL} | \
              {h for h, v in ia_state.items() if v["has_pdf"]}
    remaining = [r for r in queue if r["hs"] not in done_hs]
    print(f"Queue:                 {len(queue):,} items, {sum(r['bytes'] for r in queue) / 1e12:.2f} TB")
    print(f"IA shg items:          {len(ia_state):,} (with PDF {on_ia_pdf:,}, metadata-only {len(ia_state) - on_ia_pdf:,})")
    print(f"Harvest results:       {dict(by_status) or 'none yet'}")
    print(f"Uploaded by harvest:   {up_bytes / GB:,.1f} GB")
    print(f"Remaining in queue:    {len(remaining):,} items, {sum(r['bytes'] for r in remaining) / 1e12:.2f} TB")
    failing = [h for h, r in results.items() if r["status"] not in TERMINAL]
    if failing:
        print(f"Retryable failures:    {len(failing):,} (re-run 'run' to retry; --skip-failed to skip)")
        if args.verbose:
            for h in failing[:20]:
                r = results[h]
                print(f"  {h}: {r['status']} {r.get('error', '')}")
    stale = [h for h in ia_state if h not in queued_hs]
    if stale:
        print(f"On IA but not in queue: {len(stale):,}")
    return 0


# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--dir", default="shg_corpus",
                        help="Corpus directory holding manifest.json (default: shg_corpus)")

    sp = sub.add_parser("preflight", help="Check connectivity, merge tools, IA credentials, disk")
    common(sp)
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("plan", help="Review manifest and build the work queue")
    common(sp)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("reconcile-ia", help="Snapshot shg.* items already on archive.org")
    common(sp)
    sp.set_defaults(func=cmd_reconcile_ia)

    sp = sub.add_parser("run", help="Process queue: metadata -> download -> merge -> upload -> delete")
    common(sp)
    sp.add_argument("--execute", action="store_true",
                    help="Actually download and upload (default is a preview)")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--limit", type=int, help="Process at most N items")
    sp.add_argument("--only", nargs="+", metavar="HANDLE_SUFFIX", help="Only these handle suffixes")
    sp.add_argument("--max-item-gb", type=float, default=5.0,
                    help="Skip items larger than this (default 5)")
    sp.add_argument("--min-free-gb", type=float, default=100.0,
                    help="Keep this much disk free; workers wait when short (default 100)")
    sp.add_argument("--work-dir", help="Scratch directory (default <dir>/work)")
    sp.add_argument("--prefix", default="shg.")
    sp.add_argument("--collection", default="shodhganga")
    sp.add_argument("--upload-slots", type=int, default=2, help="Concurrent IA uploads")
    sp.add_argument("--ia-retries", type=int, default=3)
    sp.add_argument("--max-consecutive-failures", type=int, default=20,
                    help="Abort after this many failures in a row (default 20)")
    sp.add_argument("--skip-failed", action="store_true", help="Do not retry earlier failures")
    sp.add_argument("--keep-files", action="store_true", help="Keep local PDFs after upload")
    sp.add_argument("--remove-placeholder", action="store_true",
                    help="Delete the metadata-only .txt from items that receive their PDF")
    sp.add_argument("--force", action="store_true", help="Continue despite failed preflight")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="Show queue, IA and harvest progress")
    common(sp)
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
