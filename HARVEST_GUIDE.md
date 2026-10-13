# TDL Multi-Worker Harvest Guide

## Quick Start

### 1. Analyze Current State

First, check what's pending:

```bash
python3 analyze_state.py
```

This will show you:
- How many items are already uploaded
- Pending items per category
- Priority order for processing

### 2. Test with Dry Run

Preview what would happen without actually running:

```bash
python3 tdl_multi_worker.py --dry-run --total-workers 12 --categories-parallel 3
```

### 3. Start Harvesting

#### Option A: Process All Pending Categories (Recommended)

Process everything with 12 workers across 3 categories simultaneously:

```bash
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

This configuration means:
- **3 categories** are processed at the same time
- **4 workers per category** (12 total ÷ 3 categories = 4 workers each)
- Items are **uploaded immediately** after download (saves disk space)

#### Option B: Conservative Approach (Smaller Server)

If you have limited resources, use fewer workers:

```bash
python3 tdl_multi_worker.py --total-workers 6 --categories-parallel 2
```

This gives you 3 workers per category across 2 categories.

#### Option C: Aggressive Approach (Powerful Server)

If you have a powerful server and good bandwidth:

```bash
python3 tdl_multi_worker.py --total-workers 20 --categories-parallel 4
```

This gives you 5 workers per category across 4 categories.

#### Option D: Process Specific High-Priority Categories

Focus on the largest pending categories:

```bash
python3 tdl_multi_worker.py --cat Book Document Palmleaf --total-workers 15 --categories-parallel 3
```

This processes Book, Document, and Palmleaf with 5 workers each.

### 4. Monitor Progress

Watch the logs in real-time:

```bash
# Overall progress
tail -f logs/book_download.log

# Check multiple categories
tail -f logs/{book,document,palmleaf}_download.log
```

Check current state:

```bash
python3 analyze_state.py
```

### 5. Handle Interruptions

If the process is interrupted (Ctrl+C, connection loss, etc.), simply re-run the same command:

```bash
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
```

The `--resume` flag is implicit - the system will:
- Skip already uploaded items
- Retry failed items
- Continue from where it left off

## Architecture

### How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                   Multi-Worker Coordinator                   │
│                                                              │
│  - Schedules categories (smallest first for quick wins)     │
│  - Distributes workers across parallel categories           │
│  - Monitors progress and handles failures                   │
└──────────────────┬──────────────┬──────────────┬────────────┘
                   │              │              │
           ┌───────▼──────┐ ┌────▼─────┐ ┌──────▼───────┐
           │  Category 1  │ │Category 2│ │  Category 3  │
           │  (4 workers) │ │(4 workers)│ │  (4 workers) │
           └───────┬──────┘ └────┬─────┘ └──────┬───────┘
                   │              │              │
           ┌───────▼──────────────▼──────────────▼───────┐
           │       ThreadPoolExecutor (per category)     │
           │                                             │
           │  Worker 1 → Download → Upload → Delete     │
           │  Worker 2 → Download → Upload → Delete     │
           │  Worker 3 → Download → Upload → Delete     │
           │  Worker 4 → Download → Upload → Delete     │
           └─────────────────────────────────────────────┘
```

### Key Improvements Over Original Code

1. **Parallel Category Processing**
   - OLD: Process one category at a time
   - NEW: Process 3-4 categories simultaneously

2. **Better Resource Utilization**
   - OLD: 5 workers idle when waiting for page listings
   - NEW: Workers distributed across categories, always busy

3. **Disk Space Management**
   - OLD: Download all, then upload (requires lots of disk space)
   - NEW: Download → Upload → Delete immediately (minimal disk usage)

4. **Faster Completion**
   - OLD: Serial processing = 100+ hours for all categories
   - NEW: Parallel processing = ~30-40 hours for all categories (3x faster)

5. **Priority Scheduling**
   - OLD: Random or alphabetical order
   - NEW: Smallest categories first (quick wins, early feedback)

## Rate Limiting & Server Friendliness

The system includes multiple layers of rate limiting:

1. **Per-page delays** (0.5s between page listings)
2. **HTTP retry with exponential backoff** (1s, 2s, 4s, 8s...)
3. **Worker distribution** (spread load across categories)
4. **Automatic throttling** (fewer workers = lower rate)

### Recommended Configurations

| Server Type | Total Workers | Categories Parallel | Expected Rate |
|-------------|--------------|---------------------|---------------|
| Laptop/Small | 6 | 2 | ~10-15 items/min |
| Desktop/Medium | 12 | 3 | ~20-30 items/min |
| Server/Large | 20 | 4 | ~40-50 items/min |
| Powerful Server | 30 | 5 | ~60-80 items/min |

## Troubleshooting

### Too Many Rate Limit Errors

If you see HTTP 429 or 503 errors frequently:

```bash
# Reduce workers and parallelism
python3 tdl_multi_worker.py --total-workers 8 --categories-parallel 2
```

### Not Enough Disk Space

The `--upload-immediately` flag (default) should handle this, but if you still run out:

```bash
# Check disk usage
df -h .

# Clean up already-uploaded items
rm -rf tdl_corpus/*/[item-id]*  # Be careful!

# Or increase disk space before continuing
```

### Category Stuck/Frozen

If a category seems stuck:

1. Check the log file: `tail -f logs/{category}_download.log`
2. Kill the process (Ctrl+C)
3. Restart - it will resume automatically

### Want to Skip Upload for Now

Download only, upload later in batch:

```bash
python3 tdl_multi_worker.py --no-upload --total-workers 12 --categories-parallel 3
```

Then upload later:

```bash
python3 tdl_upload.py --workers 8
```

## Advanced Usage

### Process Only Failed Categories

After a run, some categories may have failed. Check the summary and re-run only those:

```bash
python3 tdl_multi_worker.py --cat FailedCat1 FailedCat2 --total-workers 8 --categories-parallel 2
```

### Custom Priority Order

Edit the `CATEGORIES` dictionary in `tdl_multi_worker.py` to change processing order. The script processes from smallest to largest by default, but you can modify the `get_pending_categories()` method to prioritize differently.

### Integration with Existing Workflow

The multi-worker harvester is compatible with existing tools:

- Uses same `upload_progress.json` for state tracking
- Saves logs to same `logs/` directory
- Creates same directory structure in `tdl_corpus/`
- Works with `tdl_status.py`, `analyze_state.py`

## Performance Expectations

Based on the current pending items:

| Category | Pending Items | Est. Time (12 workers, 3 parallel) |
|----------|--------------|-------------------------------------|
| Video | 10 | ~5 min |
| Excavation | 41 | ~15 min |
| Photograph | 37 | ~15 min |
| Painting | 43 | ~20 min |
| HistoricalMonument | 87 | ~30 min |
| Audio | 94 | ~40 min |
| PreHistoric | 128 | ~1 hour |
| CopperPlate | 154 | ~1.5 hours |
| AuthorBio | 157 | ~1.5 hours |
| ReligiousPlace | 332 | ~3 hours |
| Coin | 634 | ~5 hours |
| Map | 654 | ~5 hours |
| Sculpture | 721 | ~6 hours |
| Inscription | 1,242 | ~10 hours |
| Palmleaf | 4,433 | ~36 hours |
| Document | 4,768 | ~40 hours |
| Book | 41,387 | ~14 days |

**Total estimated time: ~16-18 days** (with 12 workers, 3 categories parallel, 24/7 operation)

Adjust workers to meet your timeline:
- **6 workers, 2 parallel**: ~30-35 days
- **20 workers, 4 parallel**: ~10-12 days
- **30 workers, 5 parallel**: ~7-9 days
