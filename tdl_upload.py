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

try:
    from unidecode import unidecode
except ImportError:
    unidecode = None

CORPUS = Path(__file__).parent / "tdl_corpus"
COLLECTION = "TamilVirtualAcademy"
WORKERS = 5
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
    parts = [p.strip() for p in ascii_chars if p.strip() and len(p.strip()) > 1]
    return ' '.join(parts) if parts else ''

PREFIX = "tdl."

def sanitize_identifier(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9_-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-').strip('_').lower()
    # IA identifier max is 80 chars; 4 are reserved for "tdl." prefix
    return s[:76]

def make_identifier(cat: str, folder: Path) -> str:
    meta_file = folder / "metadata.json"
    try:
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    except (json.JSONDecodeError, ValueError):
        return None
    art_id = meta.get("identifier", "")
    title = meta.get("title", folder.name)

    base = art_id if art_id else folder.name.split("_")[0]

    # If ID already has tdl_ prefix, append a title slug from the folder name
    if base.startswith("tdl_"):
        # folder: tdl_{hash}_{title} → extract title part after second _
        parts = folder.name.split("_", 2)
        title_part = parts[2] if len(parts) > 2 else ""
        if title_part and unidecode:
            slug = sanitize_identifier(unidecode(title_part))
            if slug:
                return f"{PREFIX}{base}-{slug}"
        return f"{PREFIX}{base}"

    # Extract English/Latin text from title
    english = extract_english(title)

    # If no English text, transliterate the Tamil title
    if not english and unidecode:
        translit = unidecode(title)
        slug = sanitize_identifier(translit)
        if slug:
            return f"{PREFIX}{base}-{slug}"

    # Fallback to hash
    if not english:
        h = hashlib.md5(folder.name.encode()).hexdigest()[:8]
        return f"{PREFIX}{base}-{h}"

    slug = sanitize_identifier(f"{base}-{english}")
    if not slug.replace('-', ''):
        h = hashlib.md5(folder.name.encode()).hexdigest()[:8]
        slug = f"{base}-{h}"
    return f"{PREFIX}{slug}"


def shorten_filename(path: Path) -> Path:
    """Rename a file if its UTF-8 byte-length exceeds IA's 230-byte S3 limit."""
    MAX_COMPONENT_BYTES = 230
    raw = path.name.encode("utf-8")
    if len(raw) <= MAX_COMPONENT_BYTES:
        return path

    stem = path.stem
    ext = path.suffix
    max_stem = MAX_COMPONENT_BYTES - len(ext.encode("utf-8")) - 1
    if max_stem < 16:
        new_stem = hashlib.md5(stem.encode()).hexdigest()[:16]
    else:
        stem_bytes = stem.encode("utf-8")
        while len(stem_bytes) > max_stem:
            stem = stem[:-1]
            stem_bytes = stem.encode("utf-8")
        new_stem = stem

    new_path = path.with_name(new_stem + ext)
    path.rename(new_path)
    return new_path


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        for _ in range(3):
            try:
                return json.loads(PROGRESS_FILE.read_text())
            except json.JSONDecodeError:
                import time; time.sleep(0.5)
        return json.loads(PROGRESS_FILE.read_text())
    return {"uploaded": [], "uploaded_by_cat": {}, "failed": []}

def save_progress(progress: dict):
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=2))
    tmp.replace(PROGRESS_FILE)

def log_failed(cat: str, folder: str, identifier: str, reason: str):
    failed = []
    if FAILED_FILE.exists():
        for _ in range(3):
            try:
                failed = json.loads(FAILED_FILE.read_text())
                break
            except json.JSONDecodeError:
                import time; time.sleep(0.5)
        else:
            try:
                failed = json.loads(FAILED_FILE.read_text())
            except json.JSONDecodeError:
                failed = []
    failed.append({
        "category": cat, "folder": folder,
        "identifier": identifier, "reason": reason,
        "time": datetime.now().isoformat(),
    })
    tmp = FAILED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(failed, indent=2))
    tmp.replace(FAILED_FILE)

def already_on_ia(identifier: str) -> bool:
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["ia", "metadata", identifier],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            if attempt < 2:
                time.sleep(2)
                continue
            return False
        if r.returncode != 0:
            return False
        try:
            d = json.loads(r.stdout)
            return bool(d) and "metadata" in d
        except (json.JSONDecodeError, TypeError):
            return False
    return False

def prepare_metadata(meta: dict, category: str, fallback_collection=False) -> list:
    keep = {"title", "description", "subject", "creator", "publisher",
            "date", "language", "identifier", "pages", "edition", "volume",
            "collection", "mediatype", "source", "original_url"}
    cleaned = {k: v for k, v in meta.items() if k in keep}
    cleaned["language"] = "tam"
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

def run_with_retry(cmd: list, max_retries=3, timeout=1200) -> subprocess.CompletedProcess:
    import time
    for attempt in range(max_retries):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r
        err = (r.stderr.strip() or r.stdout.strip()).lower()
        if "timeout" in err or "timed out" in err or "connection" in err or "slowdown" in err:
            if attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  ! transient error, retrying in {wait}s...")
                time.sleep(wait)
                continue
        return r
    return r

def upload_item(cat: str, folder: Path, no_collection_check=False) -> bool:
    base_identifier = make_identifier(cat, folder)

    if not folder.exists():
        return False

    meta_file = folder / "metadata.json"
    if not meta_file.exists():
        return False
    meta = json.loads(meta_file.read_text())

    files = sorted(f for f in folder.iterdir() if f.is_file() and f.name != "metadata.json")
    # Ensure filenames fit IA's 230-byte S3 key component limit
    files = [shorten_filename(f) for f in files]
    # Re-check files exist (may have been deleted between scan and now)
    existing = [f for f in files if f.exists()]
    for f in files:
        if not f.exists():
            print(f"  ! {base_identifier}: file vanished, skipping: {f.name}")
    files = existing
    if not files:
        print(f"  ! {base_identifier}: no files to upload")
        return False

    # Try identifier with dedup suffixes on bucket collision.
    # For each candidate: if it already exists on IA it's either our item
    # (suffix == "") or a collision with another item (suffix != "").
    for suffix in ("", "-001", "-002", "-003"):
        identifier = base_identifier + suffix
        exists = already_on_ia(identifier)

        if exists and not suffix:
            # Original identifier — our item is already uploaded
            print(f"  - {identifier}: already on IA")
            shutil.rmtree(folder, ignore_errors=True)
            return True

        if exists and suffix:
            # Suffix is taken by a different item — try next suffix
            continue

        meta_flags = prepare_metadata(meta, cat)
        if no_collection_check:
            meta_flags.append("--no-collection-check")
        cmd = ["ia", "upload", identifier] + [str(f) for f in files] + meta_flags

        try:
            r = run_with_retry(cmd)
        except FileNotFoundError:
            raise RuntimeError("ia command not found (PATH issue?)")

        if r.returncode == 0:
            shutil.rmtree(folder, ignore_errors=True)
            return True

        err = r.stderr.strip() or r.stdout.strip()

        # Bucket collision — try next suffix
        if "bucket" in err.lower() and "not available" in err.lower():
            print(f"  ! {identifier}: bucket collision, trying -001 suffix")
            continue

        # File vanished between scan and upload — skip
        if "not a valid file" in err.lower() or "usage:" in err.lower():
            print(f"  ! {identifier}: file not found, skipping")
            return False

        if "Access Denied" in err:
            print(f"  ! {identifier}: no access to {COLLECTION}, retrying with opensource")
            meta_flags2 = prepare_metadata(meta, cat, fallback_collection=True)
            if no_collection_check:
                meta_flags2.append("--no-collection-check")
            cmd2 = ["ia", "upload", identifier] + [str(f) for f in files] + meta_flags2
            try:
                r2 = run_with_retry(cmd2)
            except FileNotFoundError:
                raise RuntimeError("ia command not found (PATH issue?)")
            if r2.returncode == 0:
                shutil.rmtree(folder, ignore_errors=True)
                return True
            raise RuntimeError(r2.stderr.strip() or r2.stdout.strip())

        raise RuntimeError(err)

    raise RuntimeError(f"All identifier suffixes exhausted for {base_identifier}")

def folder_size_mb(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.iterdir() if f.is_file()) / (1024 * 1024)


def scan_items(category_filter=None, corpus_path=None, max_size_mb=None):
    if corpus_path is None:
        corpus_path = CORPUS
    if isinstance(category_filter, str):
        category_filter = [category_filter]
    selected = set(category_filter) if category_filter else None
    per_cat = {}
    for cat in CATEGORIES:
        if selected and cat not in selected:
            continue
        cat_dir = corpus_path / cat
        if not cat_dir.exists():
            continue
        folders = []
        for folder in sorted(cat_dir.iterdir()):
            if folder.is_dir() and (folder / "metadata.json").exists():
                if max_size_mb is not None and folder_size_mb(folder) > max_size_mb:
                    continue
                folders.append((cat, folder))
        if folders:
            # Books: oldest first so they get uploaded before newer ones
            if cat == "Book":
                folders.sort(key=lambda x: x[1].stat().st_mtime)
            per_cat[cat] = folders
    # Interleave round-robin so workers get mixed categories
    items = []
    while per_cat:
        for cat in list(per_cat.keys()):
            if per_cat[cat]:
                items.append(per_cat[cat].pop(0))
            if not per_cat[cat]:
                del per_cat[cat]
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
    parser.add_argument("--cat", nargs="+", help="Upload only these categories")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed items")
    parser.add_argument("--workers", type=int, default=WORKERS, help="Parallel uploads")
    parser.add_argument("--no-collection-check", action="store_true", help="Skip collection permission check")
    parser.add_argument("--corpus", help="Override corpus directory path")
    parser.add_argument("--delete", metavar="IDENTIFIER", help="Delete an item from IA")
    parser.add_argument("--max-size-mb", type=float, help="Only upload folders at or below this size")
    parser.add_argument("--cleanup-uploaded-local", action="store_true",
                        help="Delete local folders already recorded as uploaded in upload_progress.json")
    args = parser.parse_args()

    if args.delete:
        r = subprocess.run(["ia", "delete", args.delete], capture_output=True, text=True, timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
        return

    if args.corpus:
        corpus_path = Path(args.corpus)
    else:
        corpus_path = CORPUS

    progress = load_progress()
    uploaded_ids = set(progress.get("uploaded", []))

    if args.retry_failed and FAILED_FILE.exists():
        failed = json.loads(FAILED_FILE.read_text())
        items = [(f["category"], corpus_path / f["category"] / f["folder"]) for f in failed]
        FAILED_FILE.unlink()
    else:
        items = scan_items(args.cat, corpus_path, args.max_size_mb)

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
            if ident is None:
                skipped += 1
                continue
            if ident in uploaded_ids:
                if args.cleanup_uploaded_local:
                    shutil.rmtree(folder, ignore_errors=True)
                    print(f"  - {ident}: already uploaded, removed local folder")
                skipped += 1
                continue
            futures[pool.submit(upload_item, cat, folder, args.no_collection_check)] = (cat, folder, ident)

        for fut in as_completed(futures):
            cat, folder, ident = futures[fut]
            try:
                fut.result()
                uploaded_ids.add(ident)
                progress["uploaded"] = list(uploaded_ids)
                progress.setdefault("uploaded_by_cat", {})
                progress["uploaded_by_cat"][cat] = progress["uploaded_by_cat"].get(cat, 0) + 1
                save_progress(progress)
                done += 1
                print(f"  ✓ [{done}] {ident}")
            except subprocess.TimeoutExpired:
                msg = "Timeout"
                try:
                    log_failed(cat, folder.name, ident, msg)
                except Exception:
                    pass
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")
            except RuntimeError as e:
                msg = str(e)
                try:
                    log_failed(cat, folder.name, ident, msg)
                except Exception:
                    pass
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")
            except Exception as e:
                import traceback
                msg = f"{e}\n{traceback.format_exc()}"
                try:
                    log_failed(cat, folder.name, ident, msg)
                except Exception:
                    pass
                print(f"  ✗ [{done+skipped+1}] {ident}: {msg}")

    print(f"\nDone: {done} uploaded, {skipped} skipped, {len(progress.get('failed',[]))} failed")

if __name__ == "__main__":
    main()
