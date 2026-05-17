#!/usr/bin/env python3
"""
Download zipped uploads from IA, extract files, and re-upload them as individual
files to the same identifier so IA can derive previews/metadata from the content.

Usage:
  python tdl_unzip_and_push.py                          # process all uploaded items
  python tdl_unzip_and_push.py --cat Sculpture Book       # specific categories
  python tdl_unzip_and_push.py --ident tdl.12345-foo      # single item
  python tdl_unzip_and_push.py --dry-run                  # preview only
"""

import json, os, subprocess, sys, zipfile, shutil, urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROGRESS_FILE = Path(__file__).parent / "upload_progress.json"
DONE_FILE = Path(__file__).parent / "unzip_done.json"
FAILED_FILE = Path(__file__).parent / "unzip_failed.json"
TMP = Path("/var/folders/tm/6p25d8_13wj31kg9xvv74mwh0000gn/T/opencode") / "unzip_push"
WORKERS = 3

CATEGORIES = [
    "Audio", "AuthorBio", "Book", "Coin", "CopperPlate", "Document",
    "Excavation", "HistoricalMonument", "Inscription", "Map", "Painting",
    "Palmleaf", "Periodical", "Photograph", "PreHistoric",
    "ReligiousPlace", "Sculpture", "Video",
]

def already_processed(ident: str) -> bool:
    if not DONE_FILE.exists():
        return False
    done = json.loads(DONE_FILE.read_text())
    return ident in done

def mark_done(ident: str, done: list):
    DONE_FILE.write_text(json.dumps(done))

def log_failed(ident: str, reason: str):
    failed = []
    if FAILED_FILE.exists():
        failed = json.loads(FAILED_FILE.read_text())
    failed.append({"identifier": ident, "reason": reason})
    FAILED_FILE.write_text(json.dumps(failed, indent=2))

def find_zip_on_ia(ident: str):
    r = subprocess.run(
        ["ia", "metadata", ident],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return None
    try:
        meta = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not meta or "metadata" not in meta:
        return None
    files = meta.get("files", [])
    for f in files:
        if f["name"].endswith(".zip"):
            return f["name"]
    return None

def process_item(ident: str) -> bool:
    if already_processed(ident):
        return True

    zip_name = find_zip_on_ia(ident)
    if not zip_name:
        print(f"  - {ident}: no zip found, skipping")
        return True  # mark done — nothing to do

    tmp_dir = TMP / ident[4:30]  # short slug for temp
    extract_dir = tmp_dir / "extracted"

    try:
        # Download zip
        safe_zip = zip_name.replace(" ", "_")
        zip_path = tmp_dir / safe_zip
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://archive.org/download/{ident}/{urllib.parse.quote(zip_name)}"
        r = subprocess.run(
            ["curl", "-sSL", "-o", str(zip_path), url],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not zip_path.exists():
            raise RuntimeError(f"download failed: {r.stderr.strip() or r.stdout.strip()[:100]}")

        # Extract
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)

        # Collect extracted files
        extracted = sorted(
            str(f) for f in extract_dir.rglob("*")
            if f.is_file() and f.name != "metadata.json"
        )
        if not extracted:
            raise RuntimeError("no files extracted from zip")

        # Upload individual files to same identifier (no metadata flags — just add files)
        cmd = ["ia", "upload", ident] + extracted
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"upload failed: {(r.stderr.strip() or r.stdout.strip())[:200]}")

        # Delete the zip from IA
        r = subprocess.run(
            ["ia", "delete", ident, zip_name],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"  ! {ident}: files uploaded but zip delete failed: {r.stderr.strip()[:100]}")

        print(f"  ✓ {ident}: {len(extracted)} files pushed, zip removed")
        return True

    except Exception as e:
        print(f"  ✗ {ident}: {e}")
        log_failed(ident, str(e))
        return False
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

def get_uploaded_items(cat_filter=None):
    if not PROGRESS_FILE.exists():
        print("upload_progress.json not found")
        sys.exit(1)
    d = json.loads(PROGRESS_FILE.read_text())
    items = d.get("uploaded", [])
    return items

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Unzip and re-upload items to IA")
    parser.add_argument("--cat", nargs="+", help="Process only these categories")
    parser.add_argument("--ident", help="Process a single identifier")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset", action="store_true", help="Reset done list")
    args = parser.parse_args()

    if args.reset and DONE_FILE.exists():
        DONE_FILE.unlink()
        print("Reset done list")

    if args.ident:
        items = [args.ident]
    else:
        items = get_uploaded_items(args.cat)

    if not items:
        print("No items to process")
        return

    # Load done list
    done = json.loads(DONE_FILE.read_text()) if DONE_FILE.exists() else []

    if args.dry_run:
        print(f"Would process {len(items)} items ({args.workers} workers)")
        for ident in items[:20]:
            print(f"  {ident}")
        if len(items) > 20:
            print(f"  ... and {len(items)-20} more")
        return

    print(f"Processing {len(items)} items ({args.workers} workers)")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for ident in items:
            if ident in done:
                continue
            futures[pool.submit(process_item, ident)] = ident

        completed = 0
        for fut in as_completed(futures):
            ident = futures[fut]
            if fut.result():
                done.append(ident)
                mark_done(ident, done)
                completed += 1

    print(f"\nDone: {completed} processed")

if __name__ == "__main__":
    main()
