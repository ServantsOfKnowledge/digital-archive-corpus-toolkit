# Server Handoff

Use this workflow to move only code and resumable state to a server. It does not copy downloaded corpus files.

## Package Contents

- Code and docs from this repository
- `server_state/upload_progress.json` so uploads skip items already completed on IA
- `server_state/upload_failed.json` if present
- `server_state/pdf_redownload.json` if present
- `server_state/listing_*.json` source listings used to redownload content

`tdl_corpus/`, logs, and temporary redownload folders are intentionally excluded.

## Server Setup

```bash
tar -xzf tmvu-corpus-server-handoff.tar.gz
cd tmvu-corpus-server-handoff
python3 -m pip install internetarchive requests beautifulsoup4 tqdm unidecode flask
ia configure
```

Restore state and listings:

```bash
python3 tdl_server_resume.py --restore-state
```

Download + upload Book and Periodical items (integrated pipeline):

```bash
python3 tdl_server_resume.py --download --cat Book Periodical --workers 3
```

This downloads items and uploads them to IA immediately after each successful download. Items already on IA are skipped. Local files are cleaned up after successful upload.

**Safety net** — retry any items that failed during the pipeline:

```bash
python3 tdl_server_resume.py --start-uploads --cat Book Periodical --workers 3
```

To intentionally limit uploads by local folder size, add `--max-size-mb 100`.

**Multi-worker support:** `--workers` controls parallel threads within each category. Categories are processed sequentially, but items within a category are handled concurrently.

Monitor:

```bash
tail -f logs/books_upload.log logs/periodicals_upload.log
```

Recover PDF validation failures:

```bash
python3 tdl_pdf_recovery.py --log-file logs/books_upload.log --retry
python3 tdl_pdf_recovery.py --log-file logs/periodicals_upload.log --retry
```
