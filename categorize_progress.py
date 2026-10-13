#!/usr/bin/env python3
"""
Categorize items in upload_progress.json by querying IA metadata.

This populates the uploaded_by_cat field by sampling IA items to determine
which category they belong to based on their 'subject' metadata.

Usage:
    python3 categorize_progress.py --sample 1000  # Sample 1000 items
    python3 categorize_progress.py --full         # Categorize all (slow!)
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, Set

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / "upload_progress.json"

CATEGORIES = [
    "Audio", "AuthorBio", "Book", "Coin", "CopperPlate", "Document",
    "Excavation", "HistoricalMonument", "Inscription", "Map", "Painting",
    "Palmleaf", "Periodical", "Photograph", "PreHistoric",
    "ReligiousPlace", "Sculpture", "Video",
]


def get_item_category(identifier: str) -> str:
    """Query IA metadata to determine item category."""
    try:
        result = subprocess.run(
            ["ia", "metadata", identifier],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            data = json.loads(result.stdout)
            subjects = data.get("metadata", {}).get("subject", [])

            if isinstance(subjects, str):
                subjects = [subjects]

            # Find first matching category
            for subject in subjects:
                if subject in CATEGORIES:
                    return subject

    except Exception:
        pass

    return "Unknown"


def categorize_items(sample_size: int = None):
    """Categorize items in progress file."""
    if not PROGRESS_FILE.exists():
        print("Error: upload_progress.json not found")
        return

    # Load progress
    progress = json.loads(PROGRESS_FILE.read_text())
    uploaded = progress.get("uploaded", [])

    if not uploaded:
        print("No uploaded items found in progress file")
        return

    print(f"Total items to categorize: {len(uploaded):,}")

    # Determine sample
    if sample_size and sample_size < len(uploaded):
        print(f"Sampling {sample_size:,} items for categorization...")
        step = len(uploaded) // sample_size
        sample = uploaded[::step][:sample_size]
    else:
        print("Categorizing all items (this will take a while)...")
        sample = uploaded

    # Categorize samples
    category_counts = {cat: 0 for cat in CATEGORIES}
    category_counts["Unknown"] = 0

    print(f"Processing {len(sample):,} items...")

    for idx, identifier in enumerate(sample, 1):
        if idx % 10 == 0:
            print(f"  Progress: {idx}/{len(sample)} ({idx/len(sample)*100:.1f}%)", end="\r")

        category = get_item_category(identifier)
        category_counts[category] += 1

        time.sleep(0.2)  # Rate limiting

    print(f"\n\nSample results:")
    for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            pct = count / len(sample) * 100
            print(f"  {cat:20s}: {count:6,} ({pct:5.1f}%)")

    # Scale up to full count if sampled
    if sample_size and sample_size < len(uploaded):
        scale_factor = len(uploaded) / len(sample)
        print(f"\nEstimated full counts (scaled by {scale_factor:.1f}x):")

        for cat in category_counts:
            category_counts[cat] = int(category_counts[cat] * scale_factor)

    # Remove Unknown from final counts
    if "Unknown" in category_counts:
        del category_counts["Unknown"]

    # Update progress file
    progress["uploaded_by_cat"] = category_counts

    # Backup and save
    backup = PROGRESS_FILE.with_suffix(f".backup.{int(time.time())}.json")
    print(f"\nBacking up to: {backup}")
    PROGRESS_FILE.rename(backup)

    print(f"Saving updated progress file...")
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2, ensure_ascii=False))

    print("\n✓ Categorization complete!")
    print(f"\nFinal category breakdown:")
    for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            print(f"  {cat:20s}: {count:6,}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-s", "--sample", type=int, default=500,
        help="Number of items to sample (default: 500, faster but less accurate)"
    )
    parser.add_argument(
        "-f", "--full", action="store_true",
        help="Categorize all items (slow but accurate, ~30s per 100 items)"
    )

    args = parser.parse_args()

    sample_size = None if args.full else args.sample

    if args.full:
        response = input(f"This will query IA for all 97k+ items and take several hours. Continue? [y/N]: ")
        if response.lower() != 'y':
            print("Cancelled.")
            return

    categorize_items(sample_size=sample_size)


if __name__ == "__main__":
    main()
