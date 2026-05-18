# Digital Archive Corpus Toolkit

A comprehensive downloader, uploader, and corpus builder for digital archival collections hosted on the Tamil Digital Library (TDL). Uploads to Internet Archive under the **TamilVirtualAcademy** collection.

## Scripts

### `tdl_downloader.py`

Scrapes, downloads, and indexes digital archive content from TDL.

**Features:** Multi-category support (18 categories), automatic metadata extraction, parallel downloading with configurable workers, resume support via persistent progress tracking, corpus index generation (JSON/CSV), IA-ready metadata.

```bash
# List articles in a category
python tdl_downloader.py list --cat-id 20 --output listing.json

# Download from a listing file
python tdl_downloader.py download --input listing.json --dir tdl_corpus --workers 5

# Fetch (list + download) all articles
python tdl_downloader.py fetch --cat-id 20 --dir tdl_corpus --workers 5

# Build corpus index
python tdl_downloader.py corpus --dir tdl_corpus --csv
```

### `tdl_upload.py`

Uploads downloaded items to Internet Archive. Uploads individual files (PDF, cover.jpg, etc.) directly — **no zipping** — to allow IA derive to process content.

**Features:**
- Direct file upload (no zip) for better derive
- `--cat` accepts multiple categories
- `--corpus` to override corpus path (for external drives)
- Atomic writes and retry logic for multi-process safety
- Retry with exponential backoff on transient errors (timeout, connection, SlowDown)
- Auto-detects already-on-IA items and skips them
- Source folder deletion on successful upload
- `language:tam` and `TamilVirtualAcademy` collection metadata
- Transient error handling (SlowDown, timeout, connection errors)

```bash
# Preview
python tdl_upload.py --dry-run

# Upload specific categories
python tdl_upload.py --cat Book Sculpture --workers 3

# Upload from external drive
python tdl_upload.py --cat Document --corpus "/Volumes/External/tdl_corpus" --workers 2

# Retry failed
python tdl_upload.py --retry-failed

# Skip collection permission check
python tdl_upload.py --cat Book --workers 5 --no-collection-check
```

Identifiers use the format `tdl.{article_id}-{transliterated_title}`, truncated to 80 chars max. Includes `original_url` metadata pointing back to the source TDL article page. See `docs/scripts.md` for full flag documentation.

### `tdl_backfill_url.py`

Backfills `original_url` metadata on already-uploaded IA items. Extracts numeric article IDs from uploaded identifiers and adds the TDL source URL.

```bash
# Preview
python tdl_backfill_url.py --dry-run

# Run with 8 parallel workers
python tdl_backfill_url.py --workers 8
```

### `tdl_dashboard.py`

Web dashboard for monitoring all operations — process status, category progress, start/stop/restart controls.

```bash
python tdl_dashboard.py --port 8080
```

Access at http://localhost:8080.

### `tdl_status.py`

Live dashboard showing download + upload progress across all categories.

```
Category                DL /  Total     %     Size    Ul   Rem  Status
──────────────────── ─────   ──────  ────  ───────  ────  ────  ────────────
Book                 12016 /  42043   29%     909G     -     - ↑uploading
...
TOTAL                25277 /  89608   28%  FREE  135G  1680    49
```

- **DL:** items on disk (downloaded)
- **Ul:** items uploaded to IA (per-category, from `upload_progress.json`)
- **Rem:** items removed from disk after successful upload
- Status indicators: `↓downloading`, `↑uploading`

### `tdl_fix_metadata.py`

Batch retrofits existing IA items with `TamilVirtualAcademy` collection + `language:tam` metadata. Uses `ia search` to find items, then applies metadata via `ia metadata --modify`.

```bash
python tdl_fix_metadata.py
```

### `tdl_unzip_and_push.py`

For items uploaded via the old zip-based method: downloads the zip from IA, extracts the individual files, re-uploads them to the same identifier, then removes the zip. This triggers IA derive to process the actual content (PDFs, images) rather than treating a zip as opaque.

```bash
# Process all uploaded items
python tdl_unzip_and_push.py --workers 3

# Single item
python tdl_unzip_and_push.py --ident tdl.12345-foo

# Preview
python tdl_unzip_and_push.py --dry-run

# Reset progress
python tdl_unzip_and_push.py --reset
```

Progress tracked in `unzip_done.json` (resumable).

## Categories

| ID | Category | Items |
|----|----------|-------|
| 1 | Audio | 125 |
| 2 | Video | 10 |
| 3 | Map | 1,862 |
| 4 | Photograph | 53 |
| 5 | Author Bio | 196 |
| 6 | Pre-historic | 166 |
| 7 | Excavation | 55 |
| 8 | Inscription | 1,922 |
| 9 | Religious Place | 370 |
| 10 | Sculpture | 1,645 |
| 11 | Coin | 673 |
| 12 | Copper Plate | 192 |
| 13 | Historical Monument | 124 |
| 14 | Painting | 65 |
| 20 | Book | 42,043 |
| 21 | Periodical | 29,951 |
| 22 | Palmleaf | 5,387 |
| 27 | Document | 4,769 |

## Output Structure

```
tdl_corpus/
├── {Category}/
│   ├── {article_id}_{title}/
│   │   ├── metadata.json        # IA-ready metadata
│   │   ├── {pdf_filename}.pdf    # Document PDF
│   │   └── cover.jpg             # Cover image
│   └── ...
├── progress.json                  # Download progress
├── upload_progress.json           # Upload progress (per-category tracking)
├── upload_failed.json             # Failed uploads
├── unzip_done.json                # Unzip+push progress
└── unzip_failed.json              # Unzip failures
```

## Requirements

- Python 3.7+
- `requests`, `beautifulsoup4`, `tqdm`
- `internetarchive` (for `ia` CLI): `pip install internetarchive && ia configure`
- `unidecode` (for Tamil transliteration in identifiers)

```bash
pip install requests beautifulsoup4 tqdm internetarchive unidecode
```

## Corpus Fields (metadata.json)

| Field | Description |
|-------|-------------|
| `title` | Document title |
| `creator` | Author |
| `publisher` | Publisher |
| `date` | Publication year |
| `language` | Language |
| `subject` | Keywords/subjects |
| `description` | Full description |
| `source` | Source/location |
| `identifier` | Article ID |
| `original_url` | Source URL |
| `pages` | Page count |
| `edition` | Edition info |
| `volume` | Volume info |
| `collection` | Collection name |
| `mediatype` | Internet Archive mediatype |
| `scrape_date` | When metadata was scraped |
| `pdf_url` | Original PDF URL |
| `cover_url` | Original cover image URL |

---

*Maintained by [ServantsOfKnowledge](https://github.com/servantsofknowledge).*
