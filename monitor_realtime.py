#!/usr/bin/env python3
"""
Real-time harvest activity monitor.

Watches harvest logs and upload_progress.json to show:
- Items being uploaded RIGHT NOW
- Upload rate (items/minute)
- Total progress
- Active categories

Usage:
    python3 monitor_realtime.py                # Monitor all categories
    python3 monitor_realtime.py --cat Book     # Monitor specific category
    python3 monitor_realtime.py --follow       # Follow mode (like tail -f)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).parent
PROGRESS_FILE = ROOT / "upload_progress.json"
LOGS_DIR = ROOT / "logs"
HARVEST_LOG = ROOT / "harvest.log"

# Expected totals
CATEGORY_TOTALS = {
    "Book": 42031, "Periodical": 29951, "Document": 4769, "Palmleaf": 5387,
    "Inscription": 1922, "Map": 1862, "Sculpture": 1645, "Coin": 673,
    "ReligiousPlace": 370, "AuthorBio": 196, "CopperPlate": 192,
    "PreHistoric": 166, "Audio": 125, "HistoricalMonument": 124,
    "Painting": 65, "Photograph": 53, "Excavation": 55, "Video": 10,
}


class RealtimeMonitor:
    def __init__(self):
        self.upload_history = deque(maxlen=100)  # Last 100 uploads
        self.start_time = datetime.now()
        self.last_progress_check = 0
        self.last_total = 0

    def get_current_progress(self) -> Dict:
        """Get current progress from file."""
        if not PROGRESS_FILE.exists():
            return {"uploaded": [], "uploaded_by_cat": {}, "failed": []}

        try:
            # Check if file was modified
            mtime = PROGRESS_FILE.stat().st_mtime
            if mtime != self.last_progress_check:
                self.last_progress_check = mtime
                return json.loads(PROGRESS_FILE.read_text())
            return None  # No change
        except Exception as e:
            return {"uploaded": [], "uploaded_by_cat": {}, "failed": []}

    def parse_log_line(self, line: str) -> Dict:
        """Parse log line to extract upload info."""
        # Look for upload success patterns
        patterns = [
            # Success patterns
            r"Successfully uploaded.*?(?:identifier|item)[:\s]+([^\s]+)",
            r"uploaded.*?to\s+IA[:\s]+([^\s]+)",
            r"Item\s+([^\s]+).*?uploaded",
            # Skip patterns
            r"Item\s+([^\s]+).*?already.*?(?:uploaded|on IA)",
            r"([^\s]+).*?skipping.*?already",
        ]

        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                identifier = match.group(1)
                is_skip = "already" in line.lower() or "skip" in line.lower()
                return {
                    "identifier": identifier,
                    "timestamp": datetime.now(),
                    "skipped": is_skip,
                    "line": line.strip()
                }

        return None

    def tail_logs(self, category: str = None, follow: bool = False):
        """Monitor logs in real-time."""
        print("=" * 80)
        print("REAL-TIME HARVEST MONITOR")
        print("=" * 80)
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()

        # Determine which logs to monitor
        if category:
            log_files = [LOGS_DIR / f"{category.lower()}_download.log"]
        else:
            log_files = list(LOGS_DIR.glob("*_download.log")) if LOGS_DIR.exists() else []

        if not log_files and HARVEST_LOG.exists():
            log_files = [HARVEST_LOG]

        if not log_files:
            print("No log files found. Start the harvest first:")
            print("  python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3")
            return

        print(f"Monitoring {len(log_files)} log file(s)...")
        for log in log_files:
            print(f"  • {log.name}")
        print()
        print("-" * 80)

        # Track stats
        new_uploads = 0
        skipped = 0
        start_time = time.time()

        try:
            # Use tail -f for each log file
            if follow:
                # Follow mode - continuous
                processes = []
                for log_file in log_files:
                    if log_file.exists():
                        proc = subprocess.Popen(
                            ["tail", "-f", str(log_file)],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            bufsize=1
                        )
                        processes.append(proc)

                # Read from all processes
                import select
                while True:
                    # Check for updates in progress file
                    progress = self.get_current_progress()
                    if progress:
                        current_total = len(progress.get("uploaded", []))
                        if current_total != self.last_total:
                            new_since_start = current_total - self.last_total
                            self.last_total = current_total

                            # Calculate rate
                            elapsed = time.time() - start_time
                            rate = (new_uploads / elapsed * 60) if elapsed > 0 else 0

                            # Show progress update
                            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Progress update: {current_total:,} total items | {new_uploads:,} new, {skipped:,} skipped | Rate: {rate:.1f}/min")

                    # Read log lines
                    for proc in processes:
                        if proc.poll() is None:  # Process still running
                            line = proc.stdout.readline()
                            if line:
                                parsed = self.parse_log_line(line)
                                if parsed:
                                    if parsed["skipped"]:
                                        skipped += 1
                                        print(f"⊙ SKIP: {parsed['identifier'][:60]}")
                                    else:
                                        new_uploads += 1
                                        print(f"✓ UPLOAD: {parsed['identifier'][:60]}")

                    time.sleep(0.1)

            else:
                # Snapshot mode - show recent activity
                recent_lines = []
                for log_file in log_files:
                    if log_file.exists():
                        result = subprocess.run(
                            ["tail", "-100", str(log_file)],
                            capture_output=True,
                            text=True
                        )
                        recent_lines.extend(result.stdout.strip().split("\n"))

                # Parse recent activity
                recent_uploads = []
                recent_skips = []

                for line in recent_lines:
                    parsed = self.parse_log_line(line)
                    if parsed:
                        if parsed["skipped"]:
                            recent_skips.append(parsed)
                        else:
                            recent_uploads.append(parsed)

                # Show summary
                print(f"\nRECENT ACTIVITY (last 100 log lines):")
                print(f"  New uploads:  {len(recent_uploads):,}")
                print(f"  Skipped:      {len(recent_skips):,}")

                if recent_uploads:
                    print(f"\nLast {min(10, len(recent_uploads))} uploads:")
                    for item in recent_uploads[-10:]:
                        print(f"  ✓ {item['identifier'][:70]}")

                if recent_skips:
                    print(f"\nLast {min(5, len(recent_skips))} skipped (already on IA):")
                    for item in recent_skips[-5:]:
                        print(f"  ⊙ {item['identifier'][:70]}")

                # Show current progress
                progress = self.get_current_progress()
                if progress:
                    total = len(progress.get("uploaded", []))
                    by_cat = progress.get("uploaded_by_cat", {})
                    print(f"\nCURRENT PROGRESS:")
                    print(f"  Total tracked: {total:,}")
                    if by_cat:
                        print(f"  By category:")
                        for cat, count in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
                            if count > 0:
                                print(f"    {cat:20s}: {count:6,}")

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped.")
            if follow:
                for proc in processes:
                    proc.terminate()

    def dashboard(self):
        """Show live dashboard with auto-refresh."""
        try:
            while True:
                os.system('clear' if os.name == 'posix' else 'cls')

                print("=" * 80)
                print(f"HARVEST DASHBOARD - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("=" * 80)

                # Get progress
                progress = self.get_current_progress()
                if not progress:
                    progress = {"uploaded": [], "uploaded_by_cat": {}, "failed": []}

                total = len(progress.get("uploaded", []))
                by_cat = progress.get("uploaded_by_cat", {})
                failed = len(progress.get("failed", []))

                # Calculate new items
                BASELINE_IA = 97147  # Items already on IA when we started
                new_items = total - BASELINE_IA if total > BASELINE_IA else 0

                # Overall stats
                print(f"\nOVERALL STATS:")
                print(f"  Already on IA:       {BASELINE_IA:,} (skipped)")
                print(f"  NEW items found:     {new_items:,} ⭐")
                print(f"  Total tracked:       {total:,}")
                print(f"  Failed items:        {failed:,}")

                # Calculate rate
                if self.last_total > 0:
                    elapsed = (datetime.now() - self.start_time).total_seconds() / 60
                    if elapsed > 0:
                        rate = (total - self.last_total) / elapsed
                        print(f"  Upload rate:         {rate:.1f} items/min")

                # Category breakdown
                if by_cat:
                    print(f"\nNEW ITEMS BY CATEGORY:")
                    total_new_by_cat = sum(by_cat.values())
                    for cat, count in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
                        if count > 0:
                            expected = CATEGORY_TOTALS.get(cat, 0)
                            pct = (count / expected * 100) if expected > 0 else 0
                            print(f"  {cat:20s}: {count:6,} new / {expected:6,} expected ({pct:5.1f}%)")

                # Check if harvest is running
                result = subprocess.run(
                    ["pgrep", "-f", "tdl_multi_worker|tdl_downloader"],
                    capture_output=True
                )
                if result.returncode == 0:
                    print(f"\n✓ Harvest is ACTIVE")
                else:
                    print(f"\n⚠ Harvest is NOT running")
                    print(f"  Start with: python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3")

                print(f"\nRefreshing in 10 seconds... (Ctrl+C to stop)")
                self.last_total = total
                time.sleep(10)

        except KeyboardInterrupt:
            print("\n\nDashboard stopped.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--cat", "--category", dest="category",
        help="Monitor specific category (e.g., Book, Periodical)"
    )
    parser.add_argument(
        "-f", "--follow", action="store_true",
        help="Follow mode (continuous, like tail -f)"
    )
    parser.add_argument(
        "-d", "--dashboard", action="store_true",
        help="Show live dashboard with auto-refresh"
    )

    args = parser.parse_args()

    monitor = RealtimeMonitor()

    if args.dashboard:
        monitor.dashboard()
    else:
        monitor.tail_logs(category=args.category, follow=args.follow)


if __name__ == "__main__":
    main()
