#!/usr/bin/env python3
"""Analyze TDL harvesting state and identify pending work."""

import json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent
HANDOFF = ROOT.parent / "tmvu-corpus-server-handoff"

def load_json(path):
    """Load JSON file, return empty dict/list if not found."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []

def analyze():
    print("=" * 80)
    print("TDL HARVESTING STATE ANALYSIS")
    print("=" * 80)

    # Load state files
    handoff_state = HANDOFF / "server_state"

    # Check upload progress from both locations
    local_data = load_json(ROOT / "upload_progress.json")
    handoff_data = load_json(handoff_state / "upload_progress.json")

    # Handle both dict format {"uploaded": [...]} and plain list format
    if isinstance(local_data, dict):
        local_uploaded = set(local_data.get("uploaded", []))
        local_by_cat = local_data.get("uploaded_by_cat", {})
    else:
        local_uploaded = set(local_data) if local_data else set()
        local_by_cat = {}

    if isinstance(handoff_data, dict):
        handoff_uploaded = set(handoff_data.get("uploaded", []))
        handoff_by_cat = handoff_data.get("uploaded_by_cat", {})
    else:
        handoff_uploaded = set(handoff_data) if handoff_data else set()
        handoff_by_cat = {}

    # Merge category counts
    uploaded_by_cat = {}
    for cat, count in handoff_by_cat.items():
        uploaded_by_cat[cat] = count
    for cat, count in local_by_cat.items():
        uploaded_by_cat[cat] = uploaded_by_cat.get(cat, 0) + count

    all_uploaded = local_uploaded | handoff_uploaded

    print(f"\nUploaded items:")
    print(f"  Local:   {len(local_uploaded):,}")
    print(f"  Handoff: {len(handoff_uploaded):,}")
    print(f"  Total:   {len(all_uploaded):,}")

    # Check failed uploads
    local_failed = load_json(ROOT / "upload_failed.json")
    handoff_failed = load_json(handoff_state / "upload_failed.json")
    print(f"\nFailed upload records:")
    print(f"  Local:   {len(local_failed):,}")
    print(f"  Handoff: {len(handoff_failed):,}")

    # Analyze each category
    print("\n" + "=" * 80)
    print("CATEGORY ANALYSIS")
    print("=" * 80)

    category_stats = []

    for listing_file in sorted(handoff_state.glob("listing_*.json")):
        if listing_file.name.startswith("._"):
            continue

        cat_name = listing_file.stem.replace("listing_", "")
        listing_data = load_json(listing_file)

        if not listing_data:
            continue

        # Handle dict format {"articles": [...]} and plain list format
        if isinstance(listing_data, dict):
            items = listing_data.get("articles", [])
        else:
            items = listing_data

        if not items:
            continue

        total = len(items)

        # Get uploaded count from uploaded_by_cat
        uploaded_count = uploaded_by_cat.get(cat_name, 0)
        pending = total - uploaded_count
        pct = (uploaded_count / total * 100) if total > 0 else 0

        category_stats.append({
            "name": cat_name,
            "total": total,
            "uploaded": uploaded_count,
            "pending": pending,
            "pct": pct
        })

    # Print summary table
    print(f"\n{'Category':<20} {'Total':>10} {'Uploaded':>10} {'Pending':>10} {'Progress':>10}")
    print("-" * 80)

    total_items = 0
    total_uploaded = 0
    total_pending = 0

    for stat in sorted(category_stats, key=lambda x: x["pending"], reverse=True):
        print(f"{stat['name']:<20} {stat['total']:>10,} {stat['uploaded']:>10,} "
              f"{stat['pending']:>10,} {stat['pct']:>9.1f}%")
        total_items += stat['total']
        total_uploaded += stat['uploaded']
        total_pending += stat['pending']

    print("-" * 80)
    overall_pct = (total_uploaded / total_items * 100) if total_items > 0 else 0
    print(f"{'TOTAL':<20} {total_items:>10,} {total_uploaded:>10,} "
          f"{total_pending:>10,} {overall_pct:>9.1f}%")

    print("\n" + "=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)
    print("\nCategories with most pending work (prioritized):")
    for i, stat in enumerate(sorted(category_stats, key=lambda x: x["pending"], reverse=True)[:10], 1):
        if stat["pending"] > 0:
            print(f"  {i}. {stat['name']}: {stat['pending']:,} items ({stat['pct']:.1f}% complete)")

    # Check for PDF redownload queue
    pdf_redownload = load_json(ROOT / "pdf_redownload.json") or load_json(handoff_state / "pdf_redownload.json")
    if pdf_redownload:
        print(f"\nPDF recovery queue: {len(pdf_redownload):,} items need reprocessing")

    print("\n" + "=" * 80)

if __name__ == "__main__":
    analyze()
