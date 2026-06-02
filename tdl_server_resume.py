#!/usr/bin/env python3
"""Prepare a server checkout to redownload corpus items and resume uploads."""

import argparse
import shutil
import subprocess
from pathlib import Path


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
    for cat in categories:
        listing = CORPUS / f"listing_{cat}.json"
        if not listing.exists():
            raise SystemExit(f"Missing listing file: {listing}")
        run([
            "python3", "tdl_downloader.py", "download",
            "--input", str(listing),
            "--dir", str(CORPUS),
            "--cat-name", cat,
            "--workers", str(workers),
            "--resume",
            "--upload-immediately",
        ])


def start_uploads(categories, workers, max_size_mb):
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
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-size-mb", type=float, default=None,
                        help="Optional upload size cap; omitted means upload all downloaded items")
    parser.add_argument("--restore-state", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start-uploads", action="store_true")
    args = parser.parse_args()

    if args.restore_state:
        restore_state(Path(args.state_dir))
    if args.download:
        download_categories(args.cat, args.workers)
    if args.start_uploads:
        # After immediate uploads from downloader, run a fresh pass to catch any items
        # that failed during the download+upload pipeline (e.g. transient errors)
        start_uploads(args.cat, args.workers, args.max_size_mb)
    if not (args.restore_state or args.download or args.start_uploads):
        parser.print_help()


if __name__ == "__main__":
    main()
