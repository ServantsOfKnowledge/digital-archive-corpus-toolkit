# Digital Archive Corpus Toolkit

A comprehensive downloader, metadata scraper, and corpus builder for digital archival collections. Developed by **ServantsOfKnowledge** for digital archiving projects.

## Download Progress

**7,447 items downloaded · ~170 GB total · 305 GB free remaining**

| Category | Downloaded | Total | Size |
|----------|-----------:|------:|-----:|
| Audio | 124 | 125 | 12 GB |
| Author Bio | 196 | 196 | 23 MB |
| Book | 24 | 42,043 | 3.7 GB |
| Coin | 348 | 673 | 709 MB |
| Copper Plate | 192 | 192 | 188 MB |
| Document | 1,551 | 4,769 | 156 GB |
| Excavation | 55 | 55 | 73 MB |
| Historical Monument | 122 | 124 | 149 MB |
| Inscription | 1,341 | 1,922 | 2.3 GB |
| Map | 1,212 | 1,862 | 16 GB |
| Painting | 61 | 65 | 245 MB |
| Photograph | 49 | 53 | 283 MB |
| Pre-historic | 158 | 166 | 307 MB |
| Religious Place | 370 | 370 | 560 MB |
| Sculpture | 1,635 | 1,645 | 6.8 GB |
| Video | 10 | 10 | 1.2 GB |

*Largest categories (Periodical 29,951, Book 42,043) not yet started — awaiting additional storage.*

## Overview

This project downloads documents from online digital archives along with full bibliographic metadata, organized for research corpus use. It supports parallel downloads, resume capability, and automatically generates Internet Archive-ready metadata.

## Script: `tdl_downloader.py`

A Python script that scrapes, downloads, and indexes digital archive content.

### Features

- **Multi-category support**: Books, Periodicals, Palmleaf manuscripts, Documents, Audio, Video, Maps, Photographs, and more (18 categories)
- **Automatic metadata extraction**: Title, author, publisher, year, language, subject, keywords, etc.
- **Parallel downloading**: Configurable worker threads for concurrent downloads
- **Resume support**: Skip already-downloaded items on re-run
- **Progress tracking**: Persistent JSON progress file
- **Corpus builder**: Generate consolidated JSON and CSV indexes
- **IA-ready metadata**: Transforms metadata for Internet Archive upload format

### Categories

| ID | Name (Tamil) | Name (English) | Items |
|----|-------------|----------------|-------|
| 1 | ஒலி ஆவணம் | Audio | 125 |
| 2 | காணொலி ஆவணம் | Video | 10 |
| 3 | நில வரைபடம் | Map | 1,862 |
| 4 | ஒளிப்படம் | Photograph | 53 |
| 5 | ஆசிரியர் வாழ்க்கை குறிப்பு | Author Bio | 196 |
| 6 | தொல் பழங்காலம் | Pre-historic | 166 |
| 7 | அகழாய்வு | Excavation | 55 |
| 8 | கல்வெட்டு | Inscription | 1,922 |
| 9 | வழிபாட்டுத் தலம் | Religious Place | 370 |
| 10 | சிற்பம் | Sculpture | 1,645 |
| 11 | நாணயம் | Coin | 673 |
| 12 | செப்பேடு | Copper Plate | 192 |
| 13 | வரலாற்றுச் சின்னம் | Historical Monument | 124 |
| 14 | ஓவியம் | Painting | 65 |
| 20 | நூல் | Book | 42,043 |
| 21 | இதழ் | Periodical | 29,951 |
| 22 | சுவடி | Palmleaf | 5,387 |
| 27 | ஆவணம் | Document | 4,769 |

### Commands

#### List articles in a category

```bash
python tdl_downloader.py list --cat-id 20 --output listing.json
```

#### Download articles

```bash
# From a listing file
python tdl_downloader.py download --input listing.json --dir output_dir --workers 5

# From a text file of URLs
python tdl_downloader.py download --urls urls.txt --dir output_dir

# Resume interrupted download
python tdl_downloader.py download --input listing.json --dir output_dir --resume
```

#### Fetch (list + download) all articles in a category

```bash
python tdl_downloader.py fetch --cat-id 20 --dir tdl_output --workers 5
```

#### Build corpus index

```bash
python tdl_downloader.py corpus --dir tdl_verify --name tdl_corpus --csv
```

## Output Structure

```
output_dir/
├── {CategoryName}/
│   ├── {article_id}_{title}/
│   │   ├── metadata.json      # IA-ready metadata
│   │   ├── {pdf_filename}.pdf  # Document PDF
│   │   └── cover.jpg           # Cover image (if available)
│   └── ...
├── listing_{category}.json     # Article URL listing
├── progress.json               # Download progress tracker
├── corpus_index.json           # Consolidated corpus index
└── corpus_index.csv            # Corpus in CSV format
```

## Requirements

- Python 3.7+
- `requests`
- `beautifulsoup4`
- `tqdm`

Install: `pip install requests beautifulsoup4 tqdm`

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

*Maintained by [ServantsOfKnowledge](https://github.com/servantsofknowledge). Part of the ServantsOfKnowledge digital archiving initiative.*
