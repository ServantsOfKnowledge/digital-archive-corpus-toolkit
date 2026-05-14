#!/usr/bin/env python3
"""
Batch runner: downloads ALL categories from a digital archive via tdl_downloader.py.
Processes smallest categories first (by item count) to maximize coverage.

Run: nohup python3 tdl_batch.py > batch.log 2>&1 &
"""
import subprocess, sys, time, os, shutil
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = "tdl_corpus"
WORKERS = 5
LOG_FILE = "batch_download.log"
MIN_FREE_GB = 10  # Stop if less than 10GB free

# Categories sorted by item count (ascending) — smallest first
CATEGORIES = [
    (2, "Video", 10),
    (4, "Photograph", 53),
    (7, "Excavation", 55),
    (14, "Painting", 65),
    (13, "HistoricalMonument", 124),
    (1, "Audio", 125),
    (6, "PreHistoric", 166),
    (12, "CopperPlate", 192),
    (5, "AuthorBio", 196),
    (9, "ReligiousPlace", 370),
    (11, "Coin", 673),
    (10, "Sculpture", 1645),
    (3, "Map", 1862),
    (8, "Inscription", 1922),
    (27, "Document", 4769),
    (22, "Palmleaf", 5387),
    (21, "Periodical", 29951),
    (20, "Book", 42043),
]

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def free_gb():
    usage = shutil.disk_usage(OUTPUT_DIR)
    return usage.free / (1024**3)

def run(cmd, cat_name):
    log(f"Starting {cat_name}: {' '.join(cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    out = (result.stdout or "") + (result.stderr or "")
    lines = out.strip().split("\n")
    for line in lines[-5:]:
        log(f"  {cat_name}: {line.strip()}")
    if result.returncode == 0:
        log(f"Completed {cat_name} in {elapsed:.0f}s")
    else:
        log(f"FAILED {cat_name} (rc={result.returncode}) in {elapsed:.0f}s")
        with open(f"batch_error_{cat_name}.log", "w") as f:
            f.write(out)
    return result.returncode

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    start_free = free_gb()
    log(f"{'='*60}")
    log(f"Batch Download Started")
    log(f"Output: {OUTPUT_DIR}, Workers: {WORKERS}")
    log(f"Free space: {start_free:.1f} GB")
    log(f"Min free threshold: {MIN_FREE_GB} GB")
    log(f"Categories: {len(CATEGORIES)}")
    log(f"{'='*60}")

    total_downloaded = 0
    for cid, name, count in CATEGORIES:
        free = free_gb()
        if free < MIN_FREE_GB:
            log(f"LOW DISK: {free:.1f} GB free — stopping")
            break

        log(f"--- Category {cid}: {name} ({count} items, {free:.1f} GB free) ---")
        for attempt in range(3):
            rc = run(
                ["python3", "tdl_downloader.py", "fetch",
                 "--cat-id", str(cid), "--dir", OUTPUT_DIR,
                 "--workers", str(WORKERS), "--resume"],
                name,
            )
            if rc == 0:
                break
            log(f"Retry {attempt+1}/3 for {name}")
            time.sleep(10)
        else:
            log(f"Giving up on {name} after 3 attempts")
            continue

        total_downloaded += count
        free = free_gb()
        used = start_free - free

    log(f"{'='*60}")
    log(f"Batch download phase complete")
    log(f"Total items attempted: ~{total_downloaded}")
    log(f"Space used: ~{start_free - free_gb():.1f} GB")
    log(f"Space remaining: {free_gb():.1f} GB")

    log(f"Building final corpus...")
    run(
        ["python3", "tdl_downloader.py", "corpus",
         "--dir", OUTPUT_DIR, "--name", "tdl_corpus", "--csv", "--cat-id", "0"],
        "corpus",
    )
    log(f"{'='*60}")
    log(f"Batch Download Complete")
    log(f"{'='*60}")
