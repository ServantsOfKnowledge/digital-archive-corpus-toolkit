#!/usr/bin/env python3
"""
Digital Archive Corpus Toolkit — Downloader + Metadata Scraper (IA-ready)

Downloads documents with full metadata, organized for Internet Archive upload.
Supports parallel downloads, resume, and progress tracking.

Usage:
  # Fetch all books in a category and download them
  python tdl_downloader.py fetch --cat-id 20 --dir tdl_output --workers 5

  # List articles from a category (save URLs to file)
  python tdl_downloader.py list --cat-id 20 --output listing.json

  # Download from a saved listing
  python tdl_downloader.py download --input listing.json --dir tdl_output

  # Provide article URLs via text file (one URL per line)
  python tdl_downloader.py download --urls article_urls.txt --dir tdl_output

  # Resume interrupted download
  python tdl_downloader.py download --urls article_urls.txt --dir tdl_output --resume

Categories:
  20  நூல் (Book)
  27  ஆவணம் (Document)
  21  இதழ் (Periodical)
  22  சுவடி (Palmleaf)
  1   ஒலி ஆவணம் (Audio)
  2   காணொலி ஆவணம் (Video)
  3   நில வரைபடம் (Map)
  4   ஒளிப்படம் (Photograph)
  5   ஆசிரியர் வாழ்க்கை குறிப்பு (Author Bio)
  6   தொல் பழங்காலம் (Pre-historic)
  7   அகழாய்வு (Excavation)
  8   கல்வெட்டு (Inscription)
  9   வழிபாட்டுத் தலம் (Religious Place)
  10  சிற்பம் (Sculpture)
  11  நாணயம் (Coin)
  12  செப்பேடு (Copper Plate)
  13  வரலாற்றுச் சின்னம் (Historical Monument)
  14  ஓவியம் (Painting)
"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://tamildigitallibrary.in"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

CATEGORY_NAMES = {
    1: "Audio", 2: "Video", 3: "Map", 4: "Photograph",
    5: "AuthorBio", 6: "PreHistoric", 7: "Excavation", 8: "Inscription",
    9: "ReligiousPlace", 10: "Sculpture", 11: "Coin", 12: "CopperPlate",
    13: "HistoricalMonument", 14: "Painting",
    20: "Book", 21: "Periodical", 22: "Palmleaf", 27: "Document",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(s: str, maxlen: int = 200) -> str:
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r'\s+', " ", s).strip()
    if len(s) > maxlen:
        base, ext = os.path.splitext(s)
        s = base[:maxlen - len(ext)] + ext
    return s or "untitled"


def parse_article_id(url: str) -> str:
    m = re.search(r'/Articles/(?:[\w%]+-)?(\d{4,})', url)
    if m:
        return m.group(1)
    m = re.search(r'/Articles/(\d+)', url)
    if m:
        return m.group(1)
    # Non-numeric ID: use deterministic hash of URL
    return f"tdl_{hashlib.md5(url.encode()).hexdigest()[:6]}"


def fetch(url: str, session: requests.Session = None, **kwargs) -> requests.Response:
    s = session or SESSION
    for attempt in range(3):
        try:
            resp = s.get(url, timeout=kwargs.pop('timeout', 60), **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ConnectionError) as e:
            log.warning("Retry %d/3 for %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_search_page(cat_id: int = 20, sub_cat_id: str = "", inner_cat_id: str = "") -> dict:
    """Fetch the book-search-new page and extract CSRF token + hidden field defaults."""
    params = {"cat_id": str(cat_id)}
    if sub_cat_id:
        params["sub_cat_id"] = sub_cat_id
    if inner_cat_id:
        params["inner_cat_id"] = inner_cat_id
    r = SESSION.get(f"{BASE_URL}/book-search-new", params=params, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    def hidden_val(name):
        el = soup.find("input", {"name": name})
        return el["value"] if el else ""
    return {
        "csrf": hidden_val("csrf_test_name"),
        "item_per_page": hidden_val("item_per_page") or "24",
        "lazy_records": hidden_val("lazy_records") or "0",
        "header_cat_id": hidden_val("header_cat_id") or str(cat_id),
        "header_sub_cat_id": hidden_val("header_sub_cat_id") or sub_cat_id,
        "header_inner_cat_id": hidden_val("header_inner_cat_id") or inner_cat_id,
    }


# ---------------------------------------------------------------------------
# Step 1: List article URLs via the AJAX endpoint
# ---------------------------------------------------------------------------

def list_articles(cat_id: int = 20, sub_cat_id: str = "", inner_cat_id: str = "",
                  max_pages: int = None, items_per_page: int = 24) -> list:
    """
    Use the AJAX search endpoint to list all article URLs in a category.
    Pagination uses `limit` as page number (1-indexed, 0 = first page).
    Returns list of full article detail page URLs.
    """
    page_info = fetch_search_page(cat_id, sub_cat_id, inner_cat_id)
    csrf = page_info["csrf"]
    if not csrf:
        log.error("Could not get CSRF token from search page")
        return []

    articles = []
    seen_urls = set()
    page_num = 0  # 0-indexed page counter (0 = first page)

    log.info("Listing articles for category %d (max_pages=%s)", cat_id, max_pages or "all")

    while True:
        # Pagination scheme: limit=0→page1, limit=1→page1(bug), limit=n→page n (n≥2)
        if page_num == 0:
            limit_val = 0
        else:
            limit_val = page_num + 1  # page 1→limit=2, page 2→limit=3, etc.

        payload = {
            "process": "36",
            "searchtext": "",
            "category": "",
            "header_cat_id": page_info["header_cat_id"],
            "limit": str(limit_val),
            "current_page": str(page_num + 1),
            "sorting": "",
            "item_per_page": str(items_per_page),
            "author_search": "[]",
            "subject_search": "[]",
            "source_search": "[]",
            "language_search": "[]",
            "fformat_search": "[]",
            "resource_category_search": "[]",
            "resource_search": "[]",
            "location_of_site_search": "[]",
            "village_search": "[]",
            "ruler_search": "[]",
            "historic_period_search": "[]",
            "book_view": "",
            "checkval": "hide",
            "header_sub_cat_id": page_info["header_sub_cat_id"],
            "header_inner_cat_id": page_info["header_inner_cat_id"],
            "header_today_recommendations": "",
            "csrf_test_name": csrf,
        }

        try:
            resp = SESSION.post(
                f"{BASE_URL}/book-list-data-ajax-new",
                data=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Failed on page %d: %s", page_num + 1, e)
            break

        # Update CSRF token
        if "csrf" in data:
            csrf = data["csrf"]

        html_content = data.get("html", "")
        count_raw = data.get("count", "0")
        try:
            total_count = int(count_raw)
        except (ValueError, TypeError):
            total_count = 0

        if not html_content or total_count == 0:
            if page_num == 0:
                log.info("  Page 1: empty response (category %d may be empty)", cat_id)
            break

        soup = BeautifulSoup(html_content, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/Articles/" in href:
                full = urllib.parse.urljoin(BASE_URL, href.split("?")[0])
                full = full.rstrip("/")
                if full not in seen_urls:
                    seen_urls.add(full)
                    articles.append(full)
                    found += 1

        log.info("  Page %d: found %d articles (total: %d/%d)",
                 page_num + 1, found, len(articles), total_count)

        if max_pages and page_num + 1 >= max_pages:
            break
        if found == 0:
            break

        page_num += 1
        total_pages = (total_count + items_per_page - 1) // items_per_page
        if page_num >= total_pages:
            break

        time.sleep(0.5)

    log.info("Total articles found: %d", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Step 2: Extract metadata + file URLs from article detail page
# ---------------------------------------------------------------------------

def extract_article_data(article_url: str) -> dict:
    """Fetch article page and extract all metadata and download URLs."""
    resp = fetch(article_url)

    # If empty response, try numeric-only article ID
    if len(resp.text) < 500:
        path = urllib.parse.urlparse(article_url).path
        # Extract the leading numeric article ID from the slug
        m = re.match(r'/Articles/(\d{4,})', path)
        if m:
            alt = f"{BASE_URL}/Articles/{m.group(1)}"
            log.info("Empty page, trying: %s", alt)
            resp2 = fetch(alt)
            if len(resp2.text) >= 500:
                resp = resp2

    soup = BeautifulSoup(resp.text, "html.parser")

    data = {
        "article_url": article_url,
        "article_id": parse_article_id(article_url),
        "title": "",
        "category": "",
        "sub_category": "",
        "author": "",
        "publisher": "",
        "publication_year": "",
        "keywords": [],
        "document_location": "",
        "upload_institution": "",
        "upload_date": "",
        "views": "",
        "favourites": "",
        "download_count": "",
        "language": "",
        "subject": "",
        "source": "",
        "edition": "",
        "volume": "",
        "pages": "",
        "description": "",
        "pdf_url": "",
        "cover_url": "",
        "pdf_filename": "",
        "marc_url": "",
        "scrape_date": datetime.utcnow().isoformat(),
    }

    flip_input = soup.find("input", {"id": "flip_book_value"})
    if flip_input and flip_input.get("value"):
        data["pdf_url"] = urllib.parse.urljoin(BASE_URL, flip_input["value"])

    fname_input = soup.find("input", {"id": "book_file_name"})
    if fname_input and fname_input.get("value"):
        data["pdf_filename"] = fname_input["value"]

    if not data["pdf_url"]:
        obj = soup.find("object", type="application/pdf")
        if obj and obj.get("data"):
            data["pdf_url"] = urllib.parse.urljoin(BASE_URL, obj["data"])

    cover = soup.find("img", src=re.compile(r"cover|uploads.*cover", re.I))
    if cover and cover.get("src"):
        data["cover_url"] = urllib.parse.urljoin(BASE_URL, cover["src"])
    if not data["cover_url"]:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "cover" in src.lower() and src.endswith((".jpg", ".png", ".jpeg")):
                data["cover_url"] = urllib.parse.urljoin(BASE_URL, src)
                break

    meta_title = soup.find("input", {"id": "article-meta-title"})
    if meta_title and meta_title.get("value"):
        data["title"] = meta_title["value"].strip()
    else:
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            raw = re.sub(r"\s*[–-]\s*.*$", "", raw).strip()
            data["title"] = raw

    breadcrumb = soup.find("ul", class_=re.compile(r"breadcrumb|bread", re.I))
    if breadcrumb:
        items = breadcrumb.find_all("li")
        if len(items) >= 2:
            data["category"] = items[1].get_text(strip=True)
        if len(items) >= 3:
            data["sub_category"] = items[2].get_text(strip=True)

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            mapping = {
                "தலைப்பு": "title", "ஆசிரியர்": "author",
                "பதிப்பாளர்": "publisher", "பதிப்பு ஆண்டு": "publication_year",
                "குறிச்சொற்கள்": "keywords", "ஆவண இருப்பிடம்": "document_location",
                "மொழி": "language", "பொருள்": "subject", "மூலம்": "source",
                "பதிப்பு": "edition", "தொகுதி": "volume", "பக்கங்கள்": "pages",
                "பார்வைகள்": "views", "பதிவிறக்கங்கள்": "download_count",
                "பிடித்தவை": "favourites",
            }
            for tamil_key, eng_key in mapping.items():
                if tamil_key in label:
                    if eng_key == "keywords":
                        data[eng_key] = [v.strip() for v in value.split(",") if v.strip()]
                    else:
                        data[eng_key] = value
                    break

    for inp in soup.find_all("input", id=re.compile(r"input_keywords")):
        val = inp.get("value", "").strip()
        if val and val not in data["keywords"]:
            data["keywords"].append(val)

    marc_link = soup.find("a", href=re.compile(r"/Marc-Articles/", re.I))
    if marc_link and marc_link.get("href"):
        data["marc_url"] = urllib.parse.urljoin(BASE_URL, marc_link["href"])

    for a_tag in soup.find_all("a", onclick=True):
        onclick = a_tag["onclick"]
        m = re.search(r"fun_download_article_without_captcha\((\d+),(\d+),(\d+)\)", onclick)
        if m:
            data["cat_id"] = m.group(1)
            data["book_id"] = m.group(2)
        m = re.search(r"update_download_count_and_download_file\((\d+),(\d+),(\d+),'([^']+)','([^']+)'\)", onclick)
        if m:
            if not data.get("pdf_url"):
                data["pdf_url"] = urllib.parse.urljoin(BASE_URL, m.group(4))
            if not data.get("pdf_filename"):
                data["pdf_filename"] = m.group(5)

    if not data["title"]:
        data["title"] = f"untitled_{data['article_id']}"
    data["title"] = data["title"].strip().rstrip("-").strip()

    if not resp.text or len(resp.text) < 500:
        raise RuntimeError(f"Empty article page (HTTP {resp.status_code}, {len(resp.text)} bytes)")

    return data


# ---------------------------------------------------------------------------
# Step 3: Download file with resume support
# ---------------------------------------------------------------------------

def download_file(url: str, dest_path: Path, desc: str = "", timeout: int = 300) -> bool:
    if not url:
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    existing_size = dest_path.stat().st_size if dest_path.exists() else 0
    headers = {**HEADERS}

    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    try:
        resp = SESSION.get(url, headers=headers, stream=True, timeout=timeout)
        if resp.status_code == 416:
            log.info("  %s: already complete (%d bytes)", desc, existing_size)
            return True
        if resp.status_code in (200, 206):
            total = int(resp.headers.get("content-length", 0))
            if existing_size > 0 and resp.status_code == 200:
                existing_size = 0
            mode = "ab" if existing_size > 0 else "wb"
            total += existing_size
            with open(dest_path, mode) as f:
                with tqdm(
                    total=total, unit="B", unit_scale=True,
                    desc=desc[:50], initial=existing_size, leave=False,
                ) as pbar:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))
            return True
        elif resp.status_code == 403:
            log.warning("  %s: access denied (403)", desc)
            return False
        else:
            log.warning("  %s: HTTP %d", desc, resp.status_code)
            return False
    except (requests.RequestException, ConnectionError, OSError) as e:
        log.warning("  %s: download error: %s", desc, e)
        return False


# ---------------------------------------------------------------------------
# Internet Archive Metadata Mapping
# ---------------------------------------------------------------------------

def transform_to_ia_metadata(data: dict) -> dict:
    keywords = data.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    subject = data.get("subject", "")
    if subject:
        keywords = keywords + [subject] if isinstance(keywords, list) else [subject]

    description = data.get("description", "")
    if not description:
        parts = []
        for label, val in [("Category", data["category"]), ("Sub-category", data["sub_category"]),
                           ("Author", data["author"]), ("Publisher", data["publisher"]),
                           ("Year", data["publication_year"]), ("Source", data["document_location"])]:
            if val:
                parts.append(f"{label}: {val}")
        if keywords:
            parts.append(f"Keywords: {', '.join(keywords) if isinstance(keywords, list) else keywords}")
        description = "; ".join(parts)

    year = data.get("publication_year", "")
    if re.match(r'^\d{4}$', year):
        year = int(year)
    else:
        year = ""

    return {k: v for k, v in {
        "title": data.get("title", ""),
        "description": description,
        "subject": keywords if keywords else "",
        "creator": data.get("author", ""),
        "publisher": data.get("publisher", ""),
        "date": str(year) if year else "",
        "language": data.get("language", "tamil"),
        "source": data.get("document_location", "") or data.get("source", ""),
        "edition": data.get("edition", ""),
        "volume": data.get("volume", ""),
        "pages": data.get("pages", ""),
        "contributor": data.get("upload_institution", "Tamil Virtual Academy"),
        "identifier": data.get("article_id", ""),
        "original_url": data.get("article_url", ""),
        "cover_url": data.get("cover_url", ""),
        "pdf_url": data.get("pdf_url", ""),
        "collection": "tamildigitallibrary",
        "mediatype": "texts",
        "scrape_date": data.get("scrape_date", ""),
    }.items() if v}


# ---------------------------------------------------------------------------
# Process one article
# ---------------------------------------------------------------------------

def process_article(article_url: str, output_dir: Path,
                    download_pdf: bool = True, download_cover: bool = True,
                    cat_name: str = "downloads") -> dict:
    try:
        data = extract_article_data(article_url)
    except Exception as e:
        log.error("Failed to process %s: %s", article_url, e)
        return {"url": article_url, "status": "error", "error": str(e)}

    art_id = data["article_id"]
    safe_title = safe_filename(data["title"] or art_id, 80)
    art_dir = output_dir / cat_name / f"{art_id}_{safe_title}"

    # Reuse existing directory for this article_id
    cat_dir = output_dir / cat_name
    if cat_dir.exists():
        for existing_dir in cat_dir.iterdir():
            if existing_dir.is_dir() and existing_dir.name.startswith(f"{art_id}_"):
                art_dir = existing_dir
                break

    art_dir.mkdir(parents=True, exist_ok=True)

    meta_ia = transform_to_ia_metadata(data)
    meta_file = art_dir / "metadata.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta_ia, f, ensure_ascii=False, indent=2)

    result = {
        "url": article_url, "id": art_id, "title": data["title"],
        "dir": str(art_dir), "status": "metadata_ok",
        "pdf": False, "cover": False,
    }

    if download_pdf and data.get("pdf_url"):
        pdf_ext = os.path.splitext(urllib.parse.urlparse(data["pdf_url"]).path)[1] or ".pdf"
        pdf_name = data.get("pdf_filename") or f"{art_id}{pdf_ext}"
        pdf_path = art_dir / safe_filename(pdf_name)
        ok = download_file(data["pdf_url"], pdf_path, desc=data["title"][:60])
        if ok:
            result["pdf"] = True
            result["pdf_file"] = str(pdf_path)
            result["pdf_size"] = pdf_path.stat().st_size if pdf_path.exists() else 0
            result["status"] = "complete"
        else:
            result["status"] = "pdf_failed"

    if download_cover and data.get("cover_url"):
        ext = os.path.splitext(urllib.parse.urlparse(data["cover_url"]).path)[1] or ".jpg"
        cover_path = art_dir / f"cover{ext}"
        ok = download_file(data["cover_url"], cover_path, desc=f"cover:{data['title'][:40]}")
        if ok:
            result["cover"] = True

    if result.get("pdf_file") and os.path.exists(result["pdf_file"]):
        sz = os.path.getsize(result["pdf_file"])
        if sz > 0 and not result.get("pdf"):
            result["status"] = "complete"
            result["pdf_size"] = sz
            result["pdf"] = True

    return result


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"articles": {}, "completed": [], "failed": [], "skipped": [], "timestamp": ""}


def save_progress(path: Path, state: dict):
    state["timestamp"] = datetime.utcnow().isoformat()
    with open(path, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Load article URLs from various input formats
# ---------------------------------------------------------------------------

def load_article_urls(source) -> list:
    if isinstance(source, str) and '\n' not in source and not os.path.isfile(source):
        return [source.strip()]

    if os.path.isfile(source):
        with open(source) as f:
            content = f.read().strip()
        if content.startswith("{"):
            try:
                data = json.loads(content)
                if "articles" in data:
                    a = data["articles"]
                    return a if isinstance(a, list) else []
            except json.JSONDecodeError:
                pass
        urls = []
        for line in content.splitlines():
            line = line.strip()
            if line and line.startswith("http") and "/Articles/" in line:
                urls.append(line)
        if urls:
            return urls

    return []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(args):
    log.info("Listing articles for category %d...", args.cat_id)
    articles = list_articles(
        cat_id=args.cat_id,
        sub_cat_id=args.sub_cat_id or "",
        inner_cat_id=args.inner_cat_id or "",
        max_pages=args.max_pages,
        items_per_page=24,
    )
    output = {
        "source_cat_id": args.cat_id,
        "sub_cat_id": args.sub_cat_id or "",
        "inner_cat_id": args.inner_cat_id or "",
        "total": len(articles),
        "articles": articles,
        "timestamp": datetime.utcnow().isoformat(),
    }
    with open(args.output, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("Saved %d article URLs to %s", len(articles), args.output)


def cmd_download(args):
    output_dir = Path(args.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = output_dir / "progress.json"
    state = load_progress(progress_file)
    completed_ids = set(state.get("completed", []))
    failed_urls = {f["url"] for f in state.get("failed", [])}

    articles = []
    if args.urls:
        articles = load_article_urls(args.urls)
    if not articles and args.input:
        articles = load_article_urls(args.input)

    if not articles:
        log.error("No article URLs found! Use --urls FILE, --input JSON, or --cat-id")
        sys.exit(1)

    log.info("Loaded %d article URLs", len(articles))
    if args.resume:
        log.info("Already completed: %d, failed: %d", len(completed_ids), len(failed_urls))

    to_process = []
    for url in articles:
        art_id = parse_article_id(url)
        if args.resume and (art_id in completed_ids or url in failed_urls):
            continue
        to_process.append(url)

    if not to_process:
        log.info("All done! Nothing to process.")
        return

    cat_name = getattr(args, 'cat_name', None) or "downloads"
    log.info("Downloading %d articles with %d workers into %s/", len(to_process), args.workers, cat_name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_article, url, output_dir, True, True, cat_name): url for url in to_process}
        with tqdm(total=len(to_process), desc="Articles", unit="art") as pbar:
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                    if result:
                        art_id = result.get("id", parse_article_id(url))
                        state["articles"][art_id] = result
                        if result.get("status") == "complete":
                            if art_id not in completed_ids:
                                state["completed"].append(art_id)
                                completed_ids.add(art_id)
                        else:
                            state["failed"].append({"id": art_id, "url": url, "status": result.get("status")})
                        save_progress(progress_file, state)
                except Exception as e:
                    art_id = parse_article_id(url)
                    log.error("Error: %s: %s", url, e)
                    state["failed"].append({"id": art_id, "url": url, "error": str(e)})
                    save_progress(progress_file, state)
                pbar.update(1)

    completed = len(state.get("completed", []))
    failed = len(state.get("failed", []))
    log.info("Done! Completed: %d, Failed: %d", completed, failed)


def cmd_corpus(args):
    """Build a consolidated corpus index from downloaded articles."""
    output_dir = Path(args.dir)

    corpus = []
    total_size = 0
    missing_pdf = 0

    # Scan category subdirs (Book/, Video/, etc.) + legacy downloads/
    search_dirs = sorted(d for d in output_dir.iterdir()
                         if d.is_dir() and not d.name.startswith('.'))

    for cat_dir in search_dirs:
        # Each cat_dir contains article subdirs
        for art_dir in sorted(cat_dir.iterdir()):
            if not art_dir.is_dir():
                continue
            meta_file = art_dir / "metadata.json"
            if not meta_file.exists():
                continue
            meta = json.loads(meta_file.read_text(encoding="utf-8"))

            pdfs = sorted(art_dir.glob("*.pdf"))
            covers = sorted(art_dir.glob("cover.*"))

            entry = {
                "id": meta.get("identifier", ""),
                "title": meta.get("title", ""),
                "creator": meta.get("creator", ""),
                "publisher": meta.get("publisher", ""),
                "date": meta.get("date", ""),
                "language": meta.get("language", ""),
                "subject": meta.get("subject", ""),
                "description": meta.get("description", ""),
                "pages": meta.get("pages", ""),
                "source": meta.get("source", ""),
                "original_url": meta.get("original_url", ""),
                "pdf_file": str(pdfs[0].relative_to(output_dir)) if pdfs else "",
                "pdf_size": pdfs[0].stat().st_size if pdfs else 0,
                "cover_file": str(covers[0].relative_to(output_dir)) if covers else "",
            }
            if not pdfs:
                missing_pdf += 1
            total_size += entry["pdf_size"]
            corpus.append(entry)

    output = {
        "corpus_name": args.name or "tdl_corpus",
        "source": "Digital Archive (online)",
        "category": CATEGORY_NAMES.get(args.cat_id, f"cat_{args.cat_id}"),
        "total_documents": len(corpus),
        "total_size_bytes": total_size,
        "missing_pdfs": missing_pdf,
        "generated": datetime.utcnow().isoformat(),
        "documents": corpus,
    }

    corpus_file = output_dir / f"{args.name or 'corpus'}.json"
    corpus_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Corpus saved: %s (%d documents, %.1f MB)", corpus_file, len(corpus), total_size / 1e6)

    if args.csv:
        import csv
        csv_file = output_dir / f"{args.name or 'corpus'}.csv"
        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "id", "title", "creator", "publisher", "date", "language",
                "pages", "source", "pdf_file", "pdf_size", "cover_file",
            ])
            w.writeheader()
            for doc in corpus:
                w.writerow({k: doc.get(k, "") for k in w.fieldnames})
        log.info("CSV saved: %s", csv_file)


def cmd_fetch(args):
    """List articles via AJAX, then download all."""
    cat_name = CATEGORY_NAMES.get(args.cat_id, f"cat_{args.cat_id}")
    output_dir = Path(args.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    listing_file = output_dir / f"listing_{cat_name}.json"

    log.info("Step 1: Listing articles for category %d...", args.cat_id)
    articles = list_articles(
        cat_id=args.cat_id,
        sub_cat_id=args.sub_cat_id or "",
        inner_cat_id=args.inner_cat_id or "",
        max_pages=args.max_pages,
        items_per_page=24,
    )

    listing = {
        "source_cat_id": args.cat_id,
        "sub_cat_id": args.sub_cat_id or "",
        "inner_cat_id": args.inner_cat_id or "",
        "total": len(articles),
        "articles": articles,
        "timestamp": datetime.utcnow().isoformat(),
    }
    with open(listing_file, "w") as f:
        json.dump(listing, f, ensure_ascii=False, indent=2)
    log.info("Found %d articles, saved to %s", len(articles), listing_file)

    if not articles:
        log.warning("No articles found!")
        return

    log.info("Step 2: Downloading %d articles into %s/", len(articles), cat_name)
    args.input = str(listing_file)
    args.urls = None
    args.cat_name = cat_name
    cmd_download(args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Digital Archive Corpus Toolkit — Downloader + Metadata Scraper (IA-ready)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List article URLs via AJAX search endpoint")
    p_list.add_argument("--cat-id", type=int, default=20, help="Category ID (default: 20=Book)")
    p_list.add_argument("--sub-cat-id", help="Sub-category ID (optional)")
    p_list.add_argument("--inner-cat-id", help="Inner category ID (optional)")
    p_list.add_argument("--output", default="listing.json", help="Output JSON file")
    p_list.add_argument("--max-pages", type=int, help="Max pages to fetch")

    p_down = sub.add_parser("download", help="Download articles and extract metadata")
    p_down.add_argument("--urls", help="Article URL(s): file (one/line) or single URL")
    p_down.add_argument("--input", help="JSON listing from 'list' command")
    p_down.add_argument("--dir", default="tdl_output", help="Output directory")
    p_down.add_argument("--workers", type=int, default=5, help="Parallel downloads")
    p_down.add_argument("--resume", action="store_true", help="Resume interrupted download")
    p_down.add_argument("--cat-name", default="downloads", help="Category subdirectory name")

    p_fetch = sub.add_parser("fetch", help="List articles by category and download them all")
    p_fetch.add_argument("--cat-id", type=int, default=20, help="Category ID (default: 20=Book)")
    p_fetch.add_argument("--sub-cat-id", help="Sub-category ID")
    p_fetch.add_argument("--inner-cat-id", help="Inner category ID")
    p_fetch.add_argument("--dir", default="tdl_output", help="Output directory")
    p_fetch.add_argument("--workers", type=int, default=5, help="Parallel downloads")
    p_fetch.add_argument("--max-pages", type=int, help="Max search pages to list")
    p_fetch.add_argument("--resume", action="store_true", help="Resume download")

    p_corpus = sub.add_parser("corpus", help="Build consolidated corpus index from downloads")
    p_corpus.add_argument("--dir", default="tdl_output", help="Output directory with downloads/")
    p_corpus.add_argument("--name", default="corpus", help="Corpus name (default: corpus)")
    p_corpus.add_argument("--cat-id", type=int, default=20, help="Category ID for labeling")
    p_corpus.add_argument("--csv", action="store_true", help="Also export CSV")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "list":
        cmd_list(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "corpus":
        cmd_corpus(args)


if __name__ == "__main__":
    main()
