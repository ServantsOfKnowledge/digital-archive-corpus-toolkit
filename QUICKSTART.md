# TDL Harvesting - Quick Start Guide

## Current Status

Based on the previous run in `../tmvu-corpus-server-handoff/`:

- **17,472 items** already uploaded to Internet Archive ✓
- **~52,931 items** still pending
- **18 categories** with varying completion rates

## What's New

I've created an **improved multi-worker harvester** that:

1. **Processes multiple categories in parallel** (3x faster than before)
2. **Distributes workers efficiently** across categories
3. **Uploads immediately** after download (saves disk space)
4. **Prioritizes smallest categories first** (quick wins)
5. **Fully resumable** at any point

## Getting Started

### Step 1: Check Current State

```bash
python3 analyze_state.py
```

This shows you:
- What's been uploaded
- What's still pending
- Priority order

### Step 2: Start Harvesting

For a balanced approach with **12 workers across 3 categories**:

```bash
# Run in background with nohup
nohup python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3 > harvest.log 2>&1 &

# Or run in foreground (Ctrl+C to stop)
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

This will:
- Process 3 categories simultaneously
- Use 4 workers per category (12 total ÷ 3)
- Download → Upload → Delete each item (minimal disk usage)
- Resume automatically if interrupted

### Step 3: Monitor Progress

#### Option A: Real-time Dashboard

```bash
./monitor_harvest.sh
```

Refreshes every 30 seconds with:
- Category progress
- Recent activity
- System resources
- Active processes

#### Option B: Check Logs

```bash
# Watch specific category
tail -f logs/book_download.log

# Check progress periodically
python3 analyze_state.py
```

### Step 4: Resume After Interruption

If the process stops for any reason, just re-run the same command:

```bash
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

It will automatically:
- Skip already uploaded items
- Resume from where it left off
- Retry failed items

## Configuration Options

### Conservative (Small Server / Slow Connection)

```bash
python3 tdl_multi_worker.py --total-workers 6 --categories-parallel 2
```

- 2 categories in parallel
- 3 workers per category
- Lower server load
- ~30-35 days for all items

### Balanced (Recommended)

```bash
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

- 3 categories in parallel
- 4 workers per category
- Moderate server load
- ~16-18 days for all items

### Aggressive (Powerful Server / Fast Connection)

```bash
python3 tdl_multi_worker.py --total-workers 20 --categories-parallel 4
```

- 4 categories in parallel
- 5 workers per category
- Higher server load
- ~10-12 days for all items

### Very Aggressive (Maximum Speed)

```bash
python3 tdl_multi_worker.py --total-workers 30 --categories-parallel 5
```

- 5 categories in parallel
- 6 workers per category
- Heavy server load (monitor for rate limits!)
- ~7-9 days for all items

## Processing Specific Categories

Focus on high-priority categories only:

```bash
# Just the big ones
python3 tdl_multi_worker.py --cat Book Document Palmleaf --total-workers 15 --categories-parallel 3

# Small categories for quick wins
python3 tdl_multi_worker.py --cat Video Photograph Excavation Painting --total-workers 8 --categories-parallel 4
```

## Files Created

- `tdl_multi_worker.py` - New parallel harvester
- `analyze_state.py` - Progress analysis tool
- `monitor_harvest.sh` - Real-time monitoring dashboard
- `HARVEST_GUIDE.md` - Detailed documentation
- `QUICKSTART.md` - This file

## Troubleshooting

### "Too many requests" or rate limit errors

Reduce workers:

```bash
python3 tdl_multi_worker.py --total-workers 8 --categories-parallel 2
```

### Process seems stuck

1. Check the log file for the category
2. Press Ctrl+C to stop
3. Re-run the same command (it will resume)

### Running out of disk space

The `--upload-immediately` flag (default) should prevent this, but if needed:

```bash
# Check disk usage
df -h .

# Clean up corpus directory (be careful!)
rm -rf tdl_corpus/*/[0-9]*

# Then resume
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

### Want to run in background

```bash
# Start in background
nohup python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3 > harvest.log 2>&1 &

# Check if running
ps aux | grep tdl_multi_worker

# Monitor output
tail -f harvest.log

# Stop if needed
pkill -f tdl_multi_worker
```

## Expected Timeline

With **12 workers, 3 categories parallel, 24/7 operation**:

| Priority | Category | Pending | Est. Time |
|----------|----------|---------|-----------|
| 1 | Video | 10 | 5 min |
| 2 | Photograph | 53 | 15 min |
| 3 | Excavation | 55 | 20 min |
| 4 | Painting | 65 | 20 min |
| 5 | HistoricalMonument | 124 | 30 min |
| 6 | Audio | 125 | 40 min |
| 7 | PreHistoric | 166 | 1 hour |
| 8 | CopperPlate | 192 | 1.5 hours |
| 9 | AuthorBio | 196 | 1.5 hours |
| 10 | ReligiousPlace | 370 | 3 hours |
| 11 | Coin | 673 | 5 hours |
| 12 | Map | 1,862 | 12 hours |
| 13 | Inscription | 1,922 | 12 hours |
| 14 | Sculpture | 1,645 | 10 hours |
| 15 | Document | 4,769 | 2 days |
| 16 | Palmleaf | 5,387 | 2 days |
| 17 | Periodical | 29,951 | 10 days |
| 18 | Book | 42,031 | 14 days |

**Total: ~16-18 days** (continuous operation)

Smaller categories (1-11) will complete in first ~24 hours, giving you quick feedback!

## Next Steps

1. **Review** the configuration options above
2. **Start** with the balanced approach (12 workers, 3 parallel)
3. **Monitor** using `monitor_harvest.sh` or `analyze_state.py`
4. **Adjust** workers if you see rate limiting or want faster processing
5. **Let it run** - the system is fully automated and resumable

## Questions?

- See `HARVEST_GUIDE.md` for detailed documentation
- Check logs in `logs/` directory for debugging
- Run `python3 analyze_state.py` anytime to check progress
