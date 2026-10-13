#!/usr/bin/env python3
"""
Real-time harvest monitoring script.

Monitors upload_progress.json and shows:
- Total items uploaded
- Uploads by category
- Upload rate (items/hour)
- Recent uploads
- Progress toward completion

Usage:
    python3 monitor_harvest.py              # One-time snapshot
    python3 monitor_harvest.py --watch      # Live monitoring (updates every 5s)
    python3 monitor_harvest.py --compact    # Compact view
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

# Expected totals per category (from tdl_multi_worker.py)
CATEGORY_TOTALS = {
    "Book": 42031,
    "Periodical": 29951,
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

TOTAL_EXPECTED = sum(CATEGORY_TOTALS.values())  # 89,596

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / "upload_progress.json"
STATE_FILE = ROOT / ".monitor_state.json"


class HarvestMonitor:
    def __init__(self):
        self.last_count = 0
        self.last_check_time = None
        self.start_time = datetime.now()
        self.upload_rate_history = []

        # Load previous state if exists
        self.load_state()

    def load_state(self):
        """Load previous monitoring state to track rate over time."""
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                self.start_time = datetime.fromisoformat(state.get("start_time", datetime.now().isoformat()))
                self.last_count = state.get("last_count", 0)
                if state.get("last_check_time"):
                    self.last_check_time = datetime.fromisoformat(state["last_check_time"])
            except:
                pass

    def save_state(self, current_count: int):
        """Save current state for next run."""
        state = {
            "start_time": self.start_time.isoformat(),
            "last_count": current_count,
            "last_check_time": datetime.now().isoformat(),
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def read_progress(self) -> Tuple[Dict, int, int]:
        """
        Read progress file and return (progress_dict, total_uploaded, failed_count).
        """
        if not PROGRESS_FILE.exists():
            return {}, 0, 0

        try:
            progress = json.loads(PROGRESS_FILE.read_text())
            uploaded = progress.get("uploaded", [])
            failed = progress.get("failed", [])
            return progress, len(uploaded), len(failed)
        except Exception as e:
            print(f"Error reading progress file: {e}", file=sys.stderr)
            return {}, 0, 0

    def calculate_rate(self, current_count: int) -> float:
        """Calculate upload rate in items/hour."""
        now = datetime.now()

        if self.last_check_time and self.last_count > 0:
            time_diff = (now - self.last_check_time).total_seconds() / 3600  # hours
            if time_diff > 0:
                count_diff = current_count - self.last_count
                return count_diff / time_diff

        return 0.0

    def format_eta(self, remaining: int, rate: float) -> str:
        """Format estimated time to completion."""
        if rate <= 0:
            return "N/A"

        hours_remaining = remaining / rate

        if hours_remaining < 1:
            return f"{int(hours_remaining * 60)}m"
        elif hours_remaining < 24:
            return f"{int(hours_remaining)}h {int((hours_remaining % 1) * 60)}m"
        else:
            days = int(hours_remaining / 24)
            hours = int(hours_remaining % 24)
            return f"{days}d {hours}h"

    def print_snapshot(self, compact=False):
        """Print current status snapshot."""
        progress, total_uploaded, failed_count = self.read_progress()

        if total_uploaded == 0:
            print("No uploads recorded yet. Harvest may not have started.")
            return

        by_category = progress.get("uploaded_by_cat", {})

        # Calculate what's new vs pre-existing
        BASELINE_IA_COUNT = 97147  # Items that were already on IA when we started
        new_items = total_uploaded - BASELINE_IA_COUNT
        if new_items < 0:
            new_items = 0

        # Calculate rate
        rate = self.calculate_rate(total_uploaded)

        # Calculate progress
        remaining = TOTAL_EXPECTED - total_uploaded
        if remaining < 0:
            remaining = 0  # In case there are more items than expected

        pct_complete = (total_uploaded / TOTAL_EXPECTED * 100) if TOTAL_EXPECTED > 0 else 0

        # Update state
        self.last_count = total_uploaded
        self.last_check_time = datetime.now()
        self.save_state(total_uploaded)

        # Print header
        print("=" * 80)
        print(f"HARVEST MONITORING - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

        # Overall stats
        print(f"\nOVERALL PROGRESS:")
        print(f"  Already on IA:      {BASELINE_IA_COUNT:,} items (skipped)")
        print(f"  NEW items found:    {new_items:,} items ⭐")
        print(f"  Total tracked:      {total_uploaded:,} items")
        print(f"  Failed:             {failed_count:,} items")
        print(f"  Expected from TDL:  {TOTAL_EXPECTED:,} items ({pct_complete:.1f}% checked)")

        if rate > 0:
            print(f"  Upload rate:        {rate:.1f} items/hour ({rate/60:.2f} items/min)")
            eta = self.format_eta(remaining, rate)
            print(f"  Estimated time:     {eta}")

        # Progress bar
        if not compact:
            bar_width = 50
            filled = int(bar_width * pct_complete / 100)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"\n  [{bar}] {pct_complete:.1f}%")

        # Category breakdown
        print(f"\nCATEGORY BREAKDOWN:")

        # Sort categories by upload count (descending)
        sorted_cats = sorted(by_category.items(), key=lambda x: x[1], reverse=True)

        if compact:
            # Compact view - show only active categories
            for cat, count in sorted_cats:
                if count > 0:
                    expected = CATEGORY_TOTALS.get(cat, 0)
                    pct = (count / expected * 100) if expected > 0 else 0
                    print(f"  {cat:20s}: {count:6,} / {expected:6,} ({pct:5.1f}%)")
        else:
            # Full view - show all categories
            for cat, expected in sorted(CATEGORY_TOTALS.items(), key=lambda x: x[1], reverse=True):
                count = by_category.get(cat, 0)
                pct = (count / expected * 100) if expected > 0 else 0
                remaining_cat = expected - count

                # Progress bar for category
                bar_width = 20
                filled = int(bar_width * pct / 100)
                bar = "█" * filled + "░" * (bar_width - filled)

                status = "✓" if count >= expected else " "
                print(f"  {status} {cat:18s}: {count:6,} / {expected:6,} [{bar}] {pct:5.1f}% ({remaining_cat:,} left)")

        # Show recent uploads if available
        if not compact and "uploaded" in progress:
            recent = progress["uploaded"][-10:]
            if recent:
                print(f"\nRECENT UPLOADS (last 10):")
                for i, identifier in enumerate(reversed(recent), 1):
                    print(f"  {i:2d}. {identifier[:70]}")

        # Show failed items if any
        if failed_count > 0 and not compact:
            failed = progress.get("failed", [])
            print(f"\nFAILED ITEMS ({failed_count}):")
            for item in failed[:5]:
                cat = item.get("category", "Unknown")
                reason = item.get("reason", "Unknown")
                print(f"  • {cat}: {reason[:60]}")
            if failed_count > 5:
                print(f"  ... and {failed_count - 5} more")

        print("=" * 80)

    def watch(self, interval=5, compact=False):
        """Continuously monitor and update display."""
        print("Starting live monitoring (Ctrl+C to stop)...")
        print(f"Refresh interval: {interval} seconds")
        print()

        try:
            while True:
                # Clear screen (works on Unix-like systems)
                os.system('clear' if os.name == 'posix' else 'cls')

                self.print_snapshot(compact=compact)

                print(f"\nRefreshing in {interval} seconds... (Ctrl+C to stop)")
                time.sleep(interval)

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped.")
            sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-w", "--watch", action="store_true",
        help="Continuous monitoring mode (updates every 5 seconds)"
    )
    parser.add_argument(
        "-i", "--interval", type=int, default=5,
        help="Update interval in seconds for watch mode (default: 5)"
    )
    parser.add_argument(
        "-c", "--compact", action="store_true",
        help="Compact display (less detail, more concise)"
    )
    parser.add_argument(
        "--reset-state", action="store_true",
        help="Reset monitoring state (clears rate calculation history)"
    )

    args = parser.parse_args()

    # Reset state if requested
    if args.reset_state:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print("Monitoring state reset.")
        return

    monitor = HarvestMonitor()

    if args.watch:
        monitor.watch(interval=args.interval, compact=args.compact)
    else:
        monitor.print_snapshot(compact=args.compact)


if __name__ == "__main__":
    main()
