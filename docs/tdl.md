# Tamil Digital Library (TDL) Pipeline

Downloads TDL content, builds an IA-ready corpus, and uploads to Internet Archive under the **TamilVirtualAcademy** collection. Per-script flags are in [scripts.md](scripts.md); large-scale parallel runs are in [tdl-harvest.md](tdl-harvest.md); moving work to a server is in [server-handoff.md](server-handoff.md).

## Scripts

| Script | Purpose |
|--------|---------|
| `tdl_downloader.py` | List, download, and index articles (18 categories); `list`, `download`, `fetch`, `corpus` subcommands |
| `tdl_upload.py` | Upload items to IA as individual files (no zip) so IA derive works |
| `tdl_multi_worker.py` | Parallel multi-category download+upload coordinator ([guide](tdl-harvest.md)) |
| `tdl_pdf_recovery.py` | Redownload and retry items whose PDFs IA rejected |
| `tdl_server_resume.py` | Resume work on a fresh server without copying the corpus |
| `tdl_backfill_url.py` | Backfill `original_url` on already-uploaded IA items |
| `tdl_fix_metadata.py` | Retrofit `TamilVirtualAcademy` collection + `language:tam` metadata |
| `tdl_unzip_and_push.py` | Convert old zip-based uploads into individual files |
| `tdl_status.py` / `tdl_dashboard.py` | Terminal status table / web dashboard |

## Common commands

```bash
python tdl_downloader.py list --cat-id 20 --output listing.json
python tdl_downloader.py download --input listing.json --dir tdl_corpus --workers 5
python tdl_downloader.py fetch --cat-id 20 --dir tdl_corpus --workers 5
python tdl_downloader.py download --input listing.json --dir tdl_corpus --workers 5 --upload-immediately
python tdl_downloader.py corpus --dir tdl_corpus --csv

python tdl_upload.py --dry-run
python tdl_upload.py --cat Book Sculpture --workers 3
python tdl_upload.py --cat Document --corpus "/Volumes/External/tdl_corpus" --workers 2
python tdl_upload.py --retry-failed

python3 tdl_pdf_recovery.py --log-file logs/books_upload.log --retry   # for "error checking pdf file"
python tdl_backfill_url.py --workers 8
python tdl_unzip_and_push.py --workers 3        # --ident, --dry-run, --reset
python tdl_dashboard.py --port 8080
```

## Download and resume

- `progress.json` tracks `completed`, `pending` and `failed` article IDs; `--resume` skips completed and retries failed.
- `--skip-existing PATH` marks IDs found in another directory as completed.
- HTTP Range requests resume partial files; transient errors retry with exponential backoff.
- With `--upload-immediately`, IA is checked before downloading, and local files are deleted after a successful upload.

## Upload

1. Scan categories for folders containing `metadata.json`.
2. Skip items already on IA.
3. Upload PDF, cover and metadata as individual files.
4. Record in `upload_progress.json`; failures go to `upload_failed.json`.
5. Delete the source folder after success.

Identifiers are `tdl.{article_id}-{transliterated_title}` (max 80 chars) with `original_url` pointing at the TDL article. Metadata includes `language:tam` and the `TamilVirtualAcademy` collection.

## Categories

| ID | Category | Items | | ID | Category | Items |
|----|----------|-------|-|----|----------|-------|
| 1 | Audio | 125 | | 9 | Religious Place | 370 |
| 2 | Video | 10 | | 10 | Sculpture | 1,645 |
| 3 | Map | 1,862 | | 11 | Coin | 673 |
| 4 | Photograph | 53 | | 12 | Copper Plate | 192 |
| 5 | Author Bio | 196 | | 13 | Historical Monument | 124 |
| 6 | Pre-historic | 166 | | 14 | Painting | 65 |
| 7 | Excavation | 55 | | 20 | Book | 42,043 |
| 8 | Inscription | 1,922 | | 21 | Periodical | 29,951 |
| 22 | Palmleaf | 5,387 | | 27 | Document | 4,769 |

## Output layout

```
tdl_corpus/{Category}/{article_id}_{title}/{metadata.json, *.pdf, cover.jpg}
progress.json  upload_progress.json  upload_failed.json  unzip_done.json  unzip_failed.json
logs/          books.log, books_upload.log, batch*.log, ...
server_state/  upload_progress.json, upload_failed.json, pdf_redownload.json, listing_*.json
```

## `metadata.json` fields

`title`, `creator`, `publisher`, `date`, `language`, `subject`, `description`, `source`, `identifier`, `original_url`, `pages`, `edition`, `volume`, `collection`, `mediatype`, `scrape_date`, `pdf_url`, `cover_url`.

## Requirements

Python 3.7+ and `pip install requests beautifulsoup4 tqdm internetarchive unidecode`, then `ia configure`.
