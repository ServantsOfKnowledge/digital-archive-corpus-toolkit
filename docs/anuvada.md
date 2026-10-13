# Anuvada Sampada Adapter

`anuvada_harvester.py` inventories [Anuvada Sampada](https://anuvadasampada.azimpremjiuniversity.edu.in/) through its OAI-PMH feed, enriches records from EPrints' JSON export, and compares every PDF with the current `AzimPremjiUniversity` IA collection before downloading anything.

Existing identifiers are preserved: `apu.anuvadasampada.hin.{record}.{document}` (Hindi) and `apu.anuvadasampada.kan.{record}` (Kannada).

## First run

All stages resume safely after interruption.

```bash
python3 anuvada_harvester.py inventory --full
python3 anuvada_harvester.py enrich --workers 4
python3 anuvada_harvester.py reconcile-ia

python3 anuvada_harvester.py status
python3 anuvada_harvester.py export

# Preview, then download only PDFs classified missing_on_ia
python3 anuvada_harvester.py download --limit 10
python3 anuvada_harvester.py download --limit 10 --execute

# Preview, then upload downloaded records still absent from IA
python3 anuvada_harvester.py ia-upload --limit 10
python3 anuvada_harvester.py ia-upload --limit 10 --execute
```

## Later syncs

Run `inventory` without `--full`; the saved OAI response timestamp limits the pass to changed records. An interrupted inventory restarts from its saved OAI resumption token. `enrich` selects only new, changed, failed or unfinished records; `enrich --refresh` starts a resumable full refresh.

Run `reconcile-ia` before each download batch. It refreshes all matching IA items, then deduplicates by established identifier, exact source-file URL, and (only when unambiguous) source record ID.

## Downloads

- Preflight against current EPrints metadata; browser-compatible PDF headers.
- Validate recorded size and MD5; retain `.part` files for HTTP Range resume; keep successfully downloaded PDFs.
- Documents EPrints reports as zero bytes (empty-file MD5) or whose canonical file URL returns 404 become `source_unavailable` instead of being retried forever. Older database rows are repaired on their next download attempt.
- `export` includes every source record, including deleted or metadata-only records without a PDF.

## Uploads

`download` and `ia-upload` are previews unless `--execute` is present. The intended IA identifier is rechecked immediately before invoking the `ia` CLI. New items get both `AzimPremjiUniversity` and `ServantsOfKnowledge` collection metadata; to override, repeat the option: `--collection AzimPremjiUniversity --collection ServantsOfKnowledge`.

Tests: `python3 -m unittest tests/test_anuvada_harvester.py`
