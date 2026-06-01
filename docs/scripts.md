# Scripts Overview

## tdl_downloader.py
Download items from Tamil Digital Library (tamildigitallibrary.in) to local disk.

```
python3 tdl_downloader.py fetch --cat-id 20 --dir ./tdl_corpus --workers 3 --resume
python3 tdl_downloader.py fetch --cat-id 21 --dir ./tdl_corpus --workers 3 --resume --skip-existing /path/to/external/drive
```

**Flags:**
- `--cat-id N` — category ID (20=Book, 21=Periodical, etc.)
- `--dir PATH` — output directory
- `--workers N` — parallel download workers
- `--resume` — resume interrupted download from progress.json (skips completed, retries failed)
- `--retry` — retry only failed items
- `--force` — re-fetch listings from source (ignore cache)
- `--skip-existing PATH` — scan PATH and skip articles already present there (prevents re-downloading duplicates)

**Progress Tracking:**
- Maintains `{output_dir}/progress.json` with `completed`, `pending`, and `failed` article IDs
- On resume, completed items are automatically skipped
- Failed items are retried unless `--retry-failed` is specified (which processes only failed items)
- Supports HTTP range requests for resuming interrupted file transfers

**Deduplication:**
When `--skip-existing PATH` is used:
- Scans the specified directory for existing article IDs
- Adds those IDs to the `completed` set before downloading
- Prevents re-downloading items that are already present on external drives
- Useful when syncing across multiple storage devices

## tdl_upload.py
Upload downloaded items to Internet Archive under `TamilVirtualAcademy` collection.

```
python3 tdl_upload.py --cat Book --workers 6 --no-collection-check
python3 tdl_upload.py --cat Sculpture Palmleaf Map Inscription --workers 3 --no-collection-check
python3 tdl_upload.py --cat Periodical --corpus /path/to/external --workers 2 --no-collection-check
```

**Flags:**
- `--cat NAME [NAME ...]` — category(ies) to upload
- `--workers N` — parallel upload workers
- `--corpus PATH` — override corpus directory path
- `--no-collection-check` — skip collection permission check
- `--dry-run` — preview items without uploading
- `--retry-failed` — retry items from `upload_failed.json`
- `--delete IDENTIFIER` — delete an item from IA

**Metadata:** Sets `language:tam`, `collection:TamilVirtualAcademy`, and passes through `title`, `description`, `subject`, `creator`, `publisher`, `date`, `pages`, `edition`, `volume`, `source`, `original_url` from the item's `metadata.json`.

**Identifier format:** `tdl.{article_id}-{transliterated_title}` (max 80 chars). On bucket collision, appends `-001`, `-002`, `-003` suffixes.

**Upload Process:**
1. Scans category directories for items with `metadata.json`
2. Checks if item already exists on IA (skips if found)
3. Uploads individual files (PDF, cover, etc.) — no zipping
4. Tracks uploaded items in `upload_progress.json`
5. **Deletes source folder from disk** after successful upload
6. Failed uploads are logged to `upload_failed.json` for later retry

**Source Folder Deletion:**
After successful upload, the source folder is automatically deleted from disk using `shutil.rmtree()`. This is the intended behavior to manage disk space after content is safely stored on Internet Archive. To preserve source folders, use `--dry-run` first to verify the uploads will succeed, or comment out the deletion logic in the source code.

## tdl_pdf_recovery.py
Recover from Internet Archive PDF validation failures by redownloading affected items and retrying uploads.

```bash
# Scan a log and create pdf_redownload.json
python3 tdl_pdf_recovery.py --log-file logs/books_upload.log

# Redownload affected items and retry failed uploads
python3 tdl_pdf_recovery.py --log-file logs/books_upload.log --retry
```

Use this when upload logs contain `Uploaded content is unacceptable. - error checking pdf file`. The script removes corrupted PDFs, redownloads fresh copies into `tdl_redownload_temp/`, moves recovered folders back into `tdl_corpus/`, and records activity in `logs/pdf_recovery.log`.

## tdl_unzip_and_push.py
Re-upload items that were originally uploaded as zips — downloads the zip from IA, extracts files, uploads individual files, then deletes the zip.

```
python3 tdl_unzip_and_push.py --workers 2
python3 tdl_unzip_and_push.py --retry --workers 2
```

**Flags:**
- `--workers N` — parallel workers
- `--cat NAME [NAME ...]` — process only specific categories
- `--ident ID` — process a single identifier
- `--dry-run` — preview only
- `--retry` — retry items from `unzip_failed.json`
- `--reset` — reset `unzip_done.json`

## tdl_backfill_url.py
Backfill `original_url` metadata on already-uploaded items. Extracts numeric article IDs from uploaded identifiers and adds `original_url:https://tamildigitallibrary.in/Articles/{id}` via `ia metadata`.

```
python3 tdl_backfill_url.py --workers 8
python3 tdl_backfill_url.py --dry-run
```

**Flags:**
- `--workers N` — parallel metadata updates
- `--dry-run` — preview without making changes

## tdl_dashboard.py
Web dashboard for monitoring all operations.

```
python3 tdl_dashboard.py --port 8080
```

Access at http://localhost:8080. Shows process cards (status, PID, uptime, workers), a sortable category table (download/upload progress per category with disk usage), and start/stop/restart controls.

## Logging

All scripts write logs to the `logs/` directory:

| Log File | Purpose |
|----------|---------|
| `books.log` | Book download activity |
| `periodical.log` | Periodical download activity |
| `books_upload.log` | Book upload activity |
| `periodical_upload.log` | Periodical upload activity |
| `big_cats_upload.log` | Multi-category upload (Sculpture, Palmleaf, Map, Inscription) |
| `unzip_push.log` | Unzip and re-upload activity |
| `batch.log` | Batch runner output (if using `batch.py`) |
| `batch_download.log` | Batch download progress |
| `batch_error_*.log` | Per-category error logs from batch runner |

**Dashboard Log Management:**
The dashboard (`tdl_dashboard.py`) tails log files in real-time, displaying the last 6 lines for each process. It uses these logs to:
- Monitor process health and status
- Display activity history
- Track errors and progress

**To check logs:**
```bash
# View live updates
tail -f logs/books_upload.log

# Search for errors
grep "ERROR" logs/batch_error_*.log

# Count activity
wc -l logs/*.log
```
