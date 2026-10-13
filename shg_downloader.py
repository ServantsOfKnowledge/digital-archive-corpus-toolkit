#!/usr/bin/env python3
"""
Shodhganga Corpus Toolkit — Downloader + Metadata Scraper + IA Uploader

Scrapes all items from https://shodhganga.inflibnet.ac.in, captures full
metadata, downloads & merges PDFs, and uploads to Internet Archive with
configurable identifier prefix. Supports parallel workers, pause/resume,
and staged retry.

NOTE: Shodhganga serves PDFs via a custom proxy on port 8443. You MUST
have access to shodhganga.inflibnet.ac.in:8443 from your network (e.g.
within India / INFLIBNET network). Set SHG_PDF_PORT=443 to bypass the
redirect (the consent page will block download, so this only works for
metadata).

Usage:
  export SHG_PREFIX=shodhganga
  export SHG_IA_COLLECTION=shodhganga

  # Step 1: List all item URLs from browse
  python shg_downloader.py list --output listing.json

  # Step 2: Build manifest (fetches metadata, no PDF downloads)
  python shg_downloader.py manifest --input listing.json --dir shg_corpus --workers 10

  # Step 3: Run pipeline (per-item: metadata -> download PDFs -> merge -> upload -> cleanup)
  python shg_downloader.py pipeline --dir shg_corpus --workers 5

  # Step 4: Check status
  python shg_downloader.py status --dir shg_corpus -v
"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
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

BASE_URL = "https://shodhganga.inflibnet.ac.in"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# PDF download port (Shodhganga serves PDFs via port 8443)
# Set SHG_PDF_PORT=443 to bypass the 8443 redirect (may not work)
PDF_PORT = int(os.environ.get("SHG_PDF_PORT", "443"))

# Set SHG_NO_VERIFY_SSL=1 (or --no-verify-ssl flag) to disable SSL certificate
# verification for the PDF proxy on port 8443. Useful when the proxy has a
# self-signed or expired certificate (common on macOS with strict SSL).
PDF_SSL_VERIFY = os.environ.get("SHG_NO_VERIFY_SSL", "").lower() not in ("1", "true", "yes")

MAX_WORKERS_DOWNLOAD = 10
MAX_WORKERS_UPLOAD = 10
_upload_failed_lock = threading.Lock()
_progress_save_lock = threading.Lock()
_upload_semaphore = threading.BoundedSemaphore(2)

STAGE_MANIFESTED = "manifested"
STAGE_METADATA_SAVED = "metadata_saved"
STAGE_PDF_DOWNLOADED = "pdf_downloaded"
STAGE_MERGED = "merged"
STAGE_UPLOADED = "uploaded"
STAGE_CLEANED = "cleaned"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path: str = None) -> dict:
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load config file %s: %s", config_path, e)
    return {}


def get_config(key: str, default=None, config: dict = None, args=None):
    if args is not None:
        arg_val = getattr(args, key, None)
        if arg_val is not None:
            return arg_val
    if config and key in config:
        return config[key]
    env_key = f"SHG_{key.upper()}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        if isinstance(default, bool):
            return env_val.lower() in ("1", "true", "yes")
        if isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                return default
        return env_val
    return default


def clamp_workers(val: int, max_val: int) -> int:
    return max(1, min(val, max_val))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(s: str, maxbytes: int = 200) -> str:
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r'\s+', " ", s).strip()
    encoded = s.encode("utf-8")
    if len(encoded) > maxbytes:
        truncated = encoded[:maxbytes]
        s = truncated.decode("utf-8", errors="ignore")
    return s or "untitled"


def parse_handle_id(url: str) -> str:
    m = re.search(r'/handle/(\d+\.?\d*/\d+)', url)
    if m:
        return m.group(1)
    return f"shg_{hashlib.md5(url.encode()).hexdigest()[:8]}"


def parse_handle_suffix(handle: str) -> str:
    m = re.search(r'/(\d+)$', handle)
    if m:
        return m.group(1)
    return handle


def human_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 ** 2:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 ** 3:
        return f"{bytes_val / 1024 ** 2:.1f} MB"
    else:
        return f"{bytes_val / 1024 ** 3:.2f} GB"


def parse_size_text(size_text: str) -> int:
    if not size_text or not size_text.strip():
        return 0
    size_text = size_text.strip().upper()
    m = re.match(r'([\d.]+)\s*(KB|MB|GB|B)?', size_text)
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2) or "B"
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return int(val * multipliers.get(unit, 1))


class BotChallengeError(RuntimeError):
    """The site served a proof-of-work/bot challenge page instead of content."""


def is_bot_challenge(resp) -> bool:
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        return False
    head = resp.text[:4000].lower()
    return "anubis" in head and ("not a bot" in head or "proof-of-work" in head or "challenge" in head)


def fetch(url: str, session: requests.Session = None, **kwargs) -> requests.Response:
    s = session or SESSION
    timeout = kwargs.pop('timeout', 60)
    for attempt in range(3):
        try:
            resp = s.get(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            if is_bot_challenge(resp):
                raise BotChallengeError(
                    f"Bot challenge (Anubis) served instead of content for {url}; "
                    "the site requires an authorized client or access arrangement")
            return resp
        except BotChallengeError:
            raise
        except (requests.RequestException, ConnectionError) as e:
            log.warning("Retry %d/3 for %s: %s", attempt + 1, url, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}")


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning("Corrupt progress file %s, starting fresh: %s", path, e)
            backup = path.with_suffix(".json.corrupt")
            try:
                path.rename(backup)
            except OSError:
                pass
    return {
        "items": {},
        "total_size_bytes": 0,
        "downloaded_bytes": 0,
        "uploaded_bytes": 0,
        "stages": {
            STAGE_MANIFESTED: [],
            STAGE_METADATA_SAVED: [],
            STAGE_PDF_DOWNLOADED: [],
            STAGE_MERGED: [],
            STAGE_UPLOADED: [],
            STAGE_CLEANED: [],
        },
        "timestamp": "",
    }


def save_progress(path: Path, state: dict):
    state["timestamp"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        with open(path, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def set_item_stage(state: dict, hs: str, stage: str):
    if not hs:
        return
    all_stages = [STAGE_MANIFESTED, STAGE_METADATA_SAVED, STAGE_PDF_DOWNLOADED,
                  STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED]
    for s in all_stages:
        if hs in state["stages"].get(s, []):
            state["stages"][s].remove(hs)
    state["stages"].setdefault(stage, []).append(hs)


def get_item_stage(state: dict, hs: str) -> str:
    for s in [STAGE_CLEANED, STAGE_UPLOADED, STAGE_MERGED,
              STAGE_PDF_DOWNLOADED, STAGE_METADATA_SAVED, STAGE_MANIFESTED]:
        if hs in state["stages"].get(s, []):
            return s
    return None


# ---------------------------------------------------------------------------
# Step 1: List all items from browse
# ---------------------------------------------------------------------------

# Pool of User-Agents to rotate through
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
]


def _fresh_session():
    """Create a new session with a random User-Agent to avoid rate limiting."""
    s = requests.Session()
    ua = random.choice(USER_AGENTS)
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return s


def _is_connect_timeout(exc: Exception) -> bool:
    """Return True if the exception is a TCP connect timeout (not a read timeout)."""
    msg = str(exc).lower()
    return "connecttimeouterror" in msg or "connect timeout" in msg


def _classify_download_error(exc: Exception) -> str:
    """Return a short human-readable reason for a download exception."""
    from requests.exceptions import SSLError, ConnectionError as ReqConnError, Timeout
    if isinstance(exc, SSLError):
        return (
            f"SSL error — the server's certificate was rejected by macOS. "
            f"Try: --no-verify-ssl  (or set SHG_NO_VERIFY_SSL=1)"
        )
    msg = str(exc).lower()
    if "connection refused" in msg:
        return f"port refused — the server actively rejected the connection"
    if "connect timeout" in msg or "connecttimeouterror" in msg:
        return f"connect timeout — port may be firewalled"
    if "read timeout" in msg:
        return f"read timeout — connection established but server stopped sending data"
    if "remotedisconnected" in msg or "connection reset" in msg:
        return f"connection reset — server closed the connection mid-transfer"
    return str(exc)


def _fetch_browse_page(url: str, session, offset: int, retries: int = 3,
                       connect_timeout: int = 15, read_timeout: int = 60):
    """Fetch a browse page with retry, new session fallback, and adaptive backoff.

    Connect timeouts (server unreachable) use a short wait — the server being
    down means waiting 40 s does nothing useful. Rate-limit / server errors
    keep the longer exponential backoff.
    """
    last_err = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=(connect_timeout, read_timeout))
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            is_conn = _is_connect_timeout(e)
            if is_conn:
                # Connect timeout: server isn't answering. Short wait, one retry.
                wait = 3.0 + random.uniform(0, 2)
                if attempt == 0:
                    log.warning(
                        "  attempt %d/%d for offset=%d: connect timeout (waiting %.0fs)",
                        attempt + 1, retries, offset, wait,
                    )
                else:
                    # Second connect timeout in a row — give up quickly
                    log.warning(
                        "  attempt %d/%d for offset=%d: connect timeout again, giving up",
                        attempt + 1, retries, offset,
                    )
                    raise last_err
            else:
                # Rate-limit / server error: exponential backoff as before
                wait = 10 * (2 ** attempt) + random.uniform(0, 5)
                log.warning(
                    "  attempt %d/%d for offset=%d failed: %s (waiting %.0fs)",
                    attempt + 1, retries, offset, e, wait,
                )
            time.sleep(wait)
            session = _fresh_session()
    raise last_err


def list_items_from_browse(rpp: int = 100, max_items: int = None,
                            resume_path: str = None,
                            session_refresh_interval: int = 500,
                            base_delay: float = 2.0,
                            connect_timeout: int = 15) -> list:
    items = []
    seen_urls = set()
    offset = 0
    total_results = None
    request_count = 0

    log.info("Listing items from Shodhganga browse (rpp=%d, max_items=%s, refresh=%d)",
             rpp, max_items or "all", session_refresh_interval)

    # Resume from existing listing file
    start_offset = 0
    community_mode = False
    community_index = 0
    if resume_path and os.path.isfile(resume_path):
        try:
            with open(resume_path) as f:
                existing = json.load(f)
            existing_items = existing.get("items", [])
            items = list(existing_items)
            for url in items:
                seen_urls.add(url)
            community_mode = existing.get("community_mode", False)
            community_index = existing.get("community_index", 0)
            start_offset = existing.get("last_offset", 0)
            if start_offset == 0 and len(items) > 0 and not community_mode:
                start_offset = (len(items) // rpp) * rpp
                log.info("  No saved last_offset, estimating from item count: %d",
                         start_offset)
            offset = start_offset
            total_results = existing.get("total_results", None)
            log.info("Resuming from offset=%d with %d existing items%s",
                     offset, len(items),
                     " (community mode)" if community_mode else "")
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not load existing listing for resume: %s", e)

    def save_snapshot(cmode: bool = None, cindex: int = None):
        if not resume_path:
            return
        snapshot = {
            "source": "shodhganga.inflibnet.ac.in",
            "total": len(items),
            "total_results": total_results,
            "last_offset": offset,
            "community_mode": cmode if cmode is not None else community_mode,
            "community_index": cindex if cindex is not None else community_index,
            "items": items,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        tmp = resume_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, resume_path)
        except OSError as e:
            log.warning("Failed to save snapshot: %s", e)

    def do_community_browse(community_handle: str):
        """Fallback: list items within a specific community/collection."""
        nonlocal total_results
        local_offset = 0
        local_items = []
        local_seen = set()
        local_total = None
        community_code = community_handle.split("/")[-1] if community_handle else "root"
        session = _fresh_session()
        local_req_count = 0

        log.info("  Listing community %s (%s)",
                 community_handle, community_code)

        while True:
            if community_handle:
                url = (f"{BASE_URL}{community_handle}/browse?type=dateaccessioned"
                       f"&sort_by=3&order=DESC&rpp={rpp}&etal=-1&offset={local_offset}")
            else:
                url = (f"{BASE_URL}/browse?type=dateaccessioned&sort_by=3&order=DESC"
                       f"&rpp={rpp}&etal=-1&offset={local_offset}")

            local_req_count += 1
            if local_req_count > 0 and local_req_count % session_refresh_interval == 0:
                session = _fresh_session()

            try:
                resp = _fetch_browse_page(url, session, local_offset,
                                          connect_timeout=connect_timeout)
            except Exception as e:
                log.warning("  Community %s failed at offset=%d: %s",
                            community_code, local_offset, e)
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            if local_total is None:
                text = soup.get_text()
                m = re.search(r'of ([\d,]+)', text)
                if m:
                    local_total = int(m.group(1).replace(",", ""))
                    log.info("  Community %s: %d items", community_code, local_total)

            found = 0
            for a in soup.select('td[headers="t2"] a'):
                href = a.get("href", "")
                if "/handle/" in href:
                    full = urllib.parse.urljoin(BASE_URL, href.split("?")[0])
                    full = full.rstrip("/")
                    if full not in local_seen and full not in seen_urls:
                        local_seen.add(full)
                        seen_urls.add(full)
                        local_items.append(full)
                        found += 1

            if not found:
                break

            local_offset += rpp
            if local_total and local_offset >= local_total:
                break

            time.sleep(random.uniform(base_delay * 0.75, base_delay * 1.5))

        log.info("  Community %s: %d items found", community_code, len(local_items))
        return local_items

    def _run_community_listing():
        """Process all communities from university_listing.json, with resume support."""
        nonlocal items, community_mode, community_index, offset
        community_list_path = os.path.join(
            os.path.dirname(resume_path) if resume_path else ".",
            "university_listing.json")
        if os.path.isfile(community_list_path):
            with open(community_list_path) as cf:
                communities = json.load(cf).get("items", [])
        else:
            log.warning("No community list available. "
                        "Run 'list-communities' first for fallback.")
            return

        processed_file = (Path(resume_path).parent / "processed_communities.json"
                          if resume_path else None)
        processed = set()
        if processed_file and processed_file.exists():
            try:
                processed = set(json.loads(processed_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

        # If resuming, skip to the saved community_index
        start_i = community_index
        for i in range(start_i, len(communities)):
            ch, ct = communities[i]
            if ch in processed:
                continue
            log.info("  Community %d/%d: %s (%s)", i + 1, len(communities), ct, ch)
            try:
                citems = do_community_browse(ch)
                items.extend(citems)
            except Exception as ce:
                log.warning("  Community %s failed: %s", ch, ce)
            processed.add(ch)
            community_index = i + 1
            community_mode = True
            if processed_file:
                processed_file.write_text(json.dumps(list(processed)))
            save_snapshot()
            time.sleep(random.uniform(2, 4))

        community_mode = False
        community_index = 0

    # Main browsing session
    session = _fresh_session()

    # If resuming in community mode, skip global browse entirely
    if community_mode:
        log.info("Skipping global browse — resuming community-based listing from index %d",
                 community_index)
        _run_community_listing()
        save_snapshot()
        return items

    while True:
        url = (f"{BASE_URL}/browse?type=dateaccessioned&sort_by=3&order=DESC"
               f"&rpp={rpp}&etal=-1&offset={offset}")

        request_count += 1
        if request_count > 0 and request_count % session_refresh_interval == 0:
            log.info("  Refreshing session at request %d", request_count)
            session = _fresh_session()

        try:
            resp = _fetch_browse_page(url, session, offset,
                                      connect_timeout=connect_timeout)
        except Exception as e:
            log.error("All retries exhausted at offset=%d: %s", offset, e)
            save_snapshot()
            log.info("Falling back to community-based listing...")
            _run_community_listing()
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        if total_results is None:
            text = soup.get_text()
            m = re.search(r'of ([\d,]+)', text)
            if m:
                total_results = int(m.group(1).replace(",", ""))
                log.info("Total results: %d", total_results)

        found = 0
        total_on_page = 0
        for a in soup.select('td[headers="t2"] a'):
            href = a.get("href", "")
            if "/handle/" in href:
                full = urllib.parse.urljoin(BASE_URL, href.split("?")[0])
                full = full.rstrip("/")
                total_on_page += 1
                if full not in seen_urls:
                    seen_urls.add(full)
                    items.append(full)
                    found += 1

        if not found:
            if total_on_page > 0:
                # All items on this page are already seen — skip past it
                log.info("  offset=%-6d: all %d items already seen, skipping page", offset, total_on_page)
                offset += rpp
                if total_results and offset >= total_results:
                    save_snapshot()
                    break
                continue
            save_snapshot()
            break

        total_so_far = len(items)
        log.info("  offset=%-6d: found %d items (total: %d)", offset, found, total_so_far)

        if max_items and total_so_far >= max_items:
            items = items[:max_items]
            save_snapshot()
            break

        offset += rpp
        if total_results and offset >= total_results:
            save_snapshot()
            break

        # Save every 50 offsets
        if offset % (rpp * 50) == 0:
            save_snapshot()

        # Random delay to avoid rate limiting
        time.sleep(random.uniform(base_delay * 0.75, base_delay * 1.5))

    save_snapshot()
    log.info("Total items found: %d", len(items))
    return items


# ---------------------------------------------------------------------------
# Step 2: Extract metadata from item page
# ---------------------------------------------------------------------------

def extract_metadata(item_url: str) -> dict:
    resp = fetch(item_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    handle_id = parse_handle_id(item_url)
    handle_suffix = parse_handle_suffix(handle_id)

    data = {
        "item_url": item_url,
        "handle": handle_id,
        "handle_suffix": handle_suffix,
        "title": "",
        "researcher": "",
        "guides": [],
        "abstract": "",
        "subjects": [],
        "language": "",
        "type": "",
        "date_issued": "",
        "date_accessioned": "",
        "date_available": "",
        "uri": "",
        "publisher": "",
        "department": "",
        "university": "",
        "pdf_files": [],
        "pdf_size_bytes": 0,
        "pdf_size_display": "",
        "scrape_date": datetime.now(timezone.utc).isoformat(),
    }

    def meta_content(name, attr="name"):
        tag = soup.find("meta", {attr: name})
        return tag.get("content", "") if tag else ""

    data["title"] = meta_content("DC.title") or meta_content("citation_title") or ""

    abstract = meta_content("DCTERMS.abstract")
    if abstract and abstract != "newline":
        data["abstract"] = abstract
    data["language"] = meta_content("DC.language") or ""
    data["type"] = meta_content("DC.type") or ""
    data["date_issued"] = meta_content("DC.date") or ""
    data["date_accessioned"] = meta_content("DCTERMS.dateAccepted") or ""
    data["date_available"] = meta_content("DCTERMS.available") or ""
    data["uri"] = meta_content("DC.identifier") or meta_content("citation_abstract_html_url") or ""
    data["publisher"] = meta_content("DC.publisher") or ""

    for tag in soup.find_all("meta", {"name": "DC.subject"}):
        val = tag.get("content", "").strip()
        if val and val not in data["subjects"]:
            data["subjects"].append(val)

    data["researcher"] = meta_content("DC.creator") or meta_content("citation_author") or ""

    guide = meta_content("DC.contributor")
    if guide:
        data["guides"] = [g.strip() for g in guide.split(";") if g.strip()]

    if not data["title"]:
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(strip=True)
            raw = re.sub(r"\s*[–-]\s*Shodhganga@INFLIBNET.*$", "", raw).strip()
            data["title"] = raw

    if not data["title"]:
        data["title"] = f"untitled_{data['handle_suffix']}"

    # Extract all PDF files from the files panel
    files_panel = soup.find("div", class_="panel panel-info")
    if files_panel:
        panel_text = files_panel.get_text()
        if "Files in This Item" in panel_text:
            for row in files_panel.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 4:
                    file_link = cells[0].find("a")
                    if file_link and "/bitstream/" in file_link.get("href", ""):
                        file_url = urllib.parse.urljoin(BASE_URL, file_link["href"])
                        file_name = file_link.get_text(strip=True)
                        size_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                        pdf_entry = {
                            "url": file_url,
                            "filename": file_name,
                            "size_bytes": parse_size_text(size_text),
                            "size_display": size_text,
                        }
                        data["pdf_files"].append(pdf_entry)

    # Calculate total PDF size
    total_size = sum(p.get("size_bytes", 0) for p in data["pdf_files"])
    data["pdf_size_bytes"] = total_size
    data["pdf_size_display"] = human_size(total_size) if total_size else ""

    data["title"] = data["title"].strip().rstrip("-").strip()
    return data


# ---------------------------------------------------------------------------
# Step 3: Download files
# ---------------------------------------------------------------------------

def _stream_to_file(resp, dest_path: Path, existing_size: int, desc: str) -> bool:
    """Write a streaming response to dest_path. Returns True on success."""
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


def download_file(url: str, dest_path: Path, desc: str = "",
                  connect_timeout: int = 20, read_timeout: int = 300) -> bool:
    """Download a single file, handling Shodhganga's port-8443 PDF proxy.

    Flow for bitstream URLs:
      1. Pre-rewrite URL to PDF_PORT (8443) — skips the 302 redirect.
         SSL verification uses PDF_SSL_VERIFY (disable with --no-verify-ssl).
      2. On any connection/SSL error to 8443, fall back to the original
         port-443 URL with allow_redirects=False to inspect the redirect:
         - 200/206 -> download directly from 443.
         - 302 to 8443/pdfToThesis -> log diagnostic and fail.
         - 302 elsewhere -> follow once.
    """
    if not url:
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    existing_size = dest_path.stat().st_size if dest_path.exists() else 0
    headers = {**HEADERS}
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    original_url = url
    is_bitstream = "bitstream" in url.lower()
    if PDF_PORT != 443 and is_bitstream:
        parsed = urllib.parse.urlparse(url)
        url = urllib.parse.urlunparse(parsed._replace(netloc=f"{parsed.hostname}:{PDF_PORT}"))

    timeout = (connect_timeout, read_timeout)
    # Use PDF_SSL_VERIFY for 8443 requests; always verify for standard 443.
    ssl_verify = PDF_SSL_VERIFY if is_bitstream else True

    # --- Attempt 1: PDF_PORT (8443 by default) ---
    try:
        resp = SESSION.get(url, headers=headers, stream=True,
                           timeout=timeout, allow_redirects=True,
                           verify=ssl_verify)
    except (requests.RequestException, ConnectionError, OSError) as e:
        is_port_err = (
            PDF_PORT != 443
            and str(PDF_PORT) in (urllib.parse.urlparse(url).netloc or "")
        )
        reason = _classify_download_error(e)
        if not is_port_err:
            log.warning("  %s: download error: %s", desc, reason)
            return False
        # 8443 failed — fall back to port 443 without following redirects.
        log.info("  %s: port %d failed (%s), trying port 443 ...", desc, PDF_PORT, reason)
        try:
            resp = SESSION.get(original_url, headers=headers, stream=True,
                               timeout=timeout, allow_redirects=False, verify=True)
        except (requests.RequestException, ConnectionError, OSError) as e2:
            log.warning("  %s: port 443 also failed: %s", desc, _classify_download_error(e2))
            return False

        if resp.status_code in (200, 206):
            if "text/html" in resp.headers.get("content-type", ""):
                log.warning("  %s: got HTML on 443 (consent/login page)", desc)
                return False
            return _stream_to_file(resp, dest_path, existing_size, desc)

        if resp.status_code == 302:
            location = resp.headers.get("Location", "")
            if str(PDF_PORT) in location or "pdfToThesis" in location:
                # Try rewriting the redirect to port 443 — the servlet may exist there too.
                parsed_loc = urllib.parse.urlparse(location)
                loc_443 = urllib.parse.urlunparse(parsed_loc._replace(
                    netloc=parsed_loc.hostname  # drops the :8443
                ))
                log.info("  %s: rewriting redirect from port %d → 443: %s",
                         desc, PDF_PORT, loc_443[:80])
                try:
                    resp3 = SESSION.get(loc_443, headers=headers, stream=True,
                                        timeout=timeout, allow_redirects=False, verify=True)
                    if resp3.status_code in (200, 206):
                        if "text/html" in resp3.headers.get("content-type", ""):
                            log.warning("  %s: got HTML on port-443 servlet (consent page?)", desc)
                        else:
                            return _stream_to_file(resp3, dest_path, existing_size, desc)
                    elif resp3.status_code == 302:
                        log.info("  %s: port-443 servlet also redirects (%s)",
                                 desc, resp3.headers.get("Location", "")[:60])
                    else:
                        log.info("  %s: port-443 servlet returned HTTP %d", desc, resp3.status_code)
                except (requests.RequestException, ConnectionError, OSError) as e3:
                    log.info("  %s: port-443 servlet failed: %s", desc, _classify_download_error(e3))

                # Nothing worked — report why the original port failed.
                is_timeout = "timeout" in reason
                if is_timeout:
                    log.warning(
                        "  %s: port %d is firewalled — original error: %s. "
                        "Connect via INFLIBNET network or use a VPN with Indian exit node.",
                        desc, PDF_PORT, reason,
                    )
                else:
                    log.warning(
                        "  %s: PDF proxy requires port %d — original error: %s. "
                        "On macOS try: --no-verify-ssl (SSL cert rejection). "
                        "If port is blocked: connect via INFLIBNET or set SHG_PDF_PORT=443.",
                        desc, PDF_PORT, reason,
                    )
                return False
            # Redirect elsewhere — follow once.
            log.info("  %s: following 443 redirect to %s", desc, location[:80])
            try:
                resp2 = SESSION.get(location, headers=headers, stream=True,
                                    timeout=timeout, allow_redirects=False, verify=True)
                if resp2.status_code in (200, 206):
                    if "text/html" in resp2.headers.get("content-type", ""):
                        log.warning("  %s: got HTML after redirect (consent page?)", desc)
                        return False
                    return _stream_to_file(resp2, dest_path, existing_size, desc)
            except (requests.RequestException, ConnectionError, OSError) as e3:
                log.warning("  %s: redirect target failed: %s", desc, _classify_download_error(e3))
            return False

        log.warning("  %s: unexpected HTTP %d on port-443 fallback", desc, resp.status_code)
        return False

    # --- Process the response from the primary attempt ---
    if resp.status_code == 416:
        log.info("  %s: already complete (%d bytes)", desc, existing_size)
        return True
    if resp.status_code in (200, 206):
        if "text/html" in resp.headers.get("content-type", ""):
            log.warning(
                "  %s: got HTML instead of PDF — consent/login page served by pdfToThesis. "
                "Session cookie may be required. Try opening the thesis in a browser first.",
                desc,
            )
            return False
        return _stream_to_file(resp, dest_path, existing_size, desc)
    if resp.status_code == 403:
        log.warning("  %s: access denied (403) — session cookie may be required", desc)
        return False
    log.warning("  %s: HTTP %d", desc, resp.status_code)
    return False


# ---------------------------------------------------------------------------
# PDF merging
# ---------------------------------------------------------------------------

def merge_pdfs(pdf_paths: list, output_path: Path) -> bool:
    if not pdf_paths:
        return False
    if len(pdf_paths) == 1:
        shutil.copy2(pdf_paths[0], output_path)
        return True

    try:
        from pypdf import PdfWriter
        merger = PdfWriter()
        for pdf in pdf_paths:
            merger.append(str(pdf))
        merger.write(str(output_path))
        merger.close()
        return True
    except ImportError:
        pass

    pdf_args = [str(p) for p in pdf_paths]
    cmd = ["pdfunite"] + pdf_args + [str(output_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return True
        log.warning("pdfunite failed: %s", r.stderr[:200])
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("pdfunite error: %s", e)

    return False


# ---------------------------------------------------------------------------
# Identifier creation
# ---------------------------------------------------------------------------

def sanitize_identifier(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9_.-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = re.sub(r'\.+', '.', s)
    s = s.strip('-_.').lower()
    return s[:76]


def make_identifier(prefix: str, handle_suffix: str, title: str, folder: Path) -> str:
    hs = handle_suffix or ""
    if prefix.endswith("."):
        base = f"{prefix}{hs}" if hs else prefix.rstrip(".")
        combined = sanitize_identifier(f"{base}-{title}")
    else:
        combined = sanitize_identifier(f"{prefix}-{hs}-{title}" if hs else f"{prefix}-{title}")
    if len(combined) < 4 or combined.replace('-', '').replace('.', '') == '':
        h = hashlib.md5(folder.name.encode()).hexdigest()[:8]
        if hs:
            combined = sanitize_identifier(f"{prefix}{hs}-{h}")
        else:
            combined = sanitize_identifier(f"{prefix}{h}")
    if len(combined) > 80:
        combined = combined[:80]
    return combined


# ---------------------------------------------------------------------------
# Language normalization
# ---------------------------------------------------------------------------

LANGUAGE_MAP = {
    "en": "eng", "english": "eng", "eng": "eng",
    "hi": "hin", "hindi": "hin", "hin": "hin",
    "sa": "san", "sanskrit": "san", "san": "san",
    "mr": "mar", "marathi": "mar", "mar": "mar",
    "gu": "guj", "gujarati": "guj", "guj": "guj",
    "bn": "ben", "bengali": "ben", "ben": "ben",
    "ta": "tam", "tamil": "tam", "tam": "tam",
    "te": "tel", "telugu": "tel", "tel": "tel",
    "kn": "kan", "kannada": "kan", "kan": "kan",
    "ml": "mal", "malayalam": "mal", "mal": "mal",
    "or": "ori", "oriya": "ori", "ori": "ori",
    "pa": "pan", "punjabi": "pan", "pan": "pan",
    "ur": "urd", "urdu": "urd", "urd": "urd",
    "fr": "fre", "french": "fre", "fre": "fre",
    "de": "ger", "german": "ger", "ger": "ger",
    "es": "spa", "spanish": "spa", "spa": "spa",
}


def normalize_language(lang: str) -> str:
    if not lang:
        return "eng"
    key = lang.strip().lower()
    if key in LANGUAGE_MAP:
        return LANGUAGE_MAP[key]
    return key[:3] if len(key) >= 3 else key


# ---------------------------------------------------------------------------
# IA Metadata mapping
# ---------------------------------------------------------------------------

def transform_to_ia_metadata(data: dict, prefix: str = "shg.",
                              collection: str = "shodhganga", cfg: dict = None) -> dict:
    subjects = list(data.get("subjects", []))
    guides = data.get("guides", [])
    guide_str = "; ".join(guides) if guides else ""

    description_parts = []
    if data.get("abstract"):
        description_parts.append(data["abstract"])
    extra = []
    if data.get("university"):
        extra.append(f"University: {data['university']}")
    if data.get("department"):
        extra.append(f"Department: {data['department']}")
    if guide_str:
        extra.append(f"Guide(s): {guide_str}")
    if data.get("publisher"):
        extra.append(f"Publisher: {data['publisher']}")
    if data.get("handle"):
        extra.append(f"Handle: {BASE_URL}/handle/{data['handle']}")
    if extra:
        description_parts.append(" | ".join(extra))
    description = "\n\n".join(description_parts)

    lang = normalize_language(data.get("language", ""))
    license_url = (cfg or {}).get("license_url", "") or os.environ.get("SHG_LICENSE_URL", "")
    license_name = (cfg or {}).get("license", "") or os.environ.get("SHG_LICENSE", "")
    if not license_url:
        license_url = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
    if not license_name:
        license_name = "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International"

    return {k: v for k, v in {
        "title": data.get("title", ""),
        "description": description,
        "subject": subjects if subjects else "",
        "creator": data.get("researcher", ""),
        "contributor": guide_str,
        "date": data.get("date_issued", ""),
        "language": lang,
        "type": data.get("type", "Thesis"),
        "source": data.get("uri", ""),
        "identifier": data.get("handle", ""),
        "original_url": data.get("item_url", ""),
        "publisher": data.get("publisher", ""),
        "collection": collection,
        "mediatype": "texts",
        "licenseurl": license_url,
        "license": license_name,
    }.items() if v}


# ---------------------------------------------------------------------------
# IA upload
# ---------------------------------------------------------------------------

def upload_to_ia(identifier: str, files: list, meta: dict,
                 collection: str, no_collection_check: bool,
                 ia_timeout: int, ia_retries: int) -> bool:
    IA_META_KEYS = {
        "title", "description", "subject", "creator", "publisher", "contributor",
        "date", "language", "type", "source", "identifier", "collection",
        "mediatype", "licenseurl", "license", "original_url", "pages",
        "edition", "volume", "coverage", "rights",
    }
    meta_flags = []
    for k, v in meta.items():
        if k not in IA_META_KEYS:
            continue
        if k == "identifier":
            continue
        if isinstance(v, list):
            for item in v:
                meta_flags.extend(["-m", f"{k}:{item}"])
        elif v:
            meta_flags.extend(["-m", f"{k}:{v}"])

    meta_flags.extend(["-m", f"collection:{collection}"])
    if no_collection_check:
        meta_flags.append("--no-collection-check")

    ia_identifier = identifier[:80]
    time.sleep(random.uniform(0.5, 2.0))

    candidate = ia_identifier
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["ia", "metadata", candidate],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                try:
                    d = json.loads(r.stdout)
                    if d and "metadata" in d:
                        # Metadata-only placeholders (.txt) must not count as done.
                        has_pdf = any(
                            str(f.get("name", "")).lower().endswith(".pdf")
                            and int(f.get("size") or 0) > 0
                            for f in d.get("files", [])
                        )
                        if has_pdf:
                            log.info("  %s: already on IA with PDF", candidate)
                            return True
                        log.info("  %s: on IA without PDF, adding files", candidate)
                except (json.JSONDecodeError, TypeError):
                    pass
            break
        except (subprocess.TimeoutExpired, OSError):
            if attempt < 2:
                time.sleep(2)
                continue

    cmd = ["ia", "upload", candidate] + files + meta_flags
    for attempt in range(ia_retries):
        try:
            time.sleep(random.uniform(1.0, 3.0))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=ia_timeout)
            if r.returncode == 0:
                log.info("  Uploaded: %s", candidate)
                return True
            err = (r.stderr or "").lower()
            if "timeout" in err or "connection" in err or "slowdown" in err:
                if attempt < ia_retries - 1:
                    wait = 10 * (2 ** attempt)
                    log.info("  Rate limited, retrying %s in %ds...", candidate, wait)
                    time.sleep(wait)
                    continue
            if "access denied" in err:
                log.info("  %s: access denied to %s, retrying with opensource", candidate, collection)
                alt_meta = []
                i = 0
                while i < len(meta_flags):
                    if meta_flags[i] == "-m" and i + 1 < len(meta_flags) and meta_flags[i+1].startswith("collection:"):
                        i += 2
                        continue
                    alt_meta.append(meta_flags[i])
                    i += 1
                alt_cmd = ["ia", "upload", candidate] + files + alt_meta + ["-m", "collection:opensource"]
                if no_collection_check:
                    alt_cmd.append("--no-collection-check")
                time.sleep(2)
                try:
                    r2 = subprocess.run(alt_cmd, capture_output=True, text=True, timeout=ia_timeout)
                    if r2.returncode == 0:
                        log.info("  Uploaded %s to opensource collection", candidate)
                        return True
                except subprocess.TimeoutExpired:
                    pass
            if "bucket" in err and "not available" in err:
                log.warning("  %s: bucket collision", candidate)
            log.warning(
                "  Upload failed: %s (rc=%d) — stderr: %s — stdout: %s",
                candidate, r.returncode, r.stderr.strip()[:500], r.stdout.strip()[:200],
            )
            return False
        except subprocess.TimeoutExpired:
            if attempt < ia_retries - 1:
                log.info("  Timeout for %s, retrying in 15s...", candidate)
                time.sleep(15)
                continue
            log.warning("  Timeout for %s after %d attempts", candidate, ia_retries)
            return False
    return False


# ---------------------------------------------------------------------------
# Load item URLs
# ---------------------------------------------------------------------------

def load_item_urls(source) -> list:
    if isinstance(source, str) and '\n' not in source and not os.path.isfile(source):
        return [source.strip()]
    if os.path.isfile(source):
        with open(source) as f:
            content = f.read().strip()
        if content.startswith("{"):
            try:
                data = json.loads(content)
                if "items" in data:
                    return data["items"] if isinstance(data["items"], list) else []
            except json.JSONDecodeError:
                pass
        urls = []
        for line in content.splitlines():
            line = line.strip()
            if line and line.startswith("http") and "/handle/" in line:
                urls.append(line)
        if urls:
            return urls
    return []


def load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"items": [], "total_size_bytes": 0, "total_items": 0}


# ---------------------------------------------------------------------------
# COMMAND: list
# ---------------------------------------------------------------------------

def cmd_list(args):
    log.info("Listing items from Shodhganga browse...")
    resume_path = args.output if args.resume else None
    items = list_items_from_browse(
        rpp=args.rpp, max_items=args.max_items,
        resume_path=resume_path,
        session_refresh_interval=args.refresh,
        base_delay=args.delay,
        connect_timeout=args.connect_timeout,
    )
    output = {
        "source": "shodhganga.inflibnet.ac.in",
        "total": len(items),
        "items": items,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(args.output, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("Saved %d item URLs to %s", len(items), args.output)


def cmd_list_communities(args):
    """Scrape the university/community list from the portal."""
    log.info("Fetching community list from Shodhganga...")
    resp = fetch(f"{BASE_URL}/university-list.html")
    soup = BeautifulSoup(resp.text, "html.parser")
    communities = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if text and re.match(r'/handle/10603/\d+', href):
            communities.append((href, text))
    log.info("Found %d communities", len(communities))
    output = {
        "source": "shodhganga.inflibnet.ac.in",
        "total": len(communities),
        "items": communities,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(args.output, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("Saved %d communities to %s", len(communities), args.output)


# ---------------------------------------------------------------------------
# COMMAND: manifest
# ---------------------------------------------------------------------------

def _save_manifest_checkpoint(manifest_file, manifest, progress_file, state, log_msg):
    tmp = manifest_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    tmp.replace(manifest_file)
    save_progress(progress_file, state)
    if log_msg:
        log.info("Checkpoint: %s", log_msg)


def cmd_manifest(args):
    cfg = load_config(getattr(args, 'config', None))
    workers = clamp_workers(
        get_config("workers", 10, cfg, args),
        MAX_WORKERS_DOWNLOAD,
    )
    output_dir = Path(args.dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    items = []
    if args.urls:
        items = load_item_urls(args.urls)
    if not items and args.input:
        items = load_item_urls(args.input)
    if not items:
        log.error("No item URLs found. Use --input listing.json or --urls")
        sys.exit(1)

    log.info("Building manifest for %d items with %d workers...", len(items), workers)
    manifest_file = output_dir / "manifest.json"
    progress_file = output_dir / "progress.json"
    state = load_progress(progress_file)
    existing = load_manifest(manifest_file)
    existing_by_url = {m["item_url"]: m for m in existing.get("items", []) if "item_url" in m}

    new_manifest_items = []
    for item in existing.get("items", []):
        if "item_url" in item:
            new_manifest_items.append(item)

    save_interval = getattr(args, 'save_interval', 0) or 1000

    def fetch_manifest_entry(item_url: str) -> dict:
        if item_url in existing_by_url:
            return None
        try:
            data = extract_metadata(item_url)
            # Language filter: skip items that don't match
            if args.language:
                item_lang = normalize_language(data.get("language", ""))
                target_lang = normalize_language(args.language)
                if item_lang != target_lang:
                    return None
            return {
                "item_url": item_url,
                "handle": data["handle"],
                "handle_suffix": data["handle_suffix"],
                "title": data["title"],
                "pdf_files": data["pdf_files"],
                "pdf_size_bytes": data["pdf_size_bytes"],
                "pdf_size_display": data["pdf_size_display"],
                "scrape_date": data["scrape_date"],
            }
        except Exception as e:
            log.error("  Failed: %s — %s", item_url, e)
            return {
                "item_url": item_url,
                "handle": parse_handle_id(item_url),
                "handle_suffix": parse_handle_suffix(parse_handle_id(item_url)),
                "title": "",
                "pdf_files": [],
                "pdf_size_bytes": 0,
                "pdf_size_display": "",
                "scrape_date": "",
                "error": str(e),
            }

    urls_to_fetch = [u for u in items if u not in existing_by_url]
    if args.limit and args.limit < len(urls_to_fetch):
        urls_to_fetch = urls_to_fetch[:args.limit]
        log.info("Limited to first %d new items", args.limit)
    if urls_to_fetch:
        log.info("Fetching metadata for %d new items...", len(urls_to_fetch))
        seen_urls = set()
        deduped = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_manifest_entry, url): url for url in urls_to_fetch}
            with tqdm(total=len(urls_to_fetch), desc="Manifest", unit="item") as pbar:
                for future in concurrent.futures.as_completed(futures):
                    entry = future.result()
                    if entry:
                        url = entry.get("item_url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            deduped.append(entry)
                            hs = entry.get("handle_suffix", "")
                            if hs and not get_item_stage(state, hs):
                                set_item_stage(state, hs, STAGE_MANIFESTED)
                    pbar.update(1)
                    total_so_far = len(deduped) + len(existing.get("items", []))
                    if save_interval and total_so_far % save_interval == 0 and total_so_far > 0:
                        total_bytes = sum(m.get("pdf_size_bytes", 0) for m in existing.get("items", []) + deduped)
                        total_with_pdf = sum(1 for m in existing.get("items", []) + deduped
                                             if len(m.get("pdf_files", [])) > 0)
                        total_no_pdf = sum(1 for m in existing.get("items", []) + deduped
                                           if not m.get("pdf_files"))
                        total_failed = sum(1 for m in existing.get("items", []) + deduped if m.get("error"))
                        ckpt = {
                            "source": "shodhganga.inflibnet.ac.in",
                            "generated": datetime.now(timezone.utc).isoformat(),
                            "total_items": total_so_far,
                            "total_size_bytes": total_bytes,
                            "total_size_display": human_size(total_bytes),
                            "items_with_pdf": total_with_pdf,
                            "items_without_pdf": total_no_pdf,
                            "items_failed": total_failed,
                            "items": existing.get("items", []) + deduped,
                        }
                        _save_manifest_checkpoint(
                            manifest_file, ckpt, progress_file, state,
                            f"{total_so_far} items at ~{total_so_far/len(items)*100:.1f}%"
                        )
    else:
        deduped = []
        seen_urls = set()
        for m in existing.get("items", []):
            url = m.get("item_url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped.append(m)

    total_items = len(existing.get("items", [])) + len(deduped)
    total_bytes = sum(m.get("pdf_size_bytes", 0) for m in existing.get("items", []) + deduped)
    total_with_pdf = sum(1 for m in existing.get("items", []) + deduped if len(m.get("pdf_files", [])) > 0)
    total_no_pdf = sum(1 for m in existing.get("items", []) + deduped if not m.get("pdf_files"))
    total_failed = sum(1 for m in existing.get("items", []) + deduped if m.get("error"))

    manifest = {
        "source": "shodhganga.inflibnet.ac.in",
        "generated": datetime.now(timezone.utc).isoformat(),
        "total_items": total_items,
        "total_size_bytes": total_bytes,
        "total_size_display": human_size(total_bytes),
        "items_with_pdf": total_with_pdf,
        "items_without_pdf": total_no_pdf,
        "items_failed": total_failed,
        "items": existing.get("items", []) + deduped,
    }

    _save_manifest_checkpoint(manifest_file, manifest, progress_file, state, None)

    log.info("Manifest saved: %s", manifest_file)
    log.info("  Total items:   %d", total_items)
    log.info("  Total size:    %s (%d bytes)", human_size(total_bytes), total_bytes)
    log.info("  With PDF:      %d", total_with_pdf)
    log.info("  No PDF files:  %d", total_no_pdf)
    log.info("  Failed:        %d", total_failed)


# ---------------------------------------------------------------------------
# COMMAND: pipeline
# ---------------------------------------------------------------------------

def cmd_pipeline(args):
    cfg = load_config(getattr(args, 'config', None))
    prefix = get_config("prefix", "shg.", cfg, args)
    collection = get_config("ia_collection", "shodhganga", cfg, args)

    # Apply --no-verify-ssl flag to the global PDF_SSL_VERIFY before any downloads.
    if getattr(args, 'no_verify_ssl', False):
        global PDF_SSL_VERIFY
        PDF_SSL_VERIFY = False
        log.info("SSL verification disabled for PDF proxy (--no-verify-ssl)")

    output_dir = Path(args.dir)
    workers = clamp_workers(get_config("workers", 5, cfg, args), MAX_WORKERS_DOWNLOAD)
    skip_upload = args.skip_upload
    no_collection_check = get_config("no_collection_check", False, cfg, args)
    ia_timeout = get_config("ia_timeout", 300, cfg, args)
    ia_retries = get_config("ia_retries", 3, cfg, args)

    manifest_file = output_dir / "manifest.json"
    progress_file = output_dir / "progress.json"
    if not manifest_file.exists():
        log.error("Manifest not found: %s", manifest_file)
        sys.exit(1)
    manifest = load_manifest(manifest_file)
    manifest_items = manifest.get("items", [])
    if not manifest_items:
        log.error("No items in manifest.")
        sys.exit(1)

    state = load_progress(progress_file)
    log.info("Pipeline: %d items from manifest, %d workers", len(manifest_items), workers)

    to_process = []
    for entry in manifest_items:
        hs = entry.get("handle_suffix", "")
        if not hs:
            continue
        stage = get_item_stage(state, hs)
        if stage in (STAGE_CLEANED, STAGE_UPLOADED):
            continue
        if skip_upload and stage in (STAGE_PDF_DOWNLOADED, STAGE_MERGED):
            continue
        to_process.append(entry)

    if args.limit and args.limit < len(to_process):
        to_process = to_process[:args.limit]
        log.info("Limited to first %d items", args.limit)

    if not to_process:
        log.info("All items processed!")
        return

    log.info("Processing %d items through pipeline...", len(to_process))

    hs_to_dir = {}
    if output_dir.exists():
        for d in output_dir.iterdir():
            if d.is_dir() and d.name[0].isdigit() and not d.name.startswith("."):
                hs_candidate = d.name.split("_", 1)[0]
                if hs_candidate.isdigit():
                    hs_to_dir[hs_candidate] = d

    def pipeline_item(entry):
        hs = entry["handle_suffix"]
        item_url = entry["item_url"]
        title = entry.get("title", f"untitled_{hs}")
        safe_title = safe_filename(title, 200)
        item_dir = hs_to_dir.get(hs) or (output_dir / f"{hs}_{safe_title}")
        item_dir.mkdir(parents=True, exist_ok=True)
        result = {"handle_suffix": hs, "title": title, "dir": str(item_dir)}
        meta_file = item_dir / "metadata.json"

        # Stage 1: Load or fetch metadata
        meta_ia = None
        if meta_file.exists():
            try:
                cached = json.loads(meta_file.read_text())
                content_fields = ["description", "subject", "creator", "contributor", "date", "source"]
                has_content = any(cached.get(f) for f in content_fields)
                if cached.get("title") and cached.get("identifier") and has_content:
                    meta_ia = cached
            except (json.JSONDecodeError, OSError):
                pass

        if meta_ia is None:
            try:
                full_meta = extract_metadata(item_url)
                identifier = make_identifier(prefix, hs, full_meta.get("title", title), item_dir)
                meta_ia = transform_to_ia_metadata(full_meta, prefix, collection, cfg)
                meta_ia["identifier"] = identifier
                if "pdf_files" in full_meta:
                    meta_ia["pdf_files"] = full_meta["pdf_files"]
                if "handle_suffix" in full_meta:
                    meta_ia["handle_suffix"] = full_meta["handle_suffix"]
                meta_file.write_text(json.dumps(meta_ia, ensure_ascii=False, indent=2))
            except Exception as e:
                log.warning("  [%s] metadata fetch failed: %s", hs, e)
                meta_ia = entry
        else:
            meta_file.write_text(json.dumps(meta_ia, ensure_ascii=False, indent=2))

        identifier = make_identifier(prefix, hs, meta_ia.get("title", title), item_dir)
        if meta_ia.get("identifier") != identifier:
            meta_ia["identifier"] = identifier
        if meta_ia.get("handle_suffix") != hs:
            meta_ia["handle_suffix"] = hs
        meta_file.write_text(json.dumps(meta_ia, ensure_ascii=False, indent=2))

        # Language filter: skip items that don't match
        if args.language:
            item_lang = normalize_language(meta_ia.get("language", ""))
            target_lang = normalize_language(args.language)
            if item_lang != target_lang:
                log.info("  [%s] language '%s' != '%s', skipping",
                         hs, item_lang, target_lang)
                return result

        # Stage 2: Download all PDF files
        pdf_files = meta_ia.get("pdf_files") or entry.get("pdf_files", [])
        if not pdf_files:
            log.warning("  [%s] no PDF files listed", hs)

        downloaded_pdfs = []
        total_pdf_size = 0
        for pdf_info in pdf_files:
            pdf_url = pdf_info.get("url", "")
            if not pdf_url:
                continue
            pdf_name = pdf_info.get("filename", f"{hs}.pdf")
            pdf_path = item_dir / safe_filename(pdf_name)
            ok = download_file(pdf_url, pdf_path, desc=f"{title[:40]}/{pdf_name[:20]}")
            if ok and pdf_path.exists():
                downloaded_pdfs.append(pdf_path)
                total_pdf_size += pdf_path.stat().st_size

        result["pdf_count"] = len(downloaded_pdfs)
        result["pdf_files"] = [str(p) for p in downloaded_pdfs]

        # Stage 3: Merge all PDFs into one
        merged_pdf = None
        if downloaded_pdfs:
            merged_name = f"{identifier}.pdf"
            merged_path = item_dir / merged_name
            if not merged_path.exists():
                log.info("  [%s] merging %d PDFs...", hs, len(downloaded_pdfs))
                if merge_pdfs(downloaded_pdfs, merged_path):
                    log.info("  [%s] merged -> %s (%.1f MB)", hs, merged_name,
                             merged_path.stat().st_size / 1024 / 1024)
                else:
                    log.warning("  [%s] merge failed, using first PDF", hs)
                    if downloaded_pdfs:
                        shutil.copy2(downloaded_pdfs[0], merged_path)
            if merged_path.exists():
                merged_pdf = str(merged_path)
                result["pdf_file"] = merged_pdf
                result["pdf_size"] = merged_path.stat().st_size

        # Stage 4: Upload to IA
        uploaded = False
        if not skip_upload and merged_pdf and os.path.exists(merged_pdf):
            files = [merged_pdf]
            meta = json.loads(meta_file.read_text()) if meta_file.exists() else meta_ia
            log.info("  [%s] uploading to IA...", hs)
            _upload_semaphore.acquire()
            try:
                uploaded = upload_to_ia(
                    identifier, files, meta,
                    collection, no_collection_check,
                    ia_timeout, ia_retries,
                )
            finally:
                _upload_semaphore.release()
            if not uploaded:
                failed_file = output_dir / "upload_failed.json"
                log_upload_failed(failed_file, collection, item_dir.name, identifier, "upload failed")

        # Stage 5: Cleanup
        if uploaded and args.cleanup:
            shutil.rmtree(item_dir, ignore_errors=True)
            log.info("  [%s] cleaned up local files", hs)

        result["identifier"] = identifier
        result["uploaded"] = uploaded
        return result

    completed_batch = 0
    save_batch_freq = 20

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(pipeline_item, entry): entry
            for entry in to_process
        }
        with tqdm(total=len(to_process), desc="Pipeline", unit="item") as pbar:
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                try:
                    result = future.result()
                    hs = result.get("handle_suffix", "")
                    with _progress_save_lock:
                        prev_stage = get_item_stage(state, hs)

                        if result.get("pdf_file") and os.path.exists(result["pdf_file"]):
                            set_item_stage(state, hs, STAGE_PDF_DOWNLOADED)
                            if prev_stage not in (STAGE_PDF_DOWNLOADED, STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED):
                                pdf_size = os.path.getsize(result["pdf_file"])
                                state["downloaded_bytes"] = state.get("downloaded_bytes", 0) + pdf_size
                        elif not prev_stage:
                            set_item_stage(state, hs, STAGE_METADATA_SAVED)

                        if result.get("uploaded"):
                            new_stage = STAGE_CLEANED if args.cleanup else STAGE_UPLOADED
                            set_item_stage(state, hs, new_stage)
                            if prev_stage not in (STAGE_UPLOADED, STAGE_CLEANED):
                                state["uploaded_bytes"] = state.get("uploaded_bytes", 0) + result.get("pdf_size", 0)

                        state["items"][hs] = result
                        completed_batch += 1
                        if completed_batch % save_batch_freq == 0:
                            save_progress(progress_file, state)

                except Exception as e:
                    log.error("  Pipeline error: %s", e)
                    hs_val = entry.get("handle_suffix", "")
                    if hs_val:
                        with _progress_save_lock:
                            state.setdefault("failed", []).append(hs_val)
                            save_progress(progress_file, state)
                pbar.update(1)

    save_progress(progress_file, state)
    log.info("Pipeline complete.")


# ---------------------------------------------------------------------------
# COMMAND: status
# ---------------------------------------------------------------------------

def cmd_status(args):
    output_dir = Path(args.dir)
    if not output_dir.exists():
        log.error("Directory %s not found", output_dir)
        sys.exit(1)

    progress_file = output_dir / "progress.json"
    manifest_file = output_dir / "manifest.json"
    state = load_progress(progress_file)
    manifest = load_manifest(manifest_file)

    stages = state.get("stages", {})
    manifested = set(stages.get(STAGE_MANIFESTED, []))
    metadata_saved = set(stages.get(STAGE_METADATA_SAVED, []))
    pdf_downloaded = set(stages.get(STAGE_PDF_DOWNLOADED, []))
    merged = set(stages.get(STAGE_MERGED, []))
    uploaded = set(stages.get(STAGE_UPLOADED, []))
    cleaned = set(stages.get(STAGE_CLEANED, []))

    total_uploaded = len(uploaded) + len(cleaned)
    total_downloaded = len(pdf_downloaded) + len(merged) + total_uploaded

    item_dirs = [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    disk_with_pdf = 0
    disk_with_meta = 0
    disk_bytes = 0
    for d in item_dirs:
        meta = d / "metadata.json"
        if meta.exists():
            disk_with_meta += 1
        pdfs = list(d.glob("*.pdf"))
        if pdfs:
            disk_with_pdf += 1
            disk_bytes += sum(p.stat().st_size for p in pdfs)

    manifest_total = manifest.get("total_items", 0)
    manifest_size = manifest.get("total_size_bytes", 0)
    manifest_size_display = manifest.get("total_size_display", human_size(manifest_size))

    uploaded_bytes = state.get("uploaded_bytes", 0)
    downloaded_bytes = state.get("downloaded_bytes", 0)

    failed_file = output_dir / "upload_failed.json"
    upload_failures = []
    if failed_file.exists():
        try:
            upload_failures = json.loads(failed_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    remaining = manifest_total - total_uploaded
    remaining_bytes = max(0, manifest_size - uploaded_bytes)
    pct_by_count = (total_uploaded / manifest_total * 100) if manifest_total else 0
    pct_by_size = (uploaded_bytes / manifest_size * 100) if manifest_size else 0

    def pf(label, count, size):
        print("  {:<30} {:>8}  {:>10}".format(label, str(count), str(size)))

    sep1 = "  " + "─" * 30 + " " + "─" * 8 + "  " + "─" * 10
    sep2 = "  " + "─" * 25 + " " + "─" * 8

    print()
    print("=" * 70)
    print("  SHODHGANGA CORPUS — STAGE PROGRESS")
    print("=" * 70)
    pf("", "Count", "Size")
    print(sep1)

    pf("Manifest total", manifest_total, manifest_size_display)
    pf("Metadata saved", disk_with_meta, "—")
    pf("PDF downloaded+merged", disk_with_pdf, human_size(disk_bytes))
    pf("Uploaded to IA", total_uploaded, human_size(uploaded_bytes))
    pf("Cleaned (removed)", len(cleaned), "—")

    failed_count = len(state.get("failed", [])) + len(upload_failures)
    if failed_count:
        pf("Failed items", failed_count, "—")

    print(sep1)
    pf("Remaining", remaining, human_size(remaining_bytes))
    print("  Progress (by count): {:>6.1f}%".format(pct_by_count))
    print("  Progress (by size):  {:>6.1f}%".format(pct_by_size))

    print()
    print("  {:<25} {:>8}".format("Stage", "Items"))
    print(sep2)
    stage_order = [STAGE_MANIFESTED, STAGE_METADATA_SAVED, STAGE_PDF_DOWNLOADED,
                   STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED]
    stage_labels = {
        STAGE_MANIFESTED: "manifested",
        STAGE_METADATA_SAVED: "metadata saved",
        STAGE_PDF_DOWNLOADED: "PDFs downloaded",
        STAGE_MERGED: "PDFs merged",
        STAGE_UPLOADED: "uploaded",
        STAGE_CLEANED: "cleaned",
    }
    for s in stage_order:
        items_in_stage = len(stages.get(s, []))
        label = stage_labels.get(s, s)
        print(f"  {label:25s} {items_in_stage:>8d}")

    if upload_failures:
        print(f"\n  Upload failures ({len(upload_failures)}):")
        for f in upload_failures[-5:]:
            ident = f.get("identifier", "?")
            reason = f.get("reason", "?")
            print(f"    {ident}: {reason[:80]}")
        if len(upload_failures) > 5:
            print(f"    ... and {len(upload_failures) - 5} more")

    print()

    if args.verbose and manifest.get("items"):
        print(f"  {'[P]':4s} {'[M]':4s} {'[U]':4s} {'Size':>8s}  {'Identifier':45s}  {'Title':40s}")
        sep3 = "  " + "─" * 4 + " " + "─" * 4 + " " + "─" * 4 + " " + "─" * 8 + "  " + "─" * 45 + "  " + "─" * 40
        print(sep3)
        for m in manifest.get("items", []):
            hs = m.get("handle_suffix", "")
            stage = get_item_stage(state, hs) or ""
            has_pdf = "✓" if stage in (STAGE_PDF_DOWNLOADED, STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED) else " "
            has_meta = "✓" if stage in (STAGE_METADATA_SAVED, STAGE_PDF_DOWNLOADED, STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED) else " "
            is_up = "✓" if stage in (STAGE_UPLOADED, STAGE_CLEANED) else " "
            sz = m.get("pdf_size_bytes", 0)
            title = m.get("title", "")[:38]
            ident = make_identifier(
                args.prefix or "shg.", hs, m.get("title", ""), output_dir / "dummy"
            )[:43] if hs else "?"
            print(f"  [{has_pdf}] [{has_meta}] [{is_up}] {human_size(sz):>8s}  {ident:45s}  {title:40s}")


# ---------------------------------------------------------------------------
# COMMAND: download (legacy)
# ---------------------------------------------------------------------------

def stages_to_ids(state, *stages):
    ids = []
    for s in stages:
        ids.extend(state.get("stages", {}).get(s, []))
    return ids


def process_item(item_url: str, output_dir: Path,
                 download_pdf: bool = True, prefix: str = "shg.", cfg: dict = None) -> dict:
    handle_id = parse_handle_id(item_url)
    handle_suffix = parse_handle_suffix(handle_id)

    try:
        data = extract_metadata(item_url)
    except Exception as e:
        return {"url": item_url, "handle": handle_id, "status": "metadata_failed", "error": str(e)}

    safe_title = safe_filename(data["title"] or handle_suffix, 200)
    item_dir = output_dir / f"{handle_suffix}_{safe_title}"
    if output_dir.exists():
        for existing_dir in output_dir.iterdir():
            if existing_dir.is_dir() and existing_dir.name.startswith(f"{handle_suffix}_"):
                item_dir = existing_dir
                break
    item_dir.mkdir(parents=True, exist_ok=True)

    identifier = make_identifier(prefix, handle_suffix, data["title"], item_dir)
    meta_ia = transform_to_ia_metadata(data, prefix, cfg=cfg)
    meta_ia["identifier"] = identifier
    meta_ia["handle_suffix"] = handle_suffix
    if data.get("pdf_files"):
        meta_ia["pdf_files"] = data["pdf_files"]
    (item_dir / "metadata.json").write_text(json.dumps(meta_ia, ensure_ascii=False, indent=2))

    result = {"url": item_url, "handle": handle_id, "handle_suffix": handle_suffix,
              "title": data["title"], "dir": str(item_dir), "identifier": identifier,
              "status": "metadata_ok", "pdf": False}

    if download_pdf and data.get("pdf_files"):
        downloaded = []
        for pdf_info in data["pdf_files"]:
            pdf_url = pdf_info.get("url", "")
            if not pdf_url:
                continue
            pdf_name = pdf_info.get("filename", f"{handle_suffix}.pdf")
            pdf_path = item_dir / safe_filename(pdf_name)
            ok = download_file(pdf_url, pdf_path, desc=data["title"][:40])
            if ok:
                downloaded.append(pdf_path)
        if downloaded:
            merged_path = item_dir / f"{identifier}.pdf"
            if merge_pdfs(downloaded, merged_path) and merged_path.exists():
                result["pdf"] = True
                result["pdf_file"] = str(merged_path)
                result["pdf_size"] = merged_path.stat().st_size
                result["status"] = "complete"
            else:
                result["status"] = "merge_failed"

    if result.get("pdf_file") and os.path.exists(result["pdf_file"]):
        sz = os.path.getsize(result["pdf_file"])
        if sz > 0 and not result.get("pdf"):
            result["status"] = "complete"
            result["pdf_size"] = sz
            result["pdf"] = True

    return result


def cmd_download(args):
    if getattr(args, 'no_verify_ssl', False):
        global PDF_SSL_VERIFY
        PDF_SSL_VERIFY = False
        log.info("SSL verification disabled for PDF proxy (--no-verify-ssl)")

    cfg = load_config(getattr(args, 'config', None))
    prefix = get_config("prefix", "shg.", cfg, args)
    workers = clamp_workers(
        get_config("workers", 5, cfg, args),
        MAX_WORKERS_DOWNLOAD,
    )
    output_dir = Path(args.dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = output_dir / "progress.json"
    state = load_progress(progress_file)
    completed_ids = set(stages_to_ids(state, STAGE_PDF_DOWNLOADED, STAGE_MERGED, STAGE_UPLOADED, STAGE_CLEANED))
    pending_ids = set(stages_to_ids(state, STAGE_MANIFESTED, STAGE_METADATA_SAVED))
    failed_set = set(state.get("failed", []))

    items = []
    if args.urls:
        items = load_item_urls(args.urls)
    if not items and args.input:
        items = load_item_urls(args.input)
    if not items:
        log.error("No item URLs found!")
        sys.exit(1)

    retry_failed = getattr(args, 'retry', False)
    to_process = []
    for url in items:
        hs = parse_handle_suffix(parse_handle_id(url))
        if hs in completed_ids:
            continue
        if getattr(args, 'resume', False):
            pass
        elif retry_failed:
            if hs not in failed_set:
                continue
        to_process.append(url)

    if not to_process:
        log.info("All done! Nothing to process.")
        return

    log.info("Downloading %d items with %d workers", len(to_process), workers)

    for url in to_process:
        hs = parse_handle_suffix(parse_handle_id(url))
        if hs not in pending_ids and hs not in completed_ids:
            set_item_stage(state, hs, STAGE_MANIFESTED)
    save_progress(progress_file, state)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_item, url, output_dir, True, prefix, cfg): url for url in to_process}
        with tqdm(total=len(to_process), desc="Items", unit="art") as pbar:
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                    hs = result.get("handle_suffix", "")
                    if result.get("status") == "metadata_failed":
                        state.setdefault("failed", []).append(hs)
                    elif result.get("pdf_file") and os.path.exists(result["pdf_file"]):
                        set_item_stage(state, hs, STAGE_MERGED)
                        pdf_size = os.path.getsize(result["pdf_file"])
                        state["downloaded_bytes"] = state.get("downloaded_bytes", 0) + pdf_size
                    else:
                        set_item_stage(state, hs, STAGE_METADATA_SAVED)
                    state["items"][hs] = result
                    save_progress(progress_file, state)
                except Exception as e:
                    log.error("Download error: %s", e)
                    hs_val = parse_handle_suffix(parse_handle_id(url))
                    if hs_val:
                        state.setdefault("failed", []).append(hs_val)
                        save_progress(progress_file, state)
                pbar.update(1)


# ---------------------------------------------------------------------------
# COMMAND: upload (legacy)
# ---------------------------------------------------------------------------

def log_upload_failed(failed_file, category, folder, identifier, reason):
    with _upload_failed_lock:
        failed = []
        if failed_file.exists():
            try:
                failed = json.loads(failed_file.read_text())
            except json.JSONDecodeError:
                pass
        failed.append({"category": category, "folder": folder, "identifier": identifier,
                       "reason": reason, "time": datetime.now(timezone.utc).isoformat()})
        tmp = failed_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(failed, indent=2))
        tmp.replace(failed_file)


def cmd_upload(args):
    cfg = load_config(getattr(args, 'config', None))
    prefix = get_config("prefix", "shg.", cfg, args)
    collection = get_config("ia_collection", "shodhganga", cfg, args)
    try:
        r = subprocess.run(["ia", "--version"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log.error("ia CLI not available. Install with: pip install internetarchive")
            sys.exit(1)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.error("ia CLI check failed: %s. Install with: pip install internetarchive", e)
        sys.exit(1)

    workers = clamp_workers(get_config("workers", 3, cfg, args), MAX_WORKERS_UPLOAD)
    ia_timeout = get_config("ia_timeout", 300, cfg, args)
    ia_retries = get_config("ia_retries", 3, cfg, args)
    no_collection_check = get_config("no_collection_check", False, cfg, args)

    output_dir = Path(args.dir)
    if not output_dir.exists():
        log.error("Directory %s not found", output_dir)
        sys.exit(1)

    progress_file = output_dir / "progress.json"
    failed_file = output_dir / "upload_failed.json"
    state = load_progress(progress_file)
    uploaded_ids = set(stages_to_ids(state, STAGE_UPLOADED, STAGE_CLEANED))

    retry_mode = getattr(args, 'retry', False)
    retry_targets = set()
    if retry_mode:
        if failed_file.exists():
            try:
                failed_entries = json.loads(failed_file.read_text())
                retry_targets = {f.get("identifier", "") for f in failed_entries}
            except (json.JSONDecodeError, OSError):
                pass
        if not retry_targets:
            log.info("No failed items to retry.")
            return
        log.info("Retry mode: %d items", len(retry_targets))

    items_to_upload = []
    for item_dir in sorted(output_dir.iterdir()):
        if not item_dir.is_dir() or item_dir.name.startswith("."):
            continue
        meta_file = item_dir / "metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        identifier = make_identifier(prefix, meta.get("handle_suffix", ""), meta.get("title", ""), item_dir)
        if identifier in uploaded_ids:
            continue
        if retry_mode and identifier not in retry_targets:
            continue
        if not meta.get("handle_suffix"):
            from_dir = item_dir.name.split("_")[0]
            hs_candidate = from_dir if from_dir.isdigit() else ""
            meta["handle_suffix"] = hs_candidate
            meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        pdfs = sorted(item_dir.glob("*.pdf"))
        if not pdfs:
            continue
        items_to_upload.append((identifier, item_dir, meta))

    if not items_to_upload:
        log.info("No items to upload.")
        return

    log.info("Uploading %d items (workers: %d)", len(items_to_upload), workers)

    def upload_one(identifier, item_dir, meta):
        pdfs = sorted(item_dir.glob("*.pdf"))
        files = []
        for f in pdfs:
            renamed = f.parent / f"{identifier}.pdf"
            if f != renamed:
                if renamed.exists():
                    renamed.unlink()
                shutil.move(str(f), str(renamed))
                files.append(str(renamed))
            else:
                files.append(str(f))
        _upload_semaphore.acquire()
        try:
            ok = upload_to_ia(identifier, files, meta, collection,
                              no_collection_check, ia_timeout, ia_retries)
        finally:
            _upload_semaphore.release()
        return identifier, item_dir, ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(upload_one, ident, d, m): (ident, d) for ident, d, m in items_to_upload}
        for future in concurrent.futures.as_completed(futures):
            ident, item_dir = futures[future]
            try:
                ident, item_dir, ok = future.result()
                if ok:
                    uploaded_ids.add(ident)
                    hs = extract_handle_from_identifier(ident)
                    set_item_stage(state, hs, STAGE_UPLOADED)
                    save_progress(progress_file, state)
                    if args.cleanup:
                        shutil.rmtree(item_dir, ignore_errors=True)
                else:
                    log_upload_failed(failed_file, collection, item_dir.name, ident, "upload failed")
            except Exception as e:
                log.error("  Exception: %s", e)
                log_upload_failed(failed_file, collection, item_dir.name, ident, str(e))


def extract_handle_from_identifier(identifier: str) -> str:
    if not identifier:
        return ""
    parts = identifier.split(".")
    if len(parts) >= 2:
        after_dot = parts[1]
        hs = after_dot.split("-")[0]
        if hs.isdigit():
            return hs
    parts = identifier.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Shodhganga Corpus Toolkit — Downloader + Uploader + Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List all item URLs from browse pages")
    p_list.add_argument("--output", default="listing.json")
    p_list.add_argument("--rpp", type=int, default=100)
    p_list.add_argument("--max-items", type=int)
    p_list.add_argument("--resume", action="store_true",
                        help="Resume from existing listing file (uses --output)")
    p_list.add_argument("--refresh", type=int, default=500,
                        help="Session refresh interval (requests). Default: 500")
    p_list.add_argument("--delay", type=float, default=2.0,
                        help="Base delay between requests (seconds). Default: 2.0")
    p_list.add_argument("--connect-timeout", type=int, default=15,
                        help="TCP connect timeout in seconds. Default: 15. "
                             "Lower values skip unreachable servers faster.")
    p_list.add_argument("--log-file")

    p_lc = sub.add_parser("list-communities",
                          help="List all university/department communities")
    p_lc.add_argument("--output", default="university_listing.json")
    p_lc.add_argument("--log-file")

    p_man = sub.add_parser("manifest", help="Fetch metadata for all items (no PDF downloads)")
    p_man.add_argument("--input", help="JSON listing from 'list' command")
    p_man.add_argument("--urls", help="Single URL or file of URLs")
    p_man.add_argument("--dir", default="shg_corpus")
    p_man.add_argument("--workers", type=int, default=None,
                       help=f"Workers (env: SHG_WORKERS_DOWNLOAD, default: 10, max: {MAX_WORKERS_DOWNLOAD})")
    p_man.add_argument("--prefix", default=None)
    p_man.add_argument("--log-file")
    p_man.add_argument("--config")
    p_man.add_argument("--limit", type=int, default=None,
                       help="Only process first N items (for testing)")
    p_man.add_argument("--language", default=None,
                       help="Only include items matching this language (e.g. kannada, kn)")
    p_man.add_argument("--save-interval", type=int, default=1000,
                       help="Save checkpoint every N items (default: 1000)")

    p_pipe = sub.add_parser("pipeline", help="Per-item: metadata -> download -> merge -> upload -> cleanup")
    p_pipe.add_argument("--dir", default="shg_corpus")
    p_pipe.add_argument("--workers", type=int, default=None,
                        help=f"Workers (env: SHG_WORKERS_DOWNLOAD, default: 5, max: {MAX_WORKERS_DOWNLOAD})")
    p_pipe.add_argument("--prefix", default=None)
    p_pipe.add_argument("--collection", default=None)
    p_pipe.add_argument("--cleanup", action="store_true", help="Remove local files after successful upload")
    p_pipe.add_argument("--skip-upload", action="store_true", help="Download + merge only, skip upload")
    p_pipe.add_argument("--no-collection-check", action="store_true", default=None)
    p_pipe.add_argument("--ia-timeout", type=int, default=None)
    p_pipe.add_argument("--ia-retries", type=int, default=None)
    p_pipe.add_argument("--no-verify-ssl", action="store_true", default=False,
                        help="Disable SSL certificate verification for port-8443 PDF downloads. "
                             "Use on macOS if the proxy cert is rejected (SSLError). "
                             "Equivalent to SHG_NO_VERIFY_SSL=1.")
    p_pipe.add_argument("--log-file")
    p_pipe.add_argument("--config")
    p_pipe.add_argument("--language", default=None,
                        help="Only process items matching this language (e.g. kannada, kn)")
    p_pipe.add_argument("--limit", type=int, default=None,
                        help="Only process first N items (for testing)")

    p_down = sub.add_parser("download", help="Download items and extract metadata (legacy)")
    p_down.add_argument("--urls")
    p_down.add_argument("--input")
    p_down.add_argument("--dir", default="shg_corpus")
    p_down.add_argument("--workers", type=int, default=None)
    p_down.add_argument("--resume", action="store_true")
    p_down.add_argument("--retry", action="store_true")
    p_down.add_argument("--prefix", default=None)
    p_down.add_argument("--log-file")
    p_down.add_argument("--config")
    p_down.add_argument("--no-verify-ssl", action="store_true", default=False,
                        help="Disable SSL certificate verification for port-8443 PDF downloads. "
                             "Use on macOS if the proxy cert is rejected (SSLError). "
                             "Equivalent to SHG_NO_VERIFY_SSL=1.")

    p_up = sub.add_parser("upload", help="Upload downloaded items to Internet Archive (legacy)")
    p_up.add_argument("--dir", default="shg_corpus")
    p_up.add_argument("--workers", type=int, default=None)
    p_up.add_argument("--collection", default=None)
    p_up.add_argument("--prefix", default=None)
    p_up.add_argument("--cleanup", action="store_true")
    p_up.add_argument("--no-collection-check", action="store_true", default=None)
    p_up.add_argument("--ia-timeout", type=int, default=None)
    p_up.add_argument("--ia-retries", type=int, default=None)
    p_up.add_argument("--log-file")
    p_up.add_argument("--retry", action="store_true")
    p_up.add_argument("--config")

    p_st = sub.add_parser("status", help="Show stage-wise progress with data sizes")
    p_st.add_argument("--dir", default="shg_corpus")
    p_st.add_argument("--verbose", "-v", action="store_true")
    p_st.add_argument("--prefix", default=None)
    p_st.add_argument("--log-file")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    log_file = getattr(args, 'log_file', None)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        log_fh = logging.FileHandler(log_file, mode='a')
        log_fh.setLevel(logging.INFO)
        log_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(log_fh)

    cmds = {
        "list": cmd_list,
        "list-communities": cmd_list_communities,
        "manifest": cmd_manifest,
        "pipeline": cmd_pipeline,
        "download": cmd_download,
        "upload": cmd_upload,
        "status": cmd_status,
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
