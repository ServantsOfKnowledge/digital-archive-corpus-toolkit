#!/usr/bin/env python3
"""
Rebuild upload_progress.json by querying Internet Archive for all existing items.

This script queries the TamilVirtualAcademy collection on IA and builds a local
progress file to enable the harvester to skip already-uploaded items.

Usage:
    python3 rebuild_progress_from_ia.py
    python3 rebuild_progress_from_ia.py --verify  # verify counts match
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

try:
    import requests
except ImportError:
    print("ERROR: requests library required. Install with: pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / "upload_progress.json"
COLLECTION = "TamilVirtualAcademy"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"

# Expected categories (from tdl_multi_worker.py)
CATEGORIES = [
    "Audio", "AuthorBio", "Book", "Coin", "CopperPlate", "Document",
    "Excavation", "HistoricalMonument", "Inscription", "Map", "Painting",
    "Palmleaf", "Periodical", "Photograph", "PreHistoric",
    "ReligiousPlace", "Sculpture", "Video",
]


def fetch_all_ia_identifiers(collection: str, batch_size: int = 10000) -> List[str]:
    """
    Fetch all item identifiers from an IA collection using pagination.

    Args:
        collection: IA collection name
        batch_size: Number of items to fetch per request (max 10000)

    Returns:
        List of item identifiers
    """
    all_identifiers = []
    page = 1

    # First, get total count
    log.info(f"Querying Internet Archive collection: {collection}")
    params = {
        "q": f"collection:{collection}",
        "fl": "identifier",
        "rows": 0,
        "output": "json",
    }

    try:
        response = requests.get(IA_SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        total = data["response"]["numFound"]
        log.info(f"Total items in collection: {total:,}")
    except Exception as e:
        log.error(f"Failed to get total count: {e}")
        return []

    if total == 0:
        log.warning("No items found in collection!")
        return []

    # Fetch all identifiers in batches using 'start' parameter instead of 'page'
    total_pages = (total + batch_size - 1) // batch_size

    for page in range(1, total_pages + 1):
        start = (page - 1) * batch_size

        log.info(f"Fetching batch {page}/{total_pages} (items {start:,}-{min(start + batch_size, total):,})")

        params = {
            "q": f"collection:{collection}",
            "fl": "identifier",
            "rows": batch_size,
            "start": start,  # Use 'start' instead of 'page'
            "output": "json",
        }

        retry_count = 0
        max_retries = 3

        while retry_count < max_retries:
            try:
                response = requests.get(IA_SEARCH_URL, params=params, timeout=60)
                response.raise_for_status()
                data = response.json()

                # Check if response has expected structure
                if "response" not in data or "docs" not in data["response"]:
                    log.warning(f"Unexpected response structure on page {page}, retrying...")
                    retry_count += 1
                    time.sleep(2)
                    continue

                items = data["response"]["docs"]
                identifiers = [item["identifier"] for item in items if "identifier" in item]

                if not identifiers:
                    log.warning(f"No identifiers in batch {page}, may have reached end")
                    break

                all_identifiers.extend(identifiers)
                log.info(f"  Retrieved {len(identifiers)} identifiers (total so far: {len(all_identifiers):,})")

                # Rate limiting: be nice to IA servers
                time.sleep(1.5)
                break  # Success, exit retry loop

            except requests.exceptions.RequestException as e:
                retry_count += 1
                log.warning(f"Network error on batch {page}, attempt {retry_count}/{max_retries}: {e}")
                if retry_count < max_retries:
                    time.sleep(5)
                else:
                    log.error(f"Failed to fetch batch {page} after {max_retries} attempts")
                    break
            except Exception as e:
                log.error(f"Unexpected error on batch {page}: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(5)
                else:
                    break

        # If we failed all retries, ask user if they want to continue
        if retry_count >= max_retries:
            log.error(f"Failed to fetch batch {page} after multiple attempts")
            log.warning("Continuing with partial results...")
            break

    log.info(f"Successfully retrieved {len(all_identifiers):,} identifiers")
    return all_identifiers


def categorize_identifiers(identifiers: List[str]) -> Dict[str, int]:
    """
    Categorize identifiers based on folder structure or metadata hints.

    TDL identifiers follow patterns like:
    - tdl.{article_id}-{title_slug}
    - tdl.tdl_{hash}-{title_slug}

    We'll need to infer categories from the identifier structure or look at
    the local corpus to match identifiers to categories.
    """
    # Try to match identifiers to local folders if available
    corpus_dir = ROOT / "tdl_corpus"
    category_counts: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    uncategorized = []

    if not corpus_dir.exists():
        log.warning(f"Corpus directory not found: {corpus_dir}")
        log.warning("Cannot categorize identifiers without local corpus structure")
        return {}

    # Build a mapping of identifiers to categories from local corpus
    log.info("Scanning local corpus to build identifier-category mapping...")
    id_to_category: Dict[str, str] = {}

    for category in CATEGORIES:
        cat_dir = corpus_dir / category
        if not cat_dir.exists():
            continue

        # Read metadata.json from each folder to get identifiers
        folder_count = 0
        for folder in cat_dir.iterdir():
            if not folder.is_dir():
                continue

            meta_file = folder / "metadata.json"
            if not meta_file.exists():
                continue

            try:
                meta = json.loads(meta_file.read_text())
                art_id = meta.get("identifier", "")

                # Try to construct the IA identifier using the same logic as tdl_upload.py
                from tdl_upload import make_identifier
                ia_id = make_identifier(category, folder)

                if ia_id:
                    id_to_category[ia_id] = category
                    folder_count += 1

            except Exception as e:
                continue

        log.info(f"  {category}: mapped {folder_count} identifiers")

    log.info(f"Total mapped identifiers from corpus: {len(id_to_category):,}")

    # Categorize the IA identifiers
    for identifier in identifiers:
        if identifier in id_to_category:
            category = id_to_category[identifier]
            category_counts[category] += 1
        else:
            uncategorized.append(identifier)

    categorized_total = sum(category_counts.values())
    log.info(f"Categorized: {categorized_total:,} items")
    log.info(f"Uncategorized: {len(uncategorized):,} items")

    if uncategorized:
        log.warning(f"Found {len(uncategorized):,} identifiers not in local corpus")
        log.warning("These may be from old harvests or uploaded via different tools")
        # Sample some uncategorized IDs
        log.info(f"Sample uncategorized IDs: {uncategorized[:10]}")

    return category_counts


def build_progress_file(identifiers: List[str], output_file: Path, include_uncategorized: bool = True):
    """
    Build upload_progress.json from IA identifiers.

    Args:
        identifiers: List of IA identifiers
        output_file: Path to write progress file
        include_uncategorized: If True, add uncategorized items to uploaded list
    """
    log.info("Building progress file structure...")

    # Categorize identifiers
    category_counts = categorize_identifiers(identifiers)

    # Build progress structure
    progress = {
        "uploaded": identifiers if include_uncategorized else [],
        "uploaded_by_cat": category_counts,
        "failed": [],
        "rebuild_info": {
            "rebuilt_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_items": len(identifiers),
            "source": f"IA collection: {COLLECTION}",
        }
    }

    # If not including uncategorized, only add categorized items to uploaded list
    if not include_uncategorized and category_counts:
        corpus_dir = ROOT / "tdl_corpus"
        categorized_ids = set()

        for category in CATEGORIES:
            cat_dir = corpus_dir / category
            if not cat_dir.exists():
                continue

            for folder in cat_dir.iterdir():
                if not folder.is_dir():
                    continue

                meta_file = folder / "metadata.json"
                if not meta_file.exists():
                    continue

                try:
                    from tdl_upload import make_identifier
                    ia_id = make_identifier(category, folder)
                    if ia_id and ia_id in identifiers:
                        categorized_ids.add(ia_id)
                except:
                    continue

        progress["uploaded"] = sorted(categorized_ids)

    # Write progress file
    log.info(f"Writing progress file: {output_file}")
    tmp_file = output_file.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(progress, indent=2))
    tmp_file.replace(output_file)

    log.info("Progress file created successfully!")
    log.info(f"  Total uploaded items: {len(progress['uploaded']):,}")
    log.info(f"  Items by category:")
    for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            log.info(f"    {cat:20s}: {count:6,}")


def verify_progress_file(progress_file: Path):
    """Verify the progress file is valid and show statistics."""
    if not progress_file.exists():
        log.error(f"Progress file not found: {progress_file}")
        return False

    try:
        progress = json.loads(progress_file.read_text())

        uploaded = progress.get("uploaded", [])
        by_cat = progress.get("uploaded_by_cat", {})
        failed = progress.get("failed", [])

        log.info("=" * 80)
        log.info("PROGRESS FILE VERIFICATION")
        log.info("=" * 80)
        log.info(f"File: {progress_file}")
        log.info(f"Total uploaded items: {len(uploaded):,}")
        log.info(f"Failed items: {len(failed):,}")
        log.info("")
        log.info("Items by category:")

        total_by_cat = sum(by_cat.values())
        for cat, count in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
            if count > 0:
                pct = (count / total_by_cat * 100) if total_by_cat > 0 else 0
                log.info(f"  {cat:20s}: {count:6,} ({pct:5.1f}%)")

        log.info("")
        log.info(f"Total categorized: {total_by_cat:,}")
        log.info(f"Uncategorized: {len(uploaded) - total_by_cat:,}")
        log.info("=" * 80)

        if "rebuild_info" in progress:
            log.info("")
            log.info("Rebuild info:")
            for key, value in progress["rebuild_info"].items():
                log.info(f"  {key}: {value}")

        return True

    except Exception as e:
        log.error(f"Failed to verify progress file: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Verify existing progress file instead of rebuilding"
    )
    parser.add_argument(
        "--collection", default=COLLECTION,
        help=f"IA collection to query (default: {COLLECTION})"
    )
    parser.add_argument(
        "--output", type=Path, default=PROGRESS_FILE,
        help=f"Output file path (default: {PROGRESS_FILE})"
    )
    parser.add_argument(
        "--categorized-only", action="store_true",
        help="Only include items that can be matched to local corpus categories"
    )
    parser.add_argument(
        "--batch-size", type=int, default=10000,
        help="Items to fetch per IA query (default: 10000, max: 10000)"
    )

    args = parser.parse_args()

    # Verify mode
    if args.verify:
        success = verify_progress_file(args.output)
        sys.exit(0 if success else 1)

    # Rebuild mode
    log.info("=" * 80)
    log.info("REBUILDING UPLOAD PROGRESS FROM INTERNET ARCHIVE")
    log.info("=" * 80)
    log.info(f"Collection: {args.collection}")
    log.info(f"Output: {args.output}")
    log.info("")

    # Backup existing progress file if it exists
    if args.output.exists():
        backup = args.output.with_suffix(f".backup.{int(time.time())}.json")
        log.info(f"Backing up existing progress file to: {backup}")
        args.output.rename(backup)

    # Fetch all identifiers from IA
    identifiers = fetch_all_ia_identifiers(args.collection, args.batch_size)

    if not identifiers:
        log.error("No identifiers retrieved from IA!")
        sys.exit(1)

    # Build progress file
    build_progress_file(
        identifiers,
        args.output,
        include_uncategorized=not args.categorized_only
    )

    log.info("")
    log.info("=" * 80)
    log.info("REBUILD COMPLETE")
    log.info("=" * 80)

    # Verify the new file
    verify_progress_file(args.output)


if __name__ == "__main__":
    main()
