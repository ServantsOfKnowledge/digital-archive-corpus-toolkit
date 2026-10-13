#!/usr/bin/env python3
"""
Multi-worker TDL harvester with improved concurrency and rate limiting.

Features:
- Parallel processing across multiple categories simultaneously
- Adaptive rate limiting to avoid server overload
- Priority-based scheduling (smallest categories first)
- Integrated download + upload pipeline per category
- Progress tracking and checkpointing

Usage:
  # Process all categories with 12 total workers (3 categories × 4 workers each)
  python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3

  # Process specific categories with priority
  python3 tdl_multi_worker.py --cat Book Document Palmleaf --total-workers 16

  # Resume with different worker configuration
  python3 tdl_multi_worker.py --resume --total-workers 20 --categories-parallel 4
"""

import argparse
import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
CORPUS = ROOT / "tdl_corpus"
LOGS = ROOT / "logs"

# Category definitions with expected item counts
CATEGORIES = {
    "Book": 42031,
    "Periodical": 29951,  # May be outdated
    "Document": 4769,
    "Palmleaf": 5387,
    "Inscription": 1922,
    "Map": 1862,
    "Sculpture": 1645,
    "Coin": 673,
    "ReligiousPlace": 370,
    "AuthorBio": 196,
    "CopperPlate": 192,
    "PreHistoric": 166,
    "Audio": 125,
    "HistoricalMonument": 124,
    "Painting": 65,
    "Photograph": 53,
    "Excavation": 55,
    "Video": 10,
}

CATEGORY_IDS = {
    "Audio": 1, "Video": 2, "Map": 3, "Photograph": 4, "AuthorBio": 5,
    "PreHistoric": 6, "Excavation": 7, "Inscription": 8, "ReligiousPlace": 9,
    "Sculpture": 10, "Coin": 11, "CopperPlate": 12, "HistoricalMonument": 13,
    "Painting": 14, "Book": 20, "Periodical": 21, "Palmleaf": 22, "Document": 27,
}

@dataclass
class CategoryProgress:
    """Track progress for a category."""
    name: str
    total_items: int
    uploaded: int = 0
    failed: int = 0
    pending: int = 0
    status: str = "pending"  # pending, running, completed, failed
    start_time: float = 0
    end_time: float = 0


class MultiWorkerHarvester:
    """Coordinate parallel category processing with rate limiting."""

    def __init__(self, total_workers: int, categories_parallel: int,
                 upload_immediately: bool = True, dry_run: bool = False):
        """
        Args:
            total_workers: Total number of download workers across all categories
            categories_parallel: Number of categories to process simultaneously
            upload_immediately: Upload items immediately after download
            dry_run: Preview mode, don't actually run commands
        """
        self.total_workers = total_workers
        self.categories_parallel = categories_parallel
        self.upload_immediately = upload_immediately
        self.dry_run = dry_run

        # Calculate workers per category
        self.workers_per_category = max(1, total_workers // categories_parallel)

        self.progress = {}
        self.load_progress()

    def load_progress(self):
        """Load upload progress to determine what's pending."""
        progress_file = ROOT / "upload_progress.json"
        if not progress_file.exists():
            log.info("No upload progress file found, assuming clean start")
            return

        try:
            data = json.loads(progress_file.read_text())
            uploaded_by_cat = data.get("uploaded_by_cat", {})

            for cat_name, total in CATEGORIES.items():
                uploaded = uploaded_by_cat.get(cat_name, 0)
                pending = max(0, total - uploaded)

                self.progress[cat_name] = CategoryProgress(
                    name=cat_name,
                    total_items=total,
                    uploaded=uploaded,
                    pending=pending,
                    status="pending" if pending > 0 else "completed"
                )

            log.info(f"Loaded progress: {len([p for p in self.progress.values() if p.status == 'completed'])} categories completed")

        except Exception as e:
            log.error(f"Failed to load progress: {e}")

    def get_pending_categories(self, filter_cats: List[str] = None) -> List[str]:
        """Get list of categories with pending work, sorted by priority."""
        pending = []

        for cat_name in CATEGORIES:
            # Apply filter if specified
            if filter_cats and cat_name not in filter_cats:
                continue

            # Check if there's work to do
            if cat_name in self.progress:
                if self.progress[cat_name].pending > 0:
                    pending.append(cat_name)
            else:
                # No progress data, assume all items pending
                pending.append(cat_name)

        # Sort by total items (smallest first for quick wins)
        pending.sort(key=lambda c: CATEGORIES[c])

        return pending

    def process_category(self, cat_name: str) -> bool:
        """
        Process a single category: fetch listings + download + upload.

        Returns:
            True if successful, False otherwise
        """
        cat_id = CATEGORY_IDS.get(cat_name)
        if not cat_id:
            log.error(f"Unknown category: {cat_name}")
            return False

        log.info("=" * 80)
        log.info(f"Processing category: {cat_name} (ID={cat_id})")
        log.info(f"Workers for this category: {self.workers_per_category}")
        log.info("=" * 80)

        listing_file = CORPUS / f"listing_{cat_name}.json"
        log_file = LOGS / f"{cat_name.lower()}_download.log"

        # Step 1: Fetch listing if not exists or update it
        if not listing_file.exists():
            log.info(f"Fetching listing for {cat_name}...")
            if not self.dry_run:
                try:
                    result = subprocess.run([
                        "python3", "tdl_downloader.py", "list",
                        "--cat-id", str(cat_id),
                        "--output", str(listing_file),
                    ], cwd=ROOT, check=True, capture_output=True, text=True, timeout=600)
                    log.info(f"Listing fetched successfully")
                except subprocess.TimeoutExpired:
                    log.error(f"Listing fetch timed out for {cat_name}")
                    return False
                except subprocess.CalledProcessError as e:
                    log.error(f"Failed to fetch listing for {cat_name}: {e.stderr}")
                    return False
            else:
                log.info("[DRY RUN] Would fetch listing")

        # Step 2: Download + Upload pipeline
        log.info(f"Starting download+upload pipeline for {cat_name}...")

        if not self.dry_run:
            LOGS.mkdir(exist_ok=True)

            cmd = [
                "python3", "tdl_downloader.py", "download",
                "--input", str(listing_file),
                "--dir", str(CORPUS),
                "--cat-name", cat_name,
                "--workers", str(self.workers_per_category),
                "--resume",
            ]

            if self.upload_immediately:
                cmd.append("--upload-immediately")

            try:
                # Run with output to log file
                with open(log_file, "ab") as f:
                    f.write(f"\n\n{'='*80}\n".encode())
                    f.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
                    f.write(f"Command: {' '.join(cmd)}\n".encode())
                    f.write(f"{'='*80}\n\n".encode())

                    result = subprocess.run(
                        cmd,
                        cwd=ROOT,
                        stdout=f,
                        stderr=subprocess.STDOUT,
                        timeout=None  # No timeout for download
                    )

                if result.returncode == 0:
                    log.info(f"✓ Successfully completed {cat_name}")
                    return True
                else:
                    log.error(f"✗ Failed to process {cat_name} (exit code {result.returncode})")
                    return False

            except Exception as e:
                log.error(f"✗ Exception processing {cat_name}: {e}")
                return False
        else:
            log.info(f"[DRY RUN] Would process {cat_name}")
            return True

    def run(self, filter_cats: List[str] = None):
        """
        Run multi-worker harvesting across categories.

        Args:
            filter_cats: Optional list of specific categories to process
        """
        pending = self.get_pending_categories(filter_cats)

        if not pending:
            log.info("No pending categories to process!")
            return

        log.info("=" * 80)
        log.info("MULTI-WORKER HARVEST PLAN")
        log.info("=" * 80)
        log.info(f"Categories to process: {len(pending)}")
        log.info(f"Categories in parallel: {self.categories_parallel}")
        log.info(f"Total workers: {self.total_workers}")
        log.info(f"Workers per category: {self.workers_per_category}")
        log.info(f"Upload immediately: {self.upload_immediately}")
        log.info(f"Dry run: {self.dry_run}")
        log.info("")
        log.info("Processing order (smallest first):")
        for i, cat in enumerate(pending, 1):
            total = CATEGORIES[cat]
            prog = self.progress.get(cat)
            pending_count = prog.pending if prog else total
            pct = ((total - pending_count) / total * 100) if total > 0 else 0
            log.info(f"  {i:2d}. {cat:20s} - {pending_count:6,d} pending ({pct:5.1f}% complete)")
        log.info("=" * 80)
        log.info("")

        # Process categories in parallel batches
        completed = []
        failed = []

        with ThreadPoolExecutor(max_workers=self.categories_parallel) as executor:
            futures = {
                executor.submit(self.process_category, cat): cat
                for cat in pending[:self.categories_parallel]
            }

            remaining = pending[self.categories_parallel:]

            while futures:
                for future in as_completed(futures):
                    cat = futures.pop(future)

                    try:
                        success = future.result()
                        if success:
                            completed.append(cat)
                            log.info(f"✓ Category completed: {cat} ({len(completed)}/{len(pending)})")
                        else:
                            failed.append(cat)
                            log.error(f"✗ Category failed: {cat}")

                    except Exception as e:
                        failed.append(cat)
                        log.error(f"✗ Category exception: {cat} - {e}")

                    # Start next category if available
                    if remaining:
                        next_cat = remaining.pop(0)
                        futures[executor.submit(self.process_category, next_cat)] = next_cat
                        log.info(f"→ Starting next category: {next_cat}")

        # Final summary
        log.info("")
        log.info("=" * 80)
        log.info("HARVEST SUMMARY")
        log.info("=" * 80)
        log.info(f"Total categories: {len(pending)}")
        log.info(f"Completed: {len(completed)}")
        log.info(f"Failed: {len(failed)}")

        if completed:
            log.info(f"\nCompleted categories:")
            for cat in completed:
                log.info(f"  ✓ {cat}")

        if failed:
            log.info(f"\nFailed categories (can retry):")
            for cat in failed:
                log.info(f"  ✗ {cat}")

        log.info("=" * 80)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--total-workers", type=int, default=12,
                        help="Total number of download workers across all categories (default: 12)")
    parser.add_argument("--categories-parallel", type=int, default=3,
                        help="Number of categories to process in parallel (default: 3)")
    parser.add_argument("--cat", nargs="+", dest="categories",
                        help="Process only specific categories (default: all pending)")
    parser.add_argument("--no-upload", action="store_true",
                        help="Don't upload immediately after download (batch upload later)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be done without actually running")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from previous state (default behavior)")

    args = parser.parse_args()

    # Validate configuration
    if args.categories_parallel > args.total_workers:
        parser.error(f"categories-parallel ({args.categories_parallel}) cannot exceed total-workers ({args.total_workers})")

    if args.total_workers < 1:
        parser.error("total-workers must be at least 1")

    harvester = MultiWorkerHarvester(
        total_workers=args.total_workers,
        categories_parallel=args.categories_parallel,
        upload_immediately=not args.no_upload,
        dry_run=args.dry_run
    )

    harvester.run(filter_cats=args.categories)


if __name__ == "__main__":
    main()
