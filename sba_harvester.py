#!/usr/bin/env python3
"""Incremental metadata, download, and IA pipeline for the APU School Books Archive.

Inventory, enrichment, export, download, and upload are independently resumable.
Network-changing stages are dry-run by default and require an explicit --execute.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import re
import subprocess
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://schoolbooksarchive.azimpremjiuniversity.edu.in"
POLICY_URL = f"{BASE_URL}/DataPolicy.html"
USER_AGENT = "ServantsOfKnowledge-SBA-Metadata-Harvester/1.0 (+https://github.com/ServantsOfKnowledge/digital-archive-corpus-toolkit)"
IA_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:archive\.org|web\.archive\.org)/[^\s<>\"']+",
    re.IGNORECASE,
)
IA_ID_RE = re.compile(
    r"https?://(?:www\.)?archive\.org/(?:details|download|metadata)/([^/?#\s]+)",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"/handle/([^/?#]+/[^/?#]+)")
BITSTREAM_VALUE_RE = re.compile(r"\bvalue=([^\s>]+)", re.IGNORECASE)
_local = threading.local()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> Any:
    if isinstance(value, str):
        previous = value
        for _ in range(3):
            current = html.unescape(previous)
            if current == previous:
                break
            previous = current
        return previous.strip()
    if isinstance(value, list):
        return [clean_text(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_text(v) for k, v in value.items()}
    return value


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from iter_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from iter_strings(child)


def detect_ia_references(*values: Any) -> tuple[list[str], list[str]]:
    urls: set[str] = set()
    identifiers: set[str] = set()
    for value in values:
        for text in iter_strings(value):
            for raw in IA_URL_RE.findall(text):
                url = raw.rstrip(".,);]}")
                urls.add(url)
                match = IA_ID_RE.search(url)
                if match:
                    identifiers.add(match.group(1))
    return sorted(urls), sorted(identifiers)


def is_direct_ia_pdf_url(url: str) -> bool:
    parsed = urlparse(unquote(url))
    hostname = (parsed.hostname or "").lower()
    return hostname in {"archive.org", "www.archive.org"} and parsed.path.lower().endswith(".pdf")


def extract_handle(item_url: str) -> str:
    match = HANDLE_RE.search(item_url or "")
    if not match:
        raise ValueError(f"No Handle identifier in item URL: {item_url!r}")
    return match.group(1)


def session() -> requests.Session:
    current = getattr(_local, "session", None)
    if current is None:
        current = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
        )
        current.mount("https://", HTTPAdapter(max_retries=retry))
        current.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _local.session = current
    return current


def get_json(path: str, params: dict[str, Any] | None = None, timeout: int = 60) -> Any:
    response = session().get(urljoin(BASE_URL, path), params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    handle TEXT PRIMARY KEY,
    uuid TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT,
    type TEXT,
    date TEXT,
    list_json TEXT NOT NULL,
    metadata_json TEXT,
    bitstreams_json TEXT,
    ia_urls_json TEXT,
    ia_identifiers_json TEXT,
    classification TEXT NOT NULL DEFAULT 'metadata_pending',
    listed_at TEXT NOT NULL,
    enriched_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS items_classification_idx ON items(classification);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
    handle TEXT NOT NULL,
    bitstream_id TEXT NOT NULL,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(handle, bitstream_id)
);
CREATE TABLE IF NOT EXISTS ia_uploads (
    handle TEXT PRIMARY KEY,
    ia_identifier TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL
);
"""


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get_state(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        "INSERT OR REPLACE INTO sync_state(key, value) VALUES(?, ?)",
        (key, str(value)),
    )


def inventory(
    db_path: Path, page_size: int, max_items: int | None, delay: float, restart: bool = False
) -> None:
    seen = 0
    expected = None
    with closing(connect_db(db_path)) as db:
        start = 0 if restart else int(get_state(db, "inventory_next_start", "0") or 0)
        listed_at = get_state(db, "inventory_run_started_at") if start else None
        if not listed_at:
            listed_at = utc_now()
            set_state(db, "inventory_run_started_at", listed_at)
        if start:
            logging.info("Resuming inventory at source offset %s", start)
        while expected is None or start < expected:
            limit = page_size
            if max_items is not None:
                limit = min(limit, max_items - seen)
                if limit <= 0:
                    break
            payload = get_json("/dspace-mvc/getItemSearch", {"rpp": limit, "start": start})
            rows = payload.get("data") or []
            expected = int(payload.get("size") or len(rows))
            if not rows:
                break
            for raw in rows:
                item = clean_text(raw)
                handle = extract_handle(item.get("itemurl", ""))
                db.execute(
                    """INSERT INTO items
                       (handle, uuid, source_url, title, type, date, list_json, listed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(handle) DO UPDATE SET
                         uuid=excluded.uuid, source_url=excluded.source_url,
                         title=excluded.title, type=excluded.type, date=excluded.date,
                         list_json=excluded.list_json, listed_at=excluded.listed_at""",
                    (
                        handle,
                        item.get("productName", ""),
                        urljoin(BASE_URL, item.get("itemurl", "")),
                        item.get("displayTitle", ""),
                        item.get("displayType", ""),
                        item.get("displayDate", ""),
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                        listed_at,
                    ),
                )
            seen += len(rows)
            start += len(rows)
            set_state(db, "source_inventory_size", expected)
            set_state(db, "inventory_next_start", start)
            db.commit()
            logging.info("Inventory: fetched %s this run; source offset %s/%s", seen, start, expected)
            if len(rows) < limit:
                break
            if delay:
                time.sleep(delay)
        if expected is not None and start >= expected:
            set_state(db, "inventory_next_start", 0)
            set_state(db, "last_inventory_at", utc_now())
            set_state(db, "inventory_run_started_at", "")
            db.commit()
            logging.info("Inventory snapshot complete: %s source records", expected)


def normalize_bitstreams(handle: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for raw in payload.get("data") or []:
        bitstream = clean_text(raw)
        viewer_html = bitstream.pop("html", "")
        bitstream.pop("lock", None)
        direct = BITSTREAM_VALUE_RE.search(viewer_html)
        bitstream["download_url"] = urljoin(BASE_URL, direct.group(1)) if direct else None
        bitstream["viewer_url"] = (
            f"{BASE_URL}/displaybitstream?handle={quote(handle)}"
            f"&fileid={quote(str(bitstream.get('internal_id', '')))}"
        )
        result.append(bitstream)
    return result


def safe_component(value: str, fallback: str = "file") -> str:
    value = value.replace("/", "_").replace("\\", "_").replace("\x00", "")
    value = re.sub(r"[\r\n\t]+", " ", value).strip(" .") or fallback
    while len(value.encode("utf-8")) > 220:
        value = value[:-1]
    return value


def item_directory(root: Path, record: dict[str, Any]) -> Path:
    suffix = record["handle"].split("/")[-1]
    title = safe_component(record.get("title") or "untitled")[:80]
    return root / f"{suffix}_{title}"


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def load_manifest(folder: Path) -> dict[str, Any]:
    manifest = folder / "manifest.json"
    if not manifest.exists():
        return {"complete": False, "files": []}
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"complete": False, "files": []}
    except (OSError, ValueError, TypeError):
        return {"complete": False, "files": []}


def load_manifest_states(folder: Path) -> dict[str, dict[str, Any]]:
    return {
        str(item["bitstream_id"]): item
        for item in load_manifest(folder).get("files", [])
        if item.get("bitstream_id")
    }


def state_file_is_complete(state: dict[str, Any]) -> bool:
    if state.get("status") not in {"downloaded", "already_downloaded"}:
        return False
    path_value = state.get("path")
    if not path_value:
        return False
    path = Path(path_value)
    if not path.is_file():
        return False
    recorded_size = int(state.get("size") or 0)
    return recorded_size > 0 and path.stat().st_size == recorded_size


def record_download_is_complete(record: dict[str, Any], root: Path) -> bool:
    if record["classification"] == "record_only_ia_linked":
        return True
    required = [
        str(item.get("internal_id", ""))
        for item in record["bitstreams"]
        if item.get("internal_id") and not item.get("islock")
    ]
    folder = item_directory(root, record)
    manifest = load_manifest(folder)
    if not manifest.get("complete"):
        return False
    if not required:
        return True
    states = load_manifest_states(folder)
    return all(state_file_is_complete(states.get(bitstream_id, {})) for bitstream_id in required)


def download_one(record: dict[str, Any], root: Path, execute: bool) -> list[dict[str, Any]]:
    if record["classification"] == "record_only_ia_linked":
        return [{"status": "skipped_existing_ia_reference", "handle": record["handle"]}]
    fresh = get_json("/dspace-mvc/getItemBitstreamViewer", {"handle": record["handle"]})
    bitstreams = normalize_bitstreams(record["handle"], fresh)
    folder = item_directory(root, record)
    previous = load_manifest_states(folder)
    planned = []

    def checkpoint() -> None:
        merged = dict(previous)
        merged.update({state["bitstream_id"]: state for state in planned if state.get("bitstream_id")})
        write_json_atomic(
            folder / "manifest.json",
            {"complete": False, "files": list(merged.values()), "updated_at": utc_now()},
        )

    for bitstream in bitstreams:
        state = {
            "handle": record["handle"],
            "bitstream_id": str(bitstream.get("internal_id", "")),
            "name": (
                f"{int(bitstream.get('sequenceId') or 0):03d}_"
                f"{safe_component(str(bitstream.get('name') or 'file.bin'))}"
            ),
            "download_url": bitstream.get("download_url"),
            "status": "planned",
        }
        planned.append(state)
        if not execute:
            continue
        if bitstream.get("islock") or not bitstream.get("download_url"):
            state["status"] = "skipped_locked_or_missing_url"
            continue
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / state["name"]
        partial = destination.with_suffix(destination.suffix + ".part")
        old_state = previous.get(state["bitstream_id"], {})
        if state_file_is_complete(old_state):
            state.update(old_state)
            state["status"] = "already_downloaded"
            checkpoint()
            continue
        digest = hashlib.sha256()
        response = session().get(bitstream["download_url"], stream=True, timeout=(30, 600))
        response.raise_for_status()
        expected = int(response.headers.get("Content-Length") or 0)
        size = 0
        try:
            with partial.open("wb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            if expected and size != expected:
                raise IOError(f"size mismatch: received {size}, expected {expected}")
            partial.replace(destination)
            state.update(
                status="downloaded", path=str(destination.resolve()),
                size=size, sha256=digest.hexdigest(),
            )
            checkpoint()
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    if execute:
        write_json_atomic(folder / "metadata.json", ia_metadata(record, "AzimPremjiUniversity"))
        write_json_atomic(folder / "source_record.json", record)
        write_json_atomic(
            folder / "manifest.json",
            {"complete": True, "files": planned, "updated_at": utc_now()},
        )
    return planned


def download_records(
    db_path: Path, root: Path, workers: int, limit: int | None, execute: bool
) -> None:
    with closing(connect_db(db_path)) as db:
        rows = db.execute(
            "SELECT * FROM items WHERE metadata_json IS NOT NULL ORDER BY handle"
        ).fetchall()
        records = [
            record for record in (row_to_record(row) for row in rows)
            if not record_download_is_complete(record, root)
        ]
        if limit is not None:
            records = records[:limit]
        if not records:
            logging.info("No incomplete enriched records available")
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(download_one, record, root, execute): record for record in records}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    states = future.result()
                    for state in states:
                        if "bitstream_id" not in state:
                            continue
                        db.execute(
                            """INSERT OR REPLACE INTO downloads
                               (handle, bitstream_id, path, size, sha256, status, error, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                            (
                                record["handle"], state["bitstream_id"], state.get("path"),
                                state.get("size"), state.get("sha256"), state["status"], utc_now(),
                            ),
                        )
                    logging.info("%s: %s", record["handle"], ", ".join(s["status"] for s in states))
                except Exception as exc:
                    db.execute(
                        """INSERT OR REPLACE INTO downloads
                           (handle, bitstream_id, status, error, updated_at)
                           VALUES (?, '*', 'failed', ?, ?)""",
                        (record["handle"], str(exc), utc_now()),
                    )
                    logging.error("%s: %s", record["handle"], exc)
                db.commit()


def metadata_flags(metadata: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for key, value in metadata.items():
        values = value if isinstance(value, list) else [value]
        for child in values:
            if child not in (None, ""):
                flags.extend(["--metadata", f"{key}:{child}"])
    return flags


def ia_exists(identifier: str) -> bool:
    result = subprocess.run(
        ["ia", "metadata", identifier], capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        return False
    try:
        return "metadata" in json.loads(result.stdout)
    except json.JSONDecodeError:
        return False


def upload_records(
    db_path: Path, root: Path, limit: int | None, execute: bool, collection: str
) -> None:
    with closing(connect_db(db_path)) as db:
        terminal = {"uploaded", "already_on_ia", "skipped_existing_ia_reference"}
        completed = {
            row["handle"]: row["status"]
            for row in db.execute("SELECT handle, status FROM ia_uploads")
            if row["status"] in terminal
        }
        rows = db.execute(
            "SELECT * FROM items WHERE metadata_json IS NOT NULL ORDER BY handle"
        ).fetchall()
        candidates: list[tuple[dict[str, Any], list[Path]]] = []
        for row in rows:
            record = row_to_record(row)
            if record["handle"] in completed:
                continue
            identifier = "apu.sba." + record["handle"].split("/")[-1] + ".1"
            if record["internet_archive_identifiers"] or record["internet_archive_urls"]:
                logging.info("%s: skip; source already references IA", record["handle"])
                db.execute(
                    "INSERT OR REPLACE INTO ia_uploads VALUES (?, ?, ?, NULL, ?)",
                    (record["handle"], identifier, "skipped_existing_ia_reference", utc_now()),
                )
                continue
            folder = item_directory(root, record)
            files = sorted(
                path for path in folder.iterdir()
                if path.is_file() and path.name not in {"metadata.json", "manifest.json", "source_record.json"}
            ) if folder.exists() else []
            if not files or not record_download_is_complete(record, root):
                continue
            candidates.append((record, files))
        db.commit()
        if limit is not None:
            candidates = candidates[:limit]
        if not candidates:
            logging.info("No downloaded records remain to upload")
            return
        for record, files in candidates:
            identifier = "apu.sba." + record["handle"].split("/")[-1] + ".1"
            error = None
            if ia_exists(identifier):
                logging.info("%s: skip; %s already exists", record["handle"], identifier)
                status = "already_on_ia"
            elif not execute:
                logging.info("%s: would upload %s files as %s", record["handle"], len(files), identifier)
                status = "planned"
            else:
                command = [
                    "ia", "upload", identifier, *map(str, files),
                    *metadata_flags(ia_metadata(record, collection)), "--checksum", "--verify",
                ]
                result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
                if result.returncode:
                    status = "failed"
                    error = (result.stderr or result.stdout).strip()
                    logging.error("%s: %s", identifier, error)
                else:
                    status = "uploaded"
                    logging.info("%s: uploaded", identifier)
            db.execute(
                "INSERT OR REPLACE INTO ia_uploads VALUES (?, ?, ?, ?, ?)",
                (record["handle"], identifier, status, error, utc_now()),
            )
            db.commit()


def fetch_details(row: sqlite3.Row) -> dict[str, Any]:
    metadata = clean_text(
        get_json(
            "/dspace-mvc/getMetaDataValues",
            {"itemID": row["uuid"], "configProp": "metadata.ItemDisplay"},
        )
    )
    bitstreams = normalize_bitstreams(
        row["handle"],
        get_json("/dspace-mvc/getItemBitstreamViewer", {"handle": row["handle"]}),
    )
    ia_urls, ia_identifiers = detect_ia_references(metadata, bitstreams)
    classification = "record_only_ia_linked" if ia_urls or ia_identifiers else "download_candidate"
    return {
        "handle": row["handle"],
        "metadata": metadata,
        "bitstreams": bitstreams,
        "ia_urls": ia_urls,
        "ia_identifiers": ia_identifiers,
        "classification": classification,
    }


def enrich(
    db_path: Path,
    workers: int,
    limit: int | None,
    refresh: bool,
    restart: bool = False,
) -> None:
    with closing(connect_db(db_path)) as db:
        refresh_started_at = get_state(db, "enrich_refresh_started_at")
        if restart or (refresh and not refresh_started_at):
            refresh_started_at = utc_now()
            set_state(db, "enrich_refresh_started_at", refresh_started_at)
            db.commit()
            logging.info("Started a new full enrichment refresh")
        elif refresh_started_at:
            logging.info("Resuming full enrichment refresh started at %s", refresh_started_at)

        if refresh_started_at:
            where = "enriched_at IS NULL OR enriched_at < ? OR error IS NOT NULL"
            params: tuple[Any, ...] = (refresh_started_at,)
        else:
            where = "metadata_json IS NULL OR error IS NOT NULL"
            params = ()
        sql = f"SELECT * FROM items WHERE {where} ORDER BY (error IS NOT NULL), handle"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        rows = db.execute(sql, params).fetchall()
        if not rows:
            if refresh_started_at:
                set_state(db, "enrich_refresh_started_at", "")
                set_state(db, "last_enrichment_refresh_at", utc_now())
                db.commit()
                logging.info("Full enrichment refresh complete")
            logging.info("No records require enrichment")
            return
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_details, row): row["handle"] for row in rows}
            for future in as_completed(futures):
                handle = futures[future]
                try:
                    result = future.result()
                    db.execute(
                        """UPDATE items SET metadata_json=?, bitstreams_json=?, ia_urls_json=?,
                           ia_identifiers_json=?, classification=?, enriched_at=?, error=NULL
                           WHERE handle=?""",
                        (
                            json.dumps(result["metadata"], ensure_ascii=False, sort_keys=True),
                            json.dumps(result["bitstreams"], ensure_ascii=False, sort_keys=True),
                            json.dumps(result["ia_urls"], ensure_ascii=False),
                            json.dumps(result["ia_identifiers"], ensure_ascii=False),
                            result["classification"],
                            utc_now(),
                            handle,
                        ),
                    )
                except Exception as exc:  # preserve failure for a future retry
                    db.execute("UPDATE items SET error=? WHERE handle=?", (str(exc), handle))
                    logging.error("%s: %s", handle, exc)
                done += 1
                if done % 25 == 0 or done == len(rows):
                    db.commit()
                    logging.info("Enrichment: %s/%s records", done, len(rows))
        if refresh_started_at:
            remaining = db.execute(
                f"SELECT COUNT(*) FROM items WHERE {where}",
                (refresh_started_at,),
            ).fetchone()[0]
            if remaining == 0:
                set_state(db, "enrich_refresh_started_at", "")
                set_state(db, "last_enrichment_refresh_at", utc_now())
                db.commit()
                logging.info("Full enrichment refresh complete")
            else:
                logging.info("Full enrichment refresh checkpointed; %s records remain", remaining)


def row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    def load(name: str, fallback: Any) -> Any:
        value = row[name]
        return json.loads(value) if value else fallback

    return {
        "handle": row["handle"],
        "uuid": row["uuid"],
        "source_url": row["source_url"],
        "title": row["title"],
        "type": row["type"],
        "date": row["date"],
        "metadata": load("metadata_json", {}),
        "bitstreams": load("bitstreams_json", []),
        "internet_archive_urls": load("ia_urls_json", []),
        "internet_archive_identifiers": load("ia_identifiers_json", []),
        "classification": row["classification"],
        "listed_at": row["listed_at"],
        "enriched_at": row["enriched_at"],
        "error": row["error"],
    }


def export_records(db_path: Path, jsonl_path: Path, csv_path: Path | None) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    temp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    counts: dict[str, int] = {}
    with closing(connect_db(db_path)) as db, temp.open("w", encoding="utf-8") as stream:
        rows = db.execute("SELECT * FROM items ORDER BY handle").fetchall()
        records = [row_to_record(row) for row in rows]
        for record in records:
            counts[record["classification"]] = counts.get(record["classification"], 0) + 1
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(jsonl_path)
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            fields = [
                "handle", "uuid", "title", "type", "date", "source_url",
                "classification", "internet_archive_identifiers",
                "internet_archive_urls", "bitstream_count", "error",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({
                    **{key: record.get(key) for key in fields},
                    "internet_archive_identifiers": " | ".join(record["internet_archive_identifiers"]),
                    "internet_archive_urls": " | ".join(record["internet_archive_urls"]),
                    "bitstream_count": len(record["bitstreams"]),
                })
    logging.info("Exported %s records (%s)", len(records), counts)


def collect_status(db_path: Path) -> dict[str, int]:
    with closing(connect_db(db_path)) as db:
        rows = db.execute("SELECT * FROM items ORDER BY handle").fetchall()
        records = [row_to_record(row) for row in rows]
        download_counts = {
            row["status"]: row["count"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM downloads GROUP BY status"
            )
        }
        upload_counts = {
            row["status"]: row["count"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM ia_uploads GROUP BY status"
            )
        }

    enriched = 0
    errored = 0
    ia_linked = 0
    ia_metadata_source = 0
    ia_bitstream_source = 0
    direct_ia_pdf = 0
    identifiers: set[str] = set()
    for record in records:
        if record["metadata"]:
            enriched += 1
        if record["error"]:
            errored += 1
        metadata_urls, metadata_ids = detect_ia_references(record["metadata"])
        bitstream_urls, bitstream_ids = detect_ia_references(record["bitstreams"])
        aggregate_urls = set(record["internet_archive_urls"])
        aggregate_ids = set(record["internet_archive_identifiers"])
        all_urls = aggregate_urls | set(metadata_urls) | set(bitstream_urls)
        all_ids = aggregate_ids | set(metadata_ids) | set(bitstream_ids)
        if all_urls or all_ids:
            ia_linked += 1
        if metadata_urls or metadata_ids:
            ia_metadata_source += 1
        if bitstream_urls or bitstream_ids:
            ia_bitstream_source += 1
        if any(is_direct_ia_pdf_url(url) for url in all_urls):
            direct_ia_pdf += 1
        identifiers.update(all_ids)

    return {
        "inventory_items": len(records),
        "enriched_items": enriched,
        "pending_enrichment_items": len(records) - enriched,
        "enrichment_errors": errored,
        "archive_org_linked_items": ia_linked,
        "archive_org_metadata_source_items": ia_metadata_source,
        "archive_org_bitstream_source_items": ia_bitstream_source,
        "direct_archive_org_pdf_items": direct_ia_pdf,
        "unique_archive_org_identifiers": len(identifiers),
        "downloaded_files": download_counts.get("downloaded", 0),
        "download_failures": download_counts.get("failed", 0),
        "ia_uploaded_items": upload_counts.get("uploaded", 0),
        "ia_upload_failures": upload_counts.get("failed", 0),
    }


def print_status(db_path: Path, json_output: bool = False) -> None:
    stats = collect_status(db_path)
    if json_output:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return
    labels = {
        "inventory_items": "Inventory items",
        "enriched_items": "Enriched items",
        "pending_enrichment_items": "Pending enrichment",
        "enrichment_errors": "Enrichment errors",
        "archive_org_linked_items": "Any Archive.org-linked items",
        "archive_org_metadata_source_items": "Archive.org in source metadata",
        "archive_org_bitstream_source_items": "Archive.org in file records",
        "direct_archive_org_pdf_items": "Direct Archive.org PDF items",
        "unique_archive_org_identifiers": "Unique Archive.org identifiers",
        "downloaded_files": "Downloaded files",
        "download_failures": "Download failures",
        "ia_uploaded_items": "IA uploaded items",
        "ia_upload_failures": "IA upload failures",
    }
    width = max(len(label) for label in labels.values())
    for key, label in labels.items():
        print(f"{label:<{width}}  {stats[key]:>8,}")


def first(metadata: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = metadata.get(name)
        if value not in (None, "", []):
            return value
    return default


def ia_metadata(record: dict[str, Any], collection: str | None) -> dict[str, Any]:
    metadata = record["metadata"]
    result = {
        "title": first(metadata, "Title", default=record["title"]),
        "creator": first(metadata, "Author(s)", "Author", "Authors"),
        "publisher": first(metadata, "Publisher"),
        "date": first(metadata, "Date of Issue", "Date of Publication", default=record["date"]),
        "language": first(metadata, "Language"),
        "subject": first(metadata, "Subject(s)", "Keyword(s)", default=[]),
        "description": first(metadata, "Abstract"),
        "mediatype": "texts",
        "originalurl": record["source_url"],
        "source": "School Books Archive, Azim Premji University",
        "identifier-access": record["handle"],
    }
    if collection:
        result["collection"] = collection
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def make_ia_plan(db_path: Path, output: Path, collection: str | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    written = 0
    with closing(connect_db(db_path)) as db, temp.open("w", encoding="utf-8") as stream:
        for row in db.execute("SELECT * FROM items ORDER BY handle"):
            record = row_to_record(row)
            if record["classification"] == "record_only_ia_linked":
                action = "skip_existing_ia_reference"
            elif not record["metadata"]:
                action = "skip_metadata_incomplete"
            else:
                action = "download_and_upload_candidate"
            plan = {
                "action": action,
                "ia_identifier": "apu.sba." + record["handle"].split("/")[-1] + ".1",
                "source_handle": record["handle"],
                "source_url": record["source_url"],
                "existing_ia_identifiers": record["internet_archive_identifiers"],
                "existing_ia_urls": record["internet_archive_urls"],
                "metadata": ia_metadata(record, collection),
                "files": record["bitstreams"],
            }
            stream.write(json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    temp.replace(output)
    logging.info("Wrote %s IA plan records to %s", written, output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("sba_data/sba.sqlite3"))
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="Fetch/upsert the complete public item listing")
    inv.add_argument("--page-size", type=int, default=120, choices=range(1, 121), metavar="1..120")
    inv.add_argument("--max-items", type=int, help="Development/testing limit")
    inv.add_argument("--delay", type=float, default=0.25, help="Delay between listing pages")
    inv.add_argument("--restart", action="store_true", help="Start a fresh inventory pass at offset zero")

    enr = sub.add_parser("enrich", help="Fetch metadata and bitstream records; resumable")
    enr.add_argument("--workers", type=int, default=3)
    enr.add_argument("--limit", type=int, help="Development/testing limit")
    enr.add_argument(
        "--refresh", action="store_true",
        help="Start or resume a full refresh; later plain enrich commands also resume it",
    )
    enr.add_argument(
        "--restart", action="store_true",
        help="Discard the current refresh checkpoint and start a new full refresh",
    )

    exp = sub.add_parser("export", help="Export normalized JSONL and optional CSV")
    exp.add_argument("--jsonl", type=Path, default=Path("sba_data/sba_records.jsonl"))
    exp.add_argument("--csv", type=Path, default=Path("sba_data/sba_records.csv"))

    status = sub.add_parser("status", help="Show harvest progress and Archive.org source counts")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    plan = sub.add_parser("ia-plan", help="Create a non-executing Internet Archive plan")
    plan.add_argument("--output", type=Path, default=Path("sba_data/ia_plan.jsonl"))
    plan.add_argument("--collection", help="IA collection, only if permission has been confirmed")

    download = sub.add_parser("download", help="Download non-IA-linked files; dry-run by default")
    download.add_argument("--root", type=Path, default=Path("sba_corpus"))
    download.add_argument("--workers", type=int, default=2)
    download.add_argument("--limit", type=int)
    download.add_argument("--execute", action="store_true", help="Perform downloads")

    upload = sub.add_parser("ia-upload", help="Upload downloaded files with ia CLI; dry-run by default")
    upload.add_argument("--root", type=Path, default=Path("sba_corpus"))
    upload.add_argument("--limit", type=int)
    upload.add_argument("--collection", default="AzimPremjiUniversity")
    upload.add_argument("--execute", action="store_true", help="Perform uploads")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.command == "inventory":
        inventory(args.db, args.page_size, args.max_items, args.delay, args.restart)
    elif args.command == "enrich":
        enrich(args.db, args.workers, args.limit, args.refresh, args.restart)
    elif args.command == "export":
        export_records(args.db, args.jsonl, args.csv)
    elif args.command == "status":
        print_status(args.db, args.json)
    elif args.command == "ia-plan":
        make_ia_plan(args.db, args.output, args.collection)
    elif args.command == "download":
        download_records(args.db, args.root, args.workers, args.limit, args.execute)
    elif args.command == "ia-upload":
        upload_records(args.db, args.root, args.limit, args.execute, args.collection)


if __name__ == "__main__":
    main()
