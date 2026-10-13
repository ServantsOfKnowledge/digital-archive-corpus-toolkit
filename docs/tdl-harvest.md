# TDL Multi-Worker Harvest and Monitoring

`tdl_multi_worker.py` runs the TDL download→upload→delete pipeline over several categories in parallel. It shares `upload_progress.json`, `logs/` and `tdl_corpus/` with the single-category tools, so `tdl_status.py` and `analyze_state.py` keep working.

## 1. Prepare progress state

The harvester skips anything listed in `upload_progress.json` before touching the network. Without it, every item needs a slow per-item `ia metadata` check (or is re-downloaded).

```bash
python3 build_progress_from_ia_scraping.py   # rebuild from IA (see below)
python3 analyze_state.py                     # pending work per category
```

Do not pass `--help` to `build_progress_from_ia_scraping.py`: it takes no arguments, so it runs, and it renames the existing `upload_progress.json` to `upload_progress.backup.<ts>.json` first.

### Rebuilding `upload_progress.json` from IA

| Script | Notes |
|--------|-------|
| `build_progress_from_ia_scraping.py` | **Use this.** `ia search --itemlist` over `TamilVirtualAcademy`; no 10k paging limit. Backs up the old file, then writes the new one. Needs the `internetarchive` package installed in the active Python. |
| `rebuild_progress_from_ia.py` | Web-API variant, limited by IA's 10,000-result deep paging. `--verify` compares counts. Kept for diagnosis. |
| `categorize_progress.py` | Optional: fills `uploaded_by_cat` by sampling IA `subject` metadata (`--sample N`, default 500, or `--full`, slow). The harvest fills it anyway as it runs. |

Why this exists: an older progress file held ~22k identifiers that did not exist on IA, so a new harvest directory would have re-downloaded most of the ~97k items already in the collection. The collection mixes `tdl.*` (TDL harvest) and `dli.*` (Digital Library of India import) identifiers. Check the live count with:

```bash
curl -s "https://archive.org/advancedsearch.php?q=collection:TamilVirtualAcademy&rows=0&output=json" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['response']['numFound'])"
```

`upload_progress.json` shape:

```json
{"uploaded": ["tdl.001234-item-one"], "uploaded_by_cat": {"Book": 25431}, "failed": [{"category": "Book", "identifier": "tdl.999999", "reason": "...", "time": "..."}]}
```

Per item the harvester checks the local file first (instant), then IA (slow). Items found on IA are added to the file so the check happens once.

## 2. Run

```bash
python3 tdl_multi_worker.py --dry-run --total-workers 12 --categories-parallel 3
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
python3 tdl_multi_worker.py --cat Book Document Palmleaf --total-workers 15 --categories-parallel 3
python3 tdl_multi_worker.py --no-upload ...     # download only; upload later with tdl_upload.py
```

| Flag | Meaning |
|------|---------|
| `--total-workers N` | Workers across all categories (default 12), split evenly among parallel categories |
| `--categories-parallel N` | Categories processed at once (default 3) |
| `--cat A B ...` | Only these categories (default: all pending, smallest first) |
| `--no-upload` | Skip immediate upload |
| `--dry-run` | Preview only |
| `--resume` | Default behaviour: skips uploaded items, retries failed |

Helper scripts (they `cd` to this repo and activate the venv at `~/tmvu-corpus-server-handoff/venv`; edit paths for other hosts):

```bash
./start_harvest.sh    # nohup run, writes harvest.log and harvest.pid
./status.sh           # running?, processes, logs
./stop_harvest.sh     # kills via harvest.pid / pkill
```

Interrupted runs: re-run the same command.

| Host | Workers | Parallel categories | Rough rate |
|------|---------|---------------------|-----------|
| Laptop | 6 | 2 | 10–15 items/min |
| Desktop | 12 | 3 | 20–30 items/min |
| Server | 20 | 4 | 40–50 items/min |
| Large server | 30 | 5 | 60–80 items/min |

Rates are rough estimates, not measurements. On frequent HTTP 429/503, reduce workers and parallelism. Rate limiting is per-page delay (0.5 s), exponential backoff on retries, and load spread across categories.

## 3. Monitor

```bash
python3 monitor_harvest.py [--watch] [--interval 10] [--compact] [--reset-state]
python3 monitor_realtime.py [--dashboard | --follow] [--cat Book]
```

- `monitor_harvest.py`: snapshot of `upload_progress.json`: totals, per-category progress bars, upload rate, ETA. Rate history is kept in `.monitor_state.json`.
- `monitor_realtime.py`: reads harvest logs; `--dashboard` auto-refreshes, `--follow` streams uploads/skips like `tail -f`.

Useful checks:

```bash
tail -f harvest.log logs/book_download.log
grep -ci already harvest.log          # items skipped as already on IA
python3 -c "import json; print(len(json.load(open('upload_progress.json'))['uploaded']))"
```

Reading the numbers:

- Progress above 100% is possible: the IA collection can hold items outside the current TDL category counts (e.g. the DLI import, or source growth).
- Early in a run expect many "skip" lines and little upload progress; throughput drops once real downloads begin.
- 0% for every category just means `uploaded_by_cat` is not populated yet.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Items still downloading that are on IA | Identifier format in logs vs `upload_progress.json`; confirm `--upload-immediately` in `ps aux \| grep tdl_downloader` |
| Out of disk | `df -h .`; pipeline mode deletes after upload; avoid `--no-upload` on small disks |
| Category stuck | `tail -f logs/<category>_download.log`, Ctrl+C, restart |
| Upload count flat | `ps aux \| grep tdl`, `grep -i error harvest.log` |
| Monitor finds no logs | Start the harvest or `mkdir -p logs` |
| `ModuleNotFoundError: internetarchive` | Run inside the venv, or `pip install internetarchive` |
