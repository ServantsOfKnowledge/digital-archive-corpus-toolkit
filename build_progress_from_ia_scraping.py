#!/usr/bin/env python3
"""
Build upload_progress.json by scraping IA collection using the scraping API.

This bypasses the 10,000-result deep paging limit by using the scraping endpoint.

Usage:
    python3 build_progress_from_ia_scraping.py
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / "upload_progress.json"
COLLECTION = "TamilVirtualAcademy"

# Expected categories
CATEGORIES = [
    "Audio", "AuthorBio", "Book", "Coin", "CopperPlate", "Document",
    "Excavation", "HistoricalMonument", "Inscription", "Map", "Painting",
    "Palmleaf", "Periodical", "Photograph", "PreHistoric",
    "ReligiousPlace", "Sculpture", "Video",
]


def fetch_all_via_ia_cli() -> List[str]:
    """
    Fetch all item identifiers using the IA CLI tool's search command.

    This is more reliable than the web API for large collections.
    """
    log.info(f"Fetching all items from {COLLECTION} using IA CLI...")
    log.info("This may take several minutes for 97k+ items...")

    try:
        # Use ia search with --itemlist to get just identifiers
        result = subprocess.run(
            ["ia", "search", f"collection:{COLLECTION}", "--itemlist"],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )

        if result.returncode != 0:
            log.error(f"IA CLI search failed: {result.stderr}")
            return []

        # Parse output - one identifier per line
        identifiers = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

        log.info(f"Successfully retrieved {len(identifiers):,} identifiers from IA")
        return identifiers

    except subprocess.TimeoutExpired:
        log.error("IA search timed out after 10 minutes")
        return []
    except FileNotFoundError:
        log.error("IA CLI not found. Install with: pip install internetarchive")
        return []
    except Exception as e:
        log.error(f"Error fetching from IA: {e}")
        return []


def categorize_by_subject(identifiers: List[str]) -> Dict[str, int]:
    """
    Categorize identifiers by querying IA metadata for subject tags.

    This is slow but accurate. We'll sample to build category stats.
    """
    log.info("Sampling items to estimate category distribution...")

    category_counts = {cat: 0 for cat in CATEGORIES}

    # Sample every 100th item for speed (about 971 samples for 97k items)
    sample_size = max(100, len(identifiers) // 100)
    sample_ids = identifiers[::sample_size]

    log.info(f"Sampling {len(sample_ids)} items...")

    for idx, identifier in enumerate(sample_ids, 1):
        if idx % 10 == 0:
            log.info(f"  Processed {idx}/{len(sample_ids)} samples...")

        try:
            result = subprocess.run(
                ["ia", "metadata", identifier, "--formats=json"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    subjects = data.get("metadata", {}).get("subject", [])

                    if isinstance(subjects, str):
                        subjects = [subjects]

                    # Match subjects to categories
                    for subject in subjects:
                        if subject in CATEGORIES:
                            category_counts[subject] += 1
                            break  # Count each item once

                except json.JSONDecodeError:
                    pass

            time.sleep(0.3)  # Rate limiting

        except Exception as e:
            log.warning(f"Error processing {identifier}: {e}")
            continue

    # Scale up from sample to full count
    total_sampled = sum(category_counts.values())
    if total_sampled > 0:
        scale_factor = len(identifiers) / len(sample_ids)
        for cat in category_counts:
            category_counts[cat] = int(category_counts[cat] * scale_factor)

    log.info("Category distribution (estimated):")
    for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            log.info(f"  {cat:20s}: {count:6,}")

    return category_counts


def build_progress_file(identifiers: List[str], output_file: Path):
    """
    Build upload_progress.json from IA identifiers.
    """
    log.info("Building progress file...")

    # For speed, just create a simple progress file without detailed categorization
    # The category counts can be estimated or left for the harvest to update
    progress = {
        "uploaded": sorted(identifiers),
        "uploaded_by_cat": {},  # Will be updated during harvest
        "failed": [],
        "rebuild_info": {
            "rebuilt_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_items": len(identifiers),
            "source": f"IA CLI scrape of collection: {COLLECTION}",
            "method": "ia search --itemlist",
        }
    }

    # Write progress file
    log.info(f"Writing progress file: {output_file}")
    tmp_file = output_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(progress, indent=2, ensure_ascii=False))
    tmp_file.replace(output_file)

    log.info("Progress file created successfully!")
    log.info(f"  Total uploaded items: {len(progress['uploaded']):,}")


def main():
    log.info("=" * 80)
    log.info("REBUILDING UPLOAD PROGRESS FROM INTERNET ARCHIVE")
    log.info("Using IA CLI scraping method")
    log.info("=" * 80)
    log.info(f"Collection: {COLLECTION}")
    log.info(f"Output: {PROGRESS_FILE}")
    log.info("")

    # Backup existing progress file if it exists
    if PROGRESS_FILE.exists():
        backup = PROGRESS_FILE.with_suffix(f".backup.{int(time.time())}.json")
        log.info(f"Backing up existing progress file to: {backup}")
        PROGRESS_FILE.rename(backup)

    # Fetch all identifiers
    identifiers = fetch_all_via_ia_cli()

    if not identifiers:
        log.error("No identifiers retrieved from IA!")
        return 1

    # Build progress file
    build_progress_file(identifiers, PROGRESS_FILE)

    log.info("")
    log.info("=" * 80)
    log.info("REBUILD COMPLETE")
    log.info("=" * 80)
    log.info(f"Total items tracked: {len(identifiers):,}")
    log.info("")
    log.info("The harvest will now skip these items during download.")
    log.info("Run the harvest with: python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3")

    return 0


if __name__ == "__main__":
    exit(main())
