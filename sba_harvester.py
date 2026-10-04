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
from urllib.parse import quote, urljoin

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


def inventory(db_path: Path, page_size: int, max_items: int | None, delay: float) -> None:
    listed_at = utc_now()
    start = 0
    seen = 0
    expected = None
    with closing(connect_db(db_path)) as db:
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
            db.execute(
                "INSERT OR REPLACE INTO sync_state(key, value) VALUES('source_inventory_size', ?)",
                (str(expected),),
            )
            db.execute(
                "INSERT OR REPLACE INTO sync_state(key, value) VALUES('last_inventory_at', ?)",
                (listed_at,),
            )
            db.commit()
            logging.info("Inventory: %s/%s records", seen, expected)
            if len(rows) < limit:
                break
            if delay:
                time.sleep(delay)


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


def download_one(record: dict[str, Any], root: Path, execute: bool) -> list[dict[str, Any]]:
    if record["classification"] == "record_only_ia_linked":
        return [{"status": "skipped_existing_ia_reference", "handle": record["handle"]}]
    fresh = get_json("/dspace-mvc/getItemBitstreamViewer", {"handle": record["handle"]})
    bitstreams = normalize_bitstreams(record["handle"], fresh)
    folder = item_directory(root, record)
    planned = []
    for bitstream in bitstreams:
        state = {
            "handle": record["handle"],
            "bitstream_id": str(bitstream.get("internal_id", "")),
            "name": safe_component(str(bitstream.get("name") or "file.bin")),
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
        if destination.exists() and destination.stat().st_size > 0:
            state.update(status="already_downloaded", path=str(destination), size=destination.stat().st_size)
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
            state.update(status="downloaded", path=str(destination), size=size, sha256=digest.hexdigest())
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    if execute:
        write_json_atomic(folder / "metadata.json", ia_metadata(record, "AzimPremjiUniversity"))
        write_json_atomic(folder / "source_record.json", record)
        write_json_atomic(folder / "manifest.json", {"files": planned, "updated_at": utc_now()})
    return planned


def download_records(
    db_path: Path, root: Path, workers: int, limit: int | None, execute: bool
) -> None:
    with closing(connect_db(db_path)) as db:
        sql = "SELECT * FROM items WHERE metadata_json IS NOT NULL ORDER BY handle"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        records = [row_to_record(row) for row in db.execute(sql, params).fetchall()]
        if not records:
            logging.info("No enriched records available")
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
        sql = "SELECT * FROM items WHERE metadata_json IS NOT NULL ORDER BY handle"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        for row in db.execute(sql, params).fetchall():
            record = row_to_record(row)
            identifier = "apu.sba." + record["handle"].split("/")[-1] + ".1"
            error = None
            if record["internet_archive_identifiers"] or record["internet_archive_urls"]:
                logging.info("%s: skip; source already references IA", record["handle"])
                status = "skipped_existing_ia_reference"
            elif ia_exists(identifier):
                logging.info("%s: skip; %s already exists", record["handle"], identifier)
                status = "already_on_ia"
            else:
                folder = item_directory(root, record)
                files = sorted(
                    path for path in folder.iterdir()
                    if path.is_file() and path.name not in {"metadata.json", "manifest.json", "source_record.json"}
                ) if folder.exists() else []
                if not files:
                    logging.info("%s: no downloaded files", record["handle"])
                    status = "files_missing"
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


def enrich(db_path: Path, workers: int, limit: int | None, refresh: bool) -> None:
    with closing(connect_db(db_path)) as db:
        where = "1=1" if refresh else "metadata_json IS NULL OR error IS NOT NULL"
        sql = f"SELECT * FROM items WHERE {where} ORDER BY handle"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = db.execute(sql, params).fetchall()
        if not rows:
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

    enr = sub.add_parser("enrich", help="Fetch metadata and bitstream records; resumable")
    enr.add_argument("--workers", type=int, default=3)
    enr.add_argument("--limit", type=int, help="Development/testing limit")
    enr.add_argument("--refresh", action="store_true", help="Refresh already enriched records")

    exp = sub.add_parser("export", help="Export normalized JSONL and optional CSV")
    exp.add_argument("--jsonl", type=Path, default=Path("sba_data/sba_records.jsonl"))
    exp.add_argument("--csv", type=Path, default=Path("sba_data/sba_records.csv"))

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
        inventory(args.db, args.page_size, args.max_items, args.delay)
    elif args.command == "enrich":
        enrich(args.db, args.workers, args.limit, args.refresh)
    elif args.command == "export":
        export_records(args.db, args.jsonl, args.csv)
    elif args.command == "ia-plan":
        make_ia_plan(args.db, args.output, args.collection)
    elif args.command == "download":
        download_records(args.db, args.root, args.workers, args.limit, args.execute)
    elif args.command == "ia-upload":
        upload_records(args.db, args.root, args.limit, args.execute, args.collection)


if __name__ == "__main__":
    main()
