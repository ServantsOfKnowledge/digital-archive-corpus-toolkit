# Shodhganga

Indian PhD theses from [Shodhganga](https://shodhganga.inflibnet.ac.in) (INFLIBNET) → Internet Archive, collection `shodhganga`, identifiers `shg.<handle-suffix>-<title-slug>` (max 80 chars).

| Script | Role |
|--------|------|
| `shg_downloader.py` | Original toolkit: `list`, `list-communities`, `manifest`, `pipeline`, `status` (imported from the former standalone `shodaganga` project; its metadata-only placeholder uploader was deliberately not carried over) |
| `shg_harvest.py` | Automated harvest: `preflight`, `plan`, `reconcile-ia`, `run`, `status` |
| `shg_harvest.sh` | `start` / `stop` / `status` wrapper (nohup, graceful stop via `harvest.stop`) |
| `shg_beta_harvest.py` | Alternative source (beta portal, metadata only): `probe`, `harvest`, `status`, `build` |

## State of the collected data (reviewed 2026-10-13)

Corpus dir: `~/shodaganga/shg_corpus` (not in git).

| Item | Finding |
|------|---------|
| Manifest | 579,387 entries, **14.8 TB** of PDFs (the old README said ~5 TB). Median thesis 11.6 MB, median 13 parts; 85 items ≥ 1 GB, max 4.8 GB |
| Unusable entries | 1,241 without PDF files, 47 with zero listed size, 1 bogus `handle/null` (`shg_ae83c716`) → **578,099 queued** |
| Local items | 837 `metadata.json` folders, **no PDFs downloaded**; `progress.json` only records the `manifested` stage |
| On IA | 18,622 `shg.*` items, all **metadata-only** (0 with PDF); 28 landed in `opensource` instead of `shodhganga` |
| Failed placeholder uploads | 404 (IA metadata lookups timed out) – they are not on IA and will be created by `run` together with their PDF |
| PDF proxy | `:8443` (`pdfToThesis.jsp`) **times out from the current server**; it is the only route to PDFs (the `:443` bitstream URL 302-redirects to it). The harvest needs a host that can reach it (India network/VPN) |
| Bot protection | As of 2026-10-13 item pages are served behind an **Anubis proof-of-work challenge** ("Making sure you're not a bot!"). `shg_downloader.fetch` raises `BotChallengeError` and the harvest records `blocked` and stops, instead of saving challenge pages as empty items. The harvest does not attempt to solve the challenge; resolve access with INFLIBNET first |
| Descriptions | Abstracts contain literal `newline` markers; `shg_harvest.py` strips them for new uploads. The 18.6k existing placeholders still have them and are corrected when their PDF is added |
| Identifiers | Local `metadata.json` files use `shg-<hs>-…`, IA items use `shg.<hs>-…`; the harvest always reuses the identifier already on IA for a handle |

Fixed on import: `upload_to_ia()` treated any existing IA item as done, which would have skipped the PDF for every placeholder. It now skips only items that already have a non-empty PDF.

**Policy: no item is created on IA without its PDF.** The earlier placeholder items (18,622 from the original project plus 19 created on 2026-10-14 from local metadata) are left untouched; `run` adds the PDF to them. Review them before deciding whether to remove the stubs (`--remove-placeholder` only deletes the stub `.txt`, not the item).

## Alternative source: betasg (metadata only)

While the main portal serves an Anubis challenge to scripts, **https://betasg.inflibnet.ac.in** (DSpace 9 beta) exposes its anonymous discover API.

### Commands

```bash
# 1. sanity-check the API: reachable? total items? sample records + field count
python3 shg_beta_harvest.py probe

# 2. trial run: fetch 3 pages (300 items) with 3 workers, then inspect
python3 shg_beta_harvest.py harvest --dir shg_beta_corpus --limit-pages 3 --workers 3
python3 shg_beta_harvest.py status  --dir shg_beta_corpus

# 3. full run: all 6,761 pages (~4 h); safe to re-run - completed pages are skipped
#    and failed pages retried. Graceful stop: touch shg_beta_corpus/harvest.stop
python3 shg_beta_harvest.py harvest --dir shg_beta_corpus --workers 6
python3 shg_beta_harvest.py status  --dir shg_beta_corpus            # add --offline to skip the live probe

# 4. merge page chunks into one deduplicated file (refuses if pages are missing)
python3 shg_beta_harvest.py build   --dir shg_beta_corpus            # -> shg_beta_corpus/metadata.jsonl
python3 shg_beta_harvest.py build   --dir shg_beta_corpus --partial  # build what exists anyway
```

### Flags

| Command | Flag | Default | Meaning |
|---------|------|---------|---------|
| all | `--dir` | `shg_beta_corpus` | Corpus directory holding `pages/`, `source.json`, output |
| all | `--timeout` | `120` | Per-request timeout (seconds); 100-item pages take ~20 s |
| `harvest` | `--workers` | `6` | Concurrent page fetches (tested up to 12; 4–8 is polite) |
| `harvest` | `--limit-pages N` | – | Fetch at most N pages (trial runs) |
| `harvest` | `--max-failures` | `25` | Stop after this many failed pages |
| `status` | `--offline` | off | Skip the live API probe |
| `build` | `--partial` | off | Build even when some pages are missing |

Exit codes: `0` success, `1` fatal (API unreachable / no source / build refused), `2` partial (failed or cancelled pages — just re-run `harvest`).

| Property | Value (2026-10-15) |
|----------|--------------------|
| Items | 676,009 → 6,761 pages of 100 (the API clamps larger page sizes) |
| Latency | ~20 s per 100-item page; pages parallelise cleanly (6 workers ≈ 6× throughput) |
| Full run | ≈ 6,761 pages ÷ 6 workers × 20 s ≈ **4 h**; re-run `harvest` resumes and retries failures |
| Fields | Full DSpace metadata per item (~25 `dc.*` fields incl. abstract, university, guide) |
| Not usable | OAI-PMH returns `noRecordsMatch` (empty), `/core/items` needs auth (401) — discover search is the only anonymous route |
| PDFs | **Not fetched** — this harvest is metadata only; PDF download still needs the main portal/8443 proxy arrangement |

Runtime files in `<dir>`: `pages/page-NNNNN.json` (atomic chunks; existing pages are skipped on re-run), `counts.jsonl` (items per page for `status`), `source.json` (totals seen), `harvest.stop` (touch to halt gracefully), and after `build`, `metadata.jsonl` (one record per item: `handle`, `uuid`, `title`, `last_modified`, `withdrawn`, full `metadata`; ~2 GB for the full corpus). `build` refuses to run with missing pages unless `--partial`; `--limit-pages N` gives a small trial run.

## Harvest workflow

```bash
export SHG=~/shodaganga/shg_corpus
python3 shg_harvest.py preflight --dir $SHG       # 443, 8443, pypdf/pdfunite, ia credentials, disk
python3 shg_harvest.py plan --dir $SHG            # review manifest, write harvest_queue.jsonl (~1 min)
python3 shg_harvest.py reconcile-ia --dir $SHG    # snapshot shg.* items on IA -> ia_state.json
python3 shg_harvest.py run --dir $SHG --limit 20                    # preview
python3 shg_harvest.py run --dir $SHG --limit 20 --execute          # real, start small
./shg_harvest.sh start        # long run; ./shg_harvest.sh status | stop
```

Per item `run` does: skip if IA already has a PDF → fetch metadata → download every part (each must start with `%PDF` and not be truncated) → merge → upload (adds the PDF to the existing placeholder item, or creates the item) → delete local files. A failed download or merge never uploads a partial thesis.

Safeguards: preview unless `--execute`; refuses to start when `preflight` fails (`--force` to override); stops after `--max-consecutive-failures` (20) failures in a row; waits when free disk would drop below `--min-free-gb` (100) counting 2.2× each in-flight item; skips items over `--max-item-gb` (5); graceful stop with `touch $SHG/harvest.stop`.

| Flag | Default | Meaning |
|------|---------|---------|
| `--workers` | 4 | Parallel items |
| `--upload-slots` | 2 | Concurrent IA uploads |
| `--limit N` / `--only HS...` | – | Subset for trials |
| `--skip-failed` | off | Do not retry earlier failures |
| `--keep-files` | off | Keep local PDFs |
| `--remove-placeholder` | off | Delete the stub `.txt` from items that receive a PDF |
| `--work-dir` | `<dir>/work` | Scratch space |

Queue order is smallest first; sub-100 KB "PDFs" (usually stubs) go last.

Runtime files in the corpus dir: `harvest_queue.jsonl`, `ia_state.json`, `harvest_results.jsonl` (append-only, last row per handle wins; `uploaded`, `on_ia`, `no_pdf` are final, everything else is retried), `harvest.stop`.

## Requirements

`pip install requests beautifulsoup4 tqdm internetarchive pypdf` (or `pdfunite`), `ia configure`. The `tmvu-corpus-server-handoff` venv used by the TDL scripts has everything except `pypdf`. Re-run `reconcile-ia` periodically (and before a restart on a new host) so `ia_state.json` reflects IA.

Scale: 14.8 TB must go through a disk-bounded download→upload→delete loop; at the 2 TB free on this host that is fine with the default disk guard, but upload bandwidth to IA is the real limit.

The original listing/manifest commands and rate-limit notes are unchanged; see `python3 shg_downloader.py <command> --help`.
