#!/usr/bin/env python3
"""Prepare a server checkout to redownload corpus items and resume uploads."""

import argparse
import logging
import shutil
import subprocess
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "tdl_corpus"
LOGS = ROOT / "logs"


def run(cmd, background=False, log_file=None):
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    if background:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = log_file.open("ab")
        subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        return None
    return subprocess.run(cmd, cwd=ROOT, check=True)


def restore_state(state_dir: Path):
    if not state_dir.exists():
        print(f"State directory not found: {state_dir}")
        return

    CORPUS.mkdir(exist_ok=True)
    for name in ("upload_progress.json", "upload_failed.json", "pdf_redownload.json"):
        src = state_dir / name
        if src.exists():
            shutil.copy2(src, ROOT / name)
            print(f"Restored {name}")

    for listing in state_dir.glob("listing_*.json"):
        shutil.copy2(listing, CORPUS / listing.name)
        print(f"Restored {listing.name}")


def download_categories(categories, workers):
    """Download and immediately upload items for each category.
    
    Multi-worker support: The --workers flag is passed to tdl_downloader.py,
    which uses ThreadPoolExecutor internally for parallel processing within
    each category. Categories are processed sequentially.
    
    If one category fails, logs the error and continues with the next.
    """
    success = []
    failed = []
    
    for cat in categories:
        listing = CORPUS / f"listing_{cat}.json"
        if not listing.exists():
            log.error("Missing listing file: %s", listing)
            failed.append(cat)
            continue
        
        log.info("Starting download+upload pipeline for category: %s (workers=%s)", cat, workers)
        try:
            run([
                "python3", "tdl_downloader.py", "download",
                "--input", str(listing),
                "--dir", str(CORPUS),
                "--cat-name", cat,
                "--workers", str(workers),
                "--resume",
                "--upload-immediately",
            ])
            log.info("Completed download+upload pipeline for category: %s", cat)
            success.append(cat)
        except subprocess.CalledProcessError as e:
            log.error("Failed to process category %s: %s", cat, e)
            failed.append(cat)
        except Exception as e:
            log.error("Unexpected error processing category %s: %s", cat, e)
            failed.append(cat)
    
    # Summary
    log.info("=" * 60)
    log.info("Download+upload pipeline summary:")
    log.info("  Successful: %s", success if success else "None")
    log.info("  Failed:     %s", failed if failed else "None")
    log.info("=" * 60)


def start_uploads(categories, workers, max_size_mb):
    """Start background upload processes for residual items.
    
    This is a safety net for any items that failed during the 
    download+upload pipeline. Runs tdl_upload.py in background.
    
    Multi-worker support: --workers is passed to tdl_upload.py which
    uses ThreadPoolExecutor internally for parallel uploads.
    """
    for cat in categories:
        log_name = f"{cat.lower()}_upload.log"
        if cat == "Book":
            log_name = "books_upload.log"
        if cat == "Periodical":
            log_name = "periodicals_upload.log"
        cmd = [
            "python3", "-u", "tdl_upload.py",
            "--cat", cat,
            "--workers", str(workers),
            "--no-collection-check",
        ]
        if max_size_mb is not None:
            cmd.extend(["--max-size-mb", str(max_size_mb)])
        run(cmd, background=True, log_file=LOGS / log_name)
        print(f"Started {cat} upload; log: logs/{log_name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="server_state", help="Directory containing upload state and listing files")
    parser.add_argument("--cat", nargs="+", default=["Book", "Periodical"], help="Categories to process")
    parser.add_argument("--workers", type=int, default=3, help="Number of parallel workers (passed to downloader/uploader)")
    parser.add_argument("--max-size-mb", type=float, default=None,
                        help="Optional upload size cap; omitted means upload all downloaded items")
    parser.add_argument("--restore-state", action="store_true")
    parser.add_argument("--download", action="store_true", help="Download and immediately upload items")
    parser.add_argument("--start-uploads", action="store_true", help="Start background upload processes for residual items")
    args = parser.parse_args()

    if args.restore_state:
        restore_state(Path(args.state_dir))
    if args.download:
        download_categories(args.cat, args.workers)
    if args.start_uploads:
        # After immediate uploads from downloader, run a fresh pass to catch any items
        # that failed during the download+upload pipeline (e.g. transient errors)
        log.info("Starting safety-net upload pass for residual items")
        start_uploads(args.cat, args.workers, args.max_size_mb)
    if not (args.restore_state or args.download or args.start_uploads):
        parser.print_help()


if __name__ == "__main__":
    main()