#!/usr/bin/env python3
"""
Upload downloaded digital archive items to Internet Archive.
Collection: TamilVirtualAcademy

Usage:
  python tdl_upload.py                          # upload all categories
  python tdl_upload.py --cat Book               # upload a single category
  python tdl_upload.py --dry-run                # preview without uploading
  python tdl_upload.py --retry-failed           # retry previously failed items
"""

import json, os, subprocess, sys, time, re, hashlib, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

CORPUS = Path(__file__).parent / "tdl_corpus"
COLLECTION = "TamilVirtualAcademy"
WORKERS = 3
PROGRESS_FILE = Path(__file__).parent / "upload_progress.json"
FAILED_FILE = Path(__file__).parent / "upload_failed.json"

CATEGORIES = [
    "Audio", "AuthorBio", "Book", "Coin", "CopperPlate", "Document",
    "Excavation", "HistoricalMonument", "Inscription", "Map", "Painting",
    "Palmleaf", "Periodical", "Photograph", "PreHistoric",
    "ReligiousPlace", "Sculpture", "Video",
]

def extract_english(text: str) -> str:
    ascii_chars = re.findall(r'[a-zA-Z0-9_.()&,;:!?\'" -]+', text)
    parts = [p.strip() for p in ascii_chars if p.strip()]
    return ' '.join(parts) if parts else ''

PREFIX = "tdl."

def sanitize_identifier(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9_-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-').strip('_').lower()
    return s[:100]

def make_identifier(cat: str, folder: Path) -> str:
    meta_file = folder / "metadata.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    art_id = meta.get("identifier", "")
    title = meta.get("title", folder.name)

    # Use article ID as base
    base = art_id if art_id else folder.name.split("_")[0]

    # If ID already has tdl_ prefix, use tdl.id directly
    if base.startswith("tdl_"):
        return f"{PREFIX}{base}"

    # Extract English/Latin text from title
    english = extract_english(title)

    # If no English text, use a short hash of the folder name
    if not english:
        h = hashlib.md5(folder.name.encode()).hexdigest()[:8]
        return f"{PREFIX}{base}-{h}"

    slug = sanitize_identifier(f"{base}-{english}")
    if not slug.replace('-', ''):
        h = hashlib.md5(folder.name.encode()).hexdigest()[:8]
        slug = f"{base}-{h}"
    return f"{PREFIX}{slug}"

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"uploaded": [], "failed": []}

def save_progress(progress: dict):
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))

def log_failed(cat: str, folder: str, identifier: str, reason: str):
    failed = []
    if FAILED_FILE.exists():
        failed = json.loads(FAILED_FILE.read_text())
    failed.append({
        "category": cat, "folder": folder,
        "identifier": identifier, "reason": reason,
        "time": datetime.now().isoformat(),
    })
    FAILED_FILE.write_text(json.dumps(failed, indent=2))

def already_on_ia(identifier: str) -> bool:
    r = subprocess.run(
        ["ia", "metadata", identifier],
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0

def prepare_metadata(meta: dict, category: str, fallback_collection=False) -> list:
    keep = {"title", "description", "subject", "creator", "publisher",
            "date", "language", "identifier", "pages", "edition", "volume",
            "collection", "mediatype", "source"}
    cleaned = {k: v for k, v in meta.items() if k in keep}
    cleaned["collection"] = "opensource" if fallback_collection else COLLECTION
    subjects = list(meta.get("subject", [])) if isinstance(meta.get("subject"), list) else []
    if not subjects and category:
        subjects = [category]
    if category and category not in subjects:
        subjects.append(category)
    cleaned["subject"] = subjects
    out = []
    for k, v in cleaned.items():
        if isinstance(v, list):
            for item in v:
                out.append(f"-m{k}:{item}")
        elif v is not None:
            out.append(f"-m{k}:{v}")
    return out

def make_zip(folder: Path) -> Path:
    zip_name = folder.parent / f"{folder.name}.zip"
    if zip_name.exists():
        zip_name.unlink()
    shutil.make_archive(str(zip_name.with_suffix('')), 'zip', folder)
    return zip_name

def upload_item(cat: str, folder: Path, no_collection_check=False) -> bool:
    identifier = make_identifier(cat, folder)
    meta_file = folder / "metadata.json"
    if not meta_file.exists():
        return False
    meta = json.loads(meta_file.read_text())

    # Create zip of the entire folder
    zip_path = make_zip(folder)
    try:
        meta_flags = prepare_metadata(meta, cat)
        if no_collection_check:
            meta_flags.append("--no-collection-check")
        cmd = ["ia", "upload", identifier, str(zip_path)] + meta_flags
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            return True

        err = r.stderr.strip() or r.stdout.strip()
        if "Access Denied" in err:
            print(f"  ! {identifier}: no access to {COLLECTION}, retrying with opensource")
            meta_flags2 = prepare_metadata(meta, cat, fallback_collection=True)
            if no_collection_check:
                meta_flags2.append("--no-collection-check")
            cmd2 = ["ia", "upload", identifier, str(zip_path)] + meta_flags2
            r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=600)
            if r2.returncode == 0:
                return True
            raise RuntimeError(r2.stderr.strip() or r2.stdout.strip())
        raise RuntimeError(err)
    finally:
        if zip_path.exists():
            zip_path.unlink()

def scan_items(category_filter=None):
    items = []
    for cat in CATEGORIES:
        if category_filter and cat != category_filter:
            continue
        cat_dir = CORPUS / cat
        if not cat_dir.exists():
            continue
        for folder in sorted(cat_dir.iterdir()):
            if folder.is_dir() and (folder / "metadata.json").exists():
                items.append((cat, folder))
    return items

def dry_run(items):
    print(f"{'Category':<20} {'Identifier':<55} Files")
    print(f"{'─'*20} {'─'*55} {'─'*10}")
    for cat, folder in items:
        ident = make_identifier(cat, folder)
        fcount = sum(1 for f in folder.iterdir() if f.is_file() and f.name != "metadata.json")
        meta = json.loads((folder / "metadata.json").read_text())
        subjects = meta.get("subject", [cat]) if isinstance(meta.get("subject"), list) else [cat]
        print(f"{cat:<20} {ident:<55} {fcount} files, {len(subjects)} subjects")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Upload to Internet Archive")
    parser.add_argument("--cat", help="Upload only this category")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed items")
    parser.add_argument("--workers", type=int, default=WORKERS, help="Parallel uploads")
    parser.add_argument("--no-collection-check", action="store_true", help="Skip collection permission check")
    parser.add_argument("--delete", metavar="IDENTIFIER", help="Delete an item from IA")
    args = parser.parse_args()

    if args.delete:
        r = subprocess.run(["ia", "delete", args.delete], capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
        return

    progress = load_progress()
    uploaded_ids = set(progress.get("uploaded", []))

    if args.retry_failed and FAILED_FILE.exists():
        failed = json.loads(FAILED_FILE.read_text())
        items = [(f["category"], CORPUS / f["category"] / f["folder"]) for f in failed]
        FAILED_FILE.unlink()
    else:
        items = scan_items(args.cat)

    if not items:
        print("No items found to upload.")
        return

    if args.dry_run:
        dry_run(items)
        return

    print(f"Found {len(items)} items to upload ({args.workers} workers)")
    done = 0
    skipped = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for cat, folder in items:
            ident = make_identifier(cat, folder)
            if ident in uploaded_ids:
                skipped += 1
                continue
            futures[pool.submit(upload_item, cat, folder, args.no_collection_check)] = (cat, folder, ident)

        for fut in as_completed(futures):
            cat, folder, ident = futures[fut]
            try:
                fut.result()
                uploaded_ids.add(ident)
                progress["uploaded"] = list(uploaded_ids)
                save_progress(progress)
                done += 1
                print(f"  ✓ [{done}] {ident}")
            except subprocess.TimeoutExpired:
                msg = "Timeout"
                log_failed(cat, folder.name, ident, msg)
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")
            except RuntimeError as e:
                msg = str(e)
                log_failed(cat, folder.name, ident, msg)
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")
            except Exception as e:
                msg = str(e)
                log_failed(cat, folder.name, ident, msg)
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")

    print(f"\nDone: {done} uploaded, {skipped} skipped, {len(progress.get('failed',[]))} failed")

if __name__ == "__main__":
    main()
