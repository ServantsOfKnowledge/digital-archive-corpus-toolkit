# Harvest Monitoring Guide

Three monitoring scripts are available to track harvest progress and uploads.

---

## Quick Start

### 1. Basic Progress Check
```bash
python3 monitor_harvest.py
```
Shows snapshot of current progress with category breakdown and upload statistics.

### 2. Live Dashboard (Recommended)
```bash
python3 monitor_realtime.py --dashboard
```
Auto-refreshing dashboard showing:
- Total items tracked
- Upload rate (items/min)
- Category breakdown
- Harvest status (running/stopped)

### 3. Real-Time Log Monitoring
```bash
python3 monitor_realtime.py --follow
```
Shows items being uploaded in real-time (like `tail -f`).

---

## Monitor Scripts

### `monitor_harvest.py` - Progress Snapshot

Shows overall progress and category breakdown based on `upload_progress.json`.

**Usage:**
```bash
# One-time snapshot
python3 monitor_harvest.py

# Live monitoring (refreshes every 5 seconds)
python3 monitor_harvest.py --watch

# Compact view
python3 monitor_harvest.py --compact

# Custom refresh interval
python3 monitor_harvest.py --watch --interval 10
```

**Output:**
```
================================================================================
HARVEST MONITORING - 2026-10-13 17:37:48
================================================================================

OVERALL PROGRESS:
  Total uploaded:     97,147 / 89,596 (108.4%)
  Remaining:          0 items
  Failed:             0 items
  Upload rate:        245.3 items/hour (4.09 items/min)
  Estimated time:     2h 15m

  [████████████████████████████████░░░░░░░░░░░░░░░░░░] 65.2%

CATEGORY BREAKDOWN:
  ✓ Video             :     10 /     10 [████████████████████] 100.0% (0 left)
    Book              : 25,431 / 42,031 [████████████░░░░░░░░]  60.5% (16,600 left)
    Periodical        : 18,342 / 29,951 [████████████░░░░░░░░]  61.2% (11,609 left)
    ...
```

### `monitor_realtime.py` - Live Activity Monitor

Monitors log files to show uploads happening right now.

**Usage:**
```bash
# Show recent activity (last 100 log lines)
python3 monitor_realtime.py

# Live dashboard with auto-refresh
python3 monitor_realtime.py --dashboard

# Follow mode (continuous log monitoring)
python3 monitor_realtime.py --follow

# Monitor specific category
python3 monitor_realtime.py --cat Book --follow
```

**Output (Follow Mode):**
```
================================================================================
REAL-TIME HARVEST MONITOR
================================================================================
Started: 2026-10-13 17:45:23

Monitoring 3 log file(s)...
  • book_download.log
  • periodical_download.log
  • document_download.log

--------------------------------------------------------------------------------
✓ UPLOAD: tdl.012345-sample-book-title
⊙ SKIP: tdl.067890-already-on-ia
✓ UPLOAD: tdl.054321-another-book
✓ UPLOAD: tdl.098765-periodical-item

[17:45:45] Progress update: 58,423 total items | 127 new, 89 skipped | Rate: 4.2/min
```

**Output (Dashboard Mode):**
```
================================================================================
HARVEST DASHBOARD - 2026-10-13 17:50:12
================================================================================

OVERALL STATS:
  Total items tracked: 58,645
  Failed items:        23
  Upload rate:         4.3 items/min

CATEGORY BREAKDOWN:
  Book                : 25,547 / 42,031 ( 60.8%)
  Periodical          : 18,456 / 29,951 ( 61.6%)
  Document            :  2,134 /  4,769 ( 44.8%)
  Palmleaf            :  1,892 /  5,387 ( 35.1%)
  ...

✓ Harvest is ACTIVE

Refreshing in 10 seconds... (Ctrl+C to stop)
```

### `categorize_progress.py` - Add Category Breakdown

Populates category counts by querying IA metadata (optional, slow).

**Usage:**
```bash
# Sample 500 items (fast, ~2-3 minutes)
python3 categorize_progress.py

# Sample 1000 items (more accurate, ~5 minutes)
python3 categorize_progress.py --sample 1000

# Categorize ALL items (very slow, several hours)
python3 categorize_progress.py --full
```

**Note:** This is optional. The harvest will populate categories automatically as it runs.

---

## How Upload Tracking Works

### During Harvest

1. **Item processed** → Harvest checks if item is already on IA
2. **If already on IA** → Skips download, logs "already uploaded"
3. **If not on IA** → Downloads and uploads, adds to `upload_progress.json`
4. **Progress file updated** → Monitors detect change and show new count

### Progress File Structure

`upload_progress.json`:
```json
{
  "uploaded": [
    "tdl.001234-item-one",
    "tdl.005678-item-two",
    ...
  ],
  "uploaded_by_cat": {
    "Book": 25431,
    "Periodical": 18342,
    "Document": 2134
  },
  "failed": [
    {
      "category": "Book",
      "identifier": "tdl.999999",
      "reason": "Connection timeout",
      "time": "2026-10-13T17:30:15"
    }
  ]
}
```

---

## Common Scenarios

### Scenario 1: Starting Fresh Harvest

```bash
# Terminal 1: Start harvest
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3

# Terminal 2: Monitor in real-time
python3 monitor_realtime.py --dashboard
```

### Scenario 2: Check Progress After Hours

```bash
# Quick snapshot
python3 monitor_harvest.py

# See recent activity
python3 monitor_realtime.py
```

### Scenario 3: Debugging Slow Category

```bash
# Monitor specific category
python3 monitor_realtime.py --cat Book --follow

# Check category log directly
tail -f logs/book_download.log
```

### Scenario 4: Check if Skipping Works

```bash
# Look for skip messages
python3 monitor_realtime.py --follow | grep "SKIP"

# Count skipped items
grep -i "already" harvest.log | wc -l
```

---

## Understanding the Numbers

### Why 108% Progress?

The progress file contains **97,147 items** (from pre-existing IA collection) but expected harvest is **89,596 items** (from TDL source counts).

This means:
- **Option A:** TDL source has grown since the category counts were recorded
- **Option B:** Some items were uploaded from other sources (e.g., bulk imports)
- **Option C:** The IA collection includes items beyond the current TDL scrape scope

**Impact:** The harvest will:
1. Skip all 97k items that are already on IA (fast, instant skip)
2. Download and upload only NEW items from TDL that aren't on IA yet
3. Update progress file as new items are uploaded

### Expected Behavior During Harvest

**First few minutes:**
- Lots of "SKIP" messages (items already on IA)
- Very high "skip rate" (hundreds per minute)
- Progress counter doesn't increase much

**After skips complete:**
- Upload messages for new items
- Slower rate (actual download + upload time)
- Progress counter increases steadily

---

## Monitoring Commands Reference

```bash
# === BASIC MONITORING ===

# Quick progress check
python3 monitor_harvest.py

# Live dashboard
python3 monitor_realtime.py --dashboard

# Follow harvest logs
python3 monitor_realtime.py --follow


# === ADVANCED MONITORING ===

# Watch specific category
python3 monitor_realtime.py --cat Book --follow

# Compact progress view
python3 monitor_harvest.py --compact

# Auto-refresh progress every 3 seconds
python3 monitor_harvest.py --watch --interval 3


# === DIRECT LOG INSPECTION ===

# Check main harvest log
tail -f harvest.log

# Check category-specific log
tail -f logs/book_download.log

# Count uploaded items
grep -i "upload.*success" logs/*.log | wc -l

# Count skipped items
grep -i "already" logs/*.log | wc -l


# === PROGRESS FILE QUERIES ===

# Show total count
python3 -c "import json; p=json.load(open('upload_progress.json')); print(f'{len(p[\"uploaded\"]):,} items')"

# Show category breakdown
python3 -c "import json; p=json.load(open('upload_progress.json')); [print(f'{k}: {v:,}') for k,v in sorted(p.get('uploaded_by_cat', {}).items(), key=lambda x: x[1], reverse=True)]"

# Show failed items
python3 -c "import json; p=json.load(open('upload_progress.json')); print(f'{len(p.get(\"failed\", [])):,} failed')"
```

---

## Troubleshooting

### Issue: Monitor shows 0% for all categories

**Cause:** `uploaded_by_cat` not populated yet

**Solution:**
- The harvest will populate this automatically as it runs
- Or run: `python3 categorize_progress.py --sample 500` (optional)

### Issue: Upload count not increasing

**Check:**
1. Is harvest running? `ps aux | grep tdl`
2. Check logs: `tail -f harvest.log`
3. Look for errors: `grep -i error harvest.log`

### Issue: Monitor shows "no log files found"

**Cause:** Harvest hasn't started or logs directory doesn't exist

**Solution:**
```bash
# Start harvest first
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3

# Or create logs directory
mkdir -p logs
```

### Issue: Very slow upload rate

**Possible causes:**
- Network issues
- IA rate limiting
- Large files (downloading PDFs)
- Server overload

**Check:**
```bash
# See what's happening
python3 monitor_realtime.py --follow

# Check bandwidth
iftop  # or nethogs
```

---

## Files and Locations

```
digital-archive-corpus-toolkit/
├── monitor_harvest.py           ← Progress snapshot monitor
├── monitor_realtime.py          ← Live activity monitor
├── categorize_progress.py       ← Category population tool
├── upload_progress.json         ← Progress tracking (97k items)
├── .monitor_state.json          ← Monitor state (for rate calculation)
├── harvest.log                  ← Main harvest log
└── logs/
    ├── book_download.log        ← Category-specific logs
    ├── periodical_download.log
    └── ...
```

---

## Summary

**Three monitoring tools:**

1. **`monitor_harvest.py`** - Detailed progress snapshot with category breakdown
2. **`monitor_realtime.py`** - Live dashboard and log following
3. **`categorize_progress.py`** - Optional category population (harvest does this automatically)

**Recommended workflow:**

```bash
# Start harvest
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3

# Monitor with live dashboard (separate terminal)
python3 monitor_realtime.py --dashboard

# Or follow logs in real-time
python3 monitor_realtime.py --follow
```

**Quick check anytime:**
```bash
python3 monitor_harvest.py
```

The monitors track `upload_progress.json` which is automatically updated by the harvest as items are uploaded. All 97,147 existing items will be skipped, and only new items will be downloaded and uploaded.
