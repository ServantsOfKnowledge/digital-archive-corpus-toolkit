#!/usr/bin/env python3
"""Backfill original_url metadata on already-uploaded IA items.

Reads upload_progress.json, extracts article_id from each identifier,
constructs the TDL source URL, and adds it via `ia metadata`.

Usage:
  python3 tdl_backfill_url.py [--workers 8] [--dry-run]
"""
import json, re, subprocess, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

PROGRESS_FILE = Path(__file__).parent / "upload_progress.json"
WORKERS = 8

def extract_article_id(ident: str) -> Optional[str]:
    m = re.match(r'tdl\.(\d+)-', ident)
    if m:
        return m.group(1)
    return None

def make_url(article_id: str) -> str:
    return f"https://tamildigitallibrary.in/Articles/{article_id}"

def has_original_url(ident: str) -> bool:
    try:
        r = subprocess.run(
            ["ia", "metadata", ident],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False
        meta = json.loads(r.stdout)
        metadata_fields = meta.get("metadata", {})
        if metadata_fields.get("original_url"):
            return True
        # Also check if it's in the raw metadata dict
        for val in metadata_fields.values():
            if isinstance(val, str) and val.startswith("https://tamildigitallibrary.in/"):
                if "Articles/" in val:
                    return True
        return False
    except Exception:
        return False

def set_original_url(ident: str, url: str) -> bool:
    r = subprocess.run(
        ["ia", "metadata", ident, "-m", f"original_url:{url}"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip()
        raise RuntimeError(err)
    return True

def process_item(ident: str) -> tuple[str, bool, str]:
    article_id = extract_article_id(ident)
    if not article_id:
        return ident, False, "non-numeric ID, skipping"
    url = make_url(article_id)
    try:
        ok = set_original_url(ident, url)
        if ok:
            return ident, True, "added"
        return ident, False, "failed"
    except Exception as e:
        msg = str(e)
        if "cannot be located" in msg or "does not exist" in msg:
            return ident, False, "item not found (dark/deleted)"
        return ident, False, msg

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not PROGRESS_FILE.exists():
        print("upload_progress.json not found")
        sys.exit(1)

    d = json.loads(PROGRESS_FILE.read_text())
    items = d.get("uploaded", [])
    print(f"Found {len(items)} uploaded items")

    if args.dry_run:
        sample = items[:10]
        for ident in sample:
            aid = extract_article_id(ident)
            url = make_url(aid) if aid else "?"
            print(f"  {ident} -> {url}" + (" (skip)" if not aid else ""))
        if len(items) > 10:
            print(f"  ... and {len(items)-10} more")
        return

    added = 0
    skipped = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_item, ident): ident for ident in items}
        for fut in as_completed(futures):
            ident, ok, msg = fut.result()
            if msg == "already exists":
                skipped += 1
            elif ok:
                added += 1
                print(f"  ✓ {ident}: {msg}")
            else:
                failed += 1
                print(f"  ✗ {ident}: {msg}")

    print(f"\nDone: {added} added, {skipped} already existed, {failed} failed")

if __name__ == "__main__":
    main()
