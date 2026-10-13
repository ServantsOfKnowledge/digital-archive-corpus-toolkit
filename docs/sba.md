# School Books Archive (SBA) Adapter

`sba_harvester.py` incrementally inventories the [School Books Archive](https://schoolbooksarchive.azimpremjiuniversity.edu.in/), enriches each record with public metadata and bitstream landing-page records, detects existing Internet Archive (IA) references, and produces JSONL/CSV exports plus a non-executing IA migration plan.

> [SBA's data policy](https://schoolbooksarchive.azimpremjiuniversity.edu.in/DataPolicy.html) restricts robotic harvesting of full items. Only use `--execute` under the archival-partner authorization applicable to this project.

## Commands

```bash
python3 sba_harvester.py inventory              # paginated listing (resumable upserts into SQLite)
python3 sba_harvester.py enrich --workers 3     # full public metadata and file records
python3 sba_harvester.py export                 # normalized JSONL/CSV
python3 sba_harvester.py status [--json]        # progress and Archive.org source/PDF counts
python3 sba_harvester.py ia-plan                # plan only; downloads/uploads nothing

python3 sba_harvester.py download --limit 10             # preview
python3 sba_harvester.py download --limit 10 --execute
python3 sba_harvester.py ia-upload --limit 10            # preview
python3 sba_harvester.py ia-upload --limit 10 --execute
```

For a small verification run, add `--max-items 5` to `inventory` and `--limit 5` to `enrich`.

## Resume behaviour

- `inventory` checkpoints the next source offset after every page. `--restart` begins a new full synchronization pass.
- `enrich` selects only records with missing metadata or a recorded error. `enrich --refresh` starts or resumes a checkpointed full refresh; `enrich --restart` discards that checkpoint.
- `download` selects the next incomplete records, checkpoints each completed file in the item's `manifest.json`, validates saved byte size, and discards incomplete `.part` files before retrying.
- `ia-upload` skips locally recorded completed/previously-existing items. Interrupted uploads retry the same identifier with IA checksum verification.
- `--limit` applies to the next *pending* records, not the first records in the collection.

## Safety

- Downloads and uploads are previews unless `--execute` is given.
- Local files are never deleted automatically.
- Uploads go only to the `ServantsOfKnowledge` collection by default; use `--collection` only to override intentionally.
- Records containing an `archive.org` reference are classified `skip_existing_ia_reference`.

## Downloads

SBA's bitstream endpoint requires `Accept: */*`. The downloader overrides its JSON API header for file requests and supplies the item-page referrer. Bitstreams marked locked by SBA have no public URL and are recorded as `skipped_locked_or_missing_url`.

## Multipart books

An item with several PDF bitstreams is merged in source sequence order into one `sba.{handle-suffix}.1.pdf` before upload. The original parts remain on disk and in `manifest.json`; only the merged PDF (plus any non-PDF bitstreams) goes to IA. `assembly.json` records each input path, size, SHA-256, page count, and the verified output, and is reused on retries. Requires `pypdf` (`python3 -m pip install pypdf`). Single-PDF items upload unchanged.

## Identifiers and duplicate prevention

New items use `sba.{handle-suffix}.1`. Immediately before upload the adapter checks:

1. the new identifier,
2. the earlier `apu.sba.{handle-suffix}.1` form,
3. an archive-wide metadata search for the exact SBA source URL or Handle.

A match under any identifier or collection is recorded as `already_on_ia` and not uploaded again. The check fails closed on IA lookup errors rather than risk a duplicate.

Tests: `python3 -m unittest tests/test_sba_harvester.py`
