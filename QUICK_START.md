# Quick Start Guide - TVU Harvest

## Status: Ready to Run ✓

All fixes applied. The harvest will now skip 97,147 items already on Internet Archive.

---

## Start Harvest

```bash
cd /home/sokadmin/digital-archive-corpus-toolkit

# Start the harvest
nohup python3 tdl_multi_worker.py \
  --total-workers 12 \
  --categories-parallel 3 \
  > harvest.log 2>&1 &

# Get process ID
echo $! > harvest.pid
```

---

## Monitor Progress

### Option 1: Live Dashboard (Recommended)
```bash
python3 monitor_realtime.py --dashboard
```
Auto-refreshing dashboard with upload rate and progress.

### Option 2: Quick Check
```bash
python3 monitor_harvest.py
```
One-time snapshot of current progress.

### Option 3: Watch Logs
```bash
python3 monitor_realtime.py --follow
```
See items being uploaded in real-time.

---

## Quick Commands

```bash
# Check if harvest is running
ps aux | grep tdl_multi_worker

# Show recent uploads
python3 monitor_realtime.py

# Count total tracked items
python3 -c "import json; print(f\"{len(json.load(open('upload_progress.json'))['uploaded']):,}\")"

# Watch main log
tail -f harvest.log

# Stop harvest (if needed)
pkill -f tdl_multi_worker
```

---

## What to Expect

### How It Works:
The harvest compares TDL source items against Internet Archive:
- **Already on IA:** 97,147 items (skipped instantly) ✓
- **NEW items found:** Anything not in those 97k gets uploaded ⭐
- **Status tracks both:** Shows baseline + new discoveries

### First 5-10 minutes:
- **Lots of "SKIP" messages** - Items already on IA being skipped (fast)
- **High skip rate** - Hundreds per minute
- Total stays ~97k (existing items)

### After skips complete:
- **Upload messages** - New items being downloaded and uploaded
- **Slower rate** - Actual download + upload takes time
- **NEW items counter increases** - Watch the ⭐ number grow!

### Example Status:
```
Already on IA:  97,147 (skipped)
NEW items:      92 ⭐
Total tracked:  97,239
```
This means 92 items were found in TDL that weren't on IA yet!

---

## Files Created

- **`upload_progress.json`** - Tracks 97,147 items (will be updated as harvest runs)
- **`monitor_harvest.py`** - Progress snapshot monitor
- **`monitor_realtime.py`** - Live activity monitor
- **`FIX_COMPLETE.md`** - Detailed fix documentation
- **`MONITORING_GUIDE.md`** - Complete monitoring documentation
- **`QUICK_START.md`** - This guide

---

## Success Indicators

✓ Progress file has 97,147 items
✓ Harvest skips already-uploaded items
✓ New items are downloaded and uploaded
✓ Monitors show real-time progress
✓ Category breakdown updates automatically

---

## Need Help?

See detailed guides:
- **`MONITORING_GUIDE.md`** - All monitoring options
- **`FIX_COMPLETE.md`** - Technical details of the fix
- **`HARVEST_FIX_SUMMARY.md`** - Problem analysis and solution

**You're ready to start harvesting!**
