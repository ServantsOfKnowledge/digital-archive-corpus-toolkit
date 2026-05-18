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
- `--resume` — resume interrupted download from progress.json
- `--retry` — retry only failed items
- `--force` — re-fetch listings from source (ignore cache)
- `--skip-existing PATH` — skip items already present at PATH

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
- `--retry-failed` — retry items from failed list
- `--delete IDENTIFIER` — delete an item from IA

**Metadata:** Sets `language:tam`, `collection:TamilVirtualAcademy`, and passes through `title`, `description`, `subject`, `creator`, `publisher`, `date`, `pages`, `edition`, `volume`, `source`, `original_url` from the item's `metadata.json`.

**Identifier format:** `tdl.{article_id}-{transliterated_title}` (max 80 chars). On bucket collision, appends `-001`, `-002`, `-003` suffixes.

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
