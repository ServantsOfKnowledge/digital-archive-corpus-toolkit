#!/usr/bin/env python3
"""Show download + upload progress for all categories in tdl_corpus/."""
import json, shutil, subprocess, sys
from pathlib import Path

CORPUS = Path(__file__).parent / "tdl_corpus"
PROGRESS = Path(__file__).parent / "upload_progress.json"

CATEGORIES = [
    (2, "Video", 10), (4, "Photograph", 53), (7, "Excavation", 55),
    (14, "Painting", 65), (13, "HistoricalMonument", 124), (1, "Audio", 125),
    (6, "PreHistoric", 166), (12, "CopperPlate", 192), (5, "AuthorBio", 196),
    (9, "ReligiousPlace", 370), (11, "Coin", 673), (10, "Sculpture", 1645),
    (3, "Map", 1862), (8, "Inscription", 1922), (27, "Document", 4769),
    (22, "Palmleaf", 5387), (21, "Periodical", 29951), (20, "Book", 42043),
]

def count_items(cat_dir: Path) -> int:
    if not cat_dir.exists():
        return 0
    return sum(1 for p in cat_dir.iterdir() if p.is_dir())

def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total

def disk_free(path: Path) -> str:
    usage = shutil.disk_usage(path)
    gb = usage.free / (1024**3)
    return f"{gb:.0f}G" if gb > 100 else f"{gb:.1f}G"

def proc_info(pattern: str) -> dict:
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return {}
    procs = {}
    for line in out.splitlines():
        if pattern in line and "grep" not in line:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                pid = parts[1]
                cmd = parts[10]
                if "--cat-id" in cmd:
                    cid = cmd.split("--cat-id")[1].split()[0]
                    procs[cid] = pid
                elif "--cat" in cmd:
                    procs["upload"] = pid
    return procs

def load_upload_progress():
    if not PROGRESS.exists():
        return {}
    try:
        return json.loads(PROGRESS.read_text())
    except Exception:
        return {}

def main():
    if not CORPUS.exists():
        print(f"Error: {CORPUS} not found")
        sys.exit(1)

    free = disk_free(CORPUS)
    dl_procs = proc_info("tdl_downloader.py")
    ul_procs = proc_info("tdl_upload.py")
    ul = load_upload_progress()
    uploaded_by_cat = ul.get("uploaded_by_cat", {})

    total_dl = 0
    total_all = 0
    total_ul = 0
    total_rem = 0

    header = f"{'Category':<20} {'DL':>5} / {'Total':>6}  {'%':>4}  {'Size':>7}  {'Ul':>4}  {'Rem':>4}  Status"
    sep = f"{'─'*20} {'─'*5}   {'─'*6}  {'─'*4}  {'─'*7}  {'─'*4}  {'─'*4}  {'─'*12}"
    print(header)
    print(sep)

    remaining_ul = sum(v for k, v in uploaded_by_cat.items() if k not in [c[1] for c in CATEGORIES])

    for cid, name, total in CATEGORIES:
        dl = count_items(CORPUS / name)
        sz = dir_size(CORPUS / name)
        sz_str = f"{sz/1e9:.0f}G" if sz > 100e9 else f"{sz/1e6:.0f}M" if sz > 0 else "-"
        pct = f"{dl/total*100:3.0f}%" if total else " - "

        uploaded = uploaded_by_cat.get(name, 0)
        ul_str = f"{uploaded:>4,}" if uploaded else "   -"

        removed = uploaded - dl if uploaded >= dl else 0
        rem_str = f"{removed:>4,}" if removed else "   -"

        active = ""
        if str(cid) in dl_procs:
            active = " ↓downloading"
        if "upload" in ul_procs:
            active += " ↑uploading"

        print(f"{name:<20} {dl:>5,} / {total:>6,}  {pct:>4}  {sz_str:>7}  {ul_str:>4}  {rem_str:>4}{active}")
        total_dl += dl
        total_all += total
        total_ul += uploaded
        total_rem += removed

    sep2 = f"{'─'*20} {'─'*5}   {'─'*6}  {'─'*4}  {'─'*7}  {'─'*4}  {'─'*4}"
    print(sep2)

    if remaining_ul:
        total_ul += remaining_ul
        print(f"{'unassigned':<20} {'':>5}   {'':>6}  {'':>4}  {'':>7}  {remaining_ul:>4,}  {'':>4}")

    print(f"{'TOTAL':<20} {total_dl:>5,} / {total_all:>6,}  {total_dl/total_all*100:3.0f}%  FREE {free:>5}  {total_ul:>4,}  {total_rem if total_rem else '-':>4}")

if __name__ == "__main__":
    main()
