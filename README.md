# Digital Archive Corpus Toolkit

Downloaders, harvesters and uploaders that build corpora from digital archives and publish them to the [Internet Archive](https://archive.org) (IA). Every tool is resumable, checks IA for existing items before downloading or uploading, and keeps destructive or outward-facing actions behind explicit flags.

## Projects

| Project | Source | IA destination | Entry points | Docs |
|---------|--------|----------------|--------------|------|
| **Tamil Digital Library (TDL)** | tamildigitallibrary.in, 18 categories, ~90k items | `TamilVirtualAcademy` | `tdl_downloader.py`, `tdl_upload.py`, `tdl_multi_worker.py` | [docs/tdl.md](docs/tdl.md) |
| **School Books Archive (SBA)** | schoolbooksarchive.azimpremjiuniversity.edu.in | `ServantsOfKnowledge` | `sba_harvester.py` | [docs/sba.md](docs/sba.md) |
| **Anuvada Sampada** | anuvadasampada.azimpremjiuniversity.edu.in (OAI-PMH / EPrints) | `AzimPremjiUniversity` + `ServantsOfKnowledge` | `anuvada_harvester.py` | [docs/anuvada.md](docs/anuvada.md) |
| **Shodhganga** | shodhganga.inflibnet.ac.in, ~579k PhD theses, ~14.8 TB | `shodhganga` | `shg_downloader.py`, `shg_harvest.py` | [docs/shodhganga.md](docs/shodhganga.md) |

### TDL
Scrapes articles by category, downloads PDFs and covers with IA-ready metadata, and uploads each file individually (no zip) so IA derive works. `tdl_multi_worker.py` runs several categories in parallel in a download → upload → delete pipeline; monitoring scripts (`monitor_harvest.py`, `monitor_realtime.py`) and progress-rebuild tools (`build_progress_from_ia_scraping.py`) support long unattended runs.

```bash
python tdl_downloader.py fetch --cat-id 20 --dir tdl_corpus --workers 5
python tdl_upload.py --cat Book --workers 3
python3 tdl_multi_worker.py --total-workers 12 --categories-parallel 3
python3 monitor_realtime.py --dashboard
```

### SBA
Incremental inventory → enrich → export → download → upload, backed by SQLite. Merges multipart books into one PDF, and blocks duplicates by checking several IA identifier forms plus a source-URL/Handle search (failing closed on lookup errors).

```bash
python3 sba_harvester.py inventory
python3 sba_harvester.py enrich --workers 3
python3 sba_harvester.py status
python3 sba_harvester.py download --limit 10          # preview; add --execute to run
```

SBA's [data policy](https://schoolbooksarchive.azimpremjiuniversity.edu.in/DataPolicy.html) restricts robotic harvesting of full items; use `--execute` only under the applicable archival-partner authorization.

### Anuvada Sampada
Inventories via OAI-PMH, enriches from EPrints JSON, reconciles every PDF against the existing IA collection, and downloads/uploads only what is missing. Preserves existing `apu.anuvadasampada.*` identifiers.

```bash
python3 anuvada_harvester.py inventory --full
python3 anuvada_harvester.py enrich --workers 4
python3 anuvada_harvester.py reconcile-ia
python3 anuvada_harvester.py status
```

### Shodhganga
Lists and manifests all theses, then `shg_harvest.py` automates metadata → download → merge → upload → delete per thesis, reconciled against the `shg.*` items already on IA (currently metadata-only placeholders). Items are created on IA only together with their PDF. Needs access to Shodhganga's port-8443 PDF proxy.

```bash
python3 shg_harvest.py preflight --dir shg_corpus
python3 shg_harvest.py plan --dir shg_corpus
python3 shg_harvest.py reconcile-ia --dir shg_corpus
python3 shg_harvest.py run --dir shg_corpus --limit 20           # preview; add --execute to run
```

## Documentation

| Document | Contents |
|----------|----------|
| [docs/tdl.md](docs/tdl.md) | TDL pipeline overview, commands, resume/upload behaviour, categories, output layout, metadata fields |
| [docs/tdl-harvest.md](docs/tdl-harvest.md) | Multi-worker harvest, rebuilding `upload_progress.json` from IA, monitoring, tuning, troubleshooting |
| [docs/scripts.md](docs/scripts.md) | Full flag reference for TDL scripts and logging |
| [docs/server-handoff.md](docs/server-handoff.md) | Moving upload/download work to a fresh server |
| [docs/sba.md](docs/sba.md) | SBA adapter |
| [docs/anuvada.md](docs/anuvada.md) | Anuvada Sampada adapter |
| [docs/shodhganga.md](docs/shodhganga.md) | Shodhganga scripts, state of collected data, harvest workflow |

## Repository layout

```
tdl_*.py                    TDL downloader, uploader, recovery, status, dashboard, multi-worker
sba_harvester.py            SBA adapter
anuvada_harvester.py        Anuvada Sampada adapter
shg_downloader.py, shg_harvest.py, shg_harvest.sh
                            Shodhganga downloader and automated harvest
analyze_state.py            Pending work per TDL category
build_progress_from_ia_scraping.py, rebuild_progress_from_ia.py, categorize_progress.py
                            Rebuild/annotate upload_progress.json from IA
monitor_harvest.py, monitor_realtime.py, monitor_harvest.sh
                            Harvest monitoring
start_harvest.sh, stop_harvest.sh, status.sh
                            Background harvest control
tests/                      Unit tests for the SBA, Anuvada and Shodhganga tools
docs/                       Detailed documentation
```

## Requirements

Python 3.7+ and:

```bash
pip install requests beautifulsoup4 tqdm internetarchive unidecode
pip install pypdf          # SBA / Shodhganga multipart merging
ia configure               # IA credentials
```

---

*Maintained by [ServantsOfKnowledge](https://github.com/servantsofknowledge).*
