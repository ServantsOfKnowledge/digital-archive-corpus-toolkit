#!/usr/bin/env python3
"""Resumable Anuvada Sampada -> Internet Archive synchronization pipeline.

Inventory and metadata harvesting are safe to run repeatedly. Downloads and IA
uploads are previews unless their command is given an explicit ``--execute``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://anuvadasampada.azimpremjiuniversity.edu.in"
OAI_URL = f"{BASE_URL}/cgi/oai2"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
USER_AGENT = (
    "ServantsOfKnowledge-Anuvada-Sampada-Harvester/1.0 "
    "(+https://github.com/ServantsOfKnowledge/digital-archive-corpus-toolkit)"
)
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
SOURCE_FILE_RE = re.compile(r"/([0-9]+)/([0-9]+)/([^/?#]+)")
IA_IDENTIFIER_RE = re.compile(r"^apu\.anuvadasampada\.(hin|kan)\.(\d+)(?:\.(\d+))?$")
LANGUAGE = {"hi": "hin", "kn": "kan", "hin": "hin", "kan": "kan"}
_local = threading.local()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        current.headers.update({"User-Agent": USER_AGENT})
        _local.session = current
    return current


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    record_id INTEGER PRIMARY KEY,
    oai_identifier TEXT NOT NULL,
    datestamp TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    dc_json TEXT,
    metadata_json TEXT,
    language TEXT,
    source_url TEXT NOT NULL,
    enriched_datestamp TEXT,
    error TEXT,
    listed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    record_id INTEGER NOT NULL,
    document_number INTEGER NOT NULL,
    filename TEXT NOT NULL,
    source_url TEXT NOT NULL,
    mime_type TEXT,
    expected_size INTEGER,
    source_md5 TEXT,
    ia_identifier TEXT,
    classification TEXT NOT NULL DEFAULT 'pending_reconcile',
    existing_ia_identifier TEXT,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    download_status TEXT,
    download_error TEXT,
    upload_status TEXT,
    upload_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(record_id, document_number),
    FOREIGN KEY(record_id) REFERENCES items(record_id)
);
CREATE INDEX IF NOT EXISTS documents_classification_idx ON documents(classification);
CREATE TABLE IF NOT EXISTS ia_existing (
    identifier TEXT PRIMARY KEY,
    source_url TEXT,
    source_file_url TEXT,
    source_record_id INTEGER,
    source_document_number INTEGER,
    refreshed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ia_existing_source_idx
    ON ia_existing(source_record_id, source_document_number);
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def get_state(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES(?,?)", (key, str(value)))


def source_parts(url: str) -> tuple[int | None, int | None]:
    match = SOURCE_FILE_RE.search(urlparse(unquote(url)).path)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def oai_request(params: dict[str, str]) -> tuple[ET.Element, str]:
    response = session().get(OAI_URL, params=params, timeout=120)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    error = root.find("oai:error", NS)
    if error is not None:
        raise RuntimeError(f"OAI {error.attrib.get('code', 'error')}: {error.text or ''}")
    response_date = root.findtext("oai:responseDate", default="", namespaces=NS)
    return root, response_date


def dc_values(dc: ET.Element, field: str) -> list[str]:
    return [node.text.strip() for node in dc.findall(f"dc:{field}", NS) if node.text and node.text.strip()]


def parse_oai_record(node: ET.Element) -> dict[str, Any]:
    header = node.find("oai:header", NS)
    if header is None:
        raise ValueError("OAI record has no header")
    identifier = header.findtext("oai:identifier", default="", namespaces=NS)
    record_id = int(identifier.rsplit(":", 1)[-1])
    deleted = header.attrib.get("status") == "deleted"
    result: dict[str, Any] = {
        "record_id": record_id,
        "oai_identifier": identifier,
        "datestamp": header.findtext("oai:datestamp", default="", namespaces=NS),
        "deleted": deleted,
        "source_url": f"{BASE_URL}/{record_id}/",
        "dc": {},
    }
    if deleted:
        return result
    dc = node.find("oai:metadata/*", NS)
    if dc is not None:
        for field in ("title", "creator", "subject", "description", "publisher", "date", "type", "format", "language", "identifier", "relation"):
            values = dc_values(dc, field)
            if values:
                result["dc"][field] = values
    return result


def inventory(db_path: Path, full: bool, restart: bool, max_pages: int | None, delay: float) -> None:
    with closing(connect_db(db_path)) as db:
        if full:
            set_state(db, "inventory_token", "")
            set_state(db, "inventory_run_from", "__FULL__")
        elif restart:
            set_state(db, "inventory_token", "")
        token = get_state(db, "inventory_token")
        run_from = get_state(db, "inventory_run_from")
        if not token and not run_from and not full:
            run_from = get_state(db, "last_inventory_response_date")
            set_state(db, "inventory_run_from", run_from)
        db.commit()
        pages = records_seen = 0
        while True:
            if token:
                params = {"verb": "ListRecords", "resumptionToken": token}
            else:
                params = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
                if run_from and run_from != "__FULL__":
                    params["from"] = run_from
            root, response_date = oai_request(params)
            records = root.findall(".//oai:ListRecords/oai:record", NS)
            now = utc_now()
            for node in records:
                record = parse_oai_record(node)
                previous = db.execute(
                    "SELECT datestamp FROM items WHERE record_id=?", (record["record_id"],)
                ).fetchone()
                changed = not previous or previous["datestamp"] != record["datestamp"]
                db.execute(
                    """INSERT INTO items
                       (record_id,oai_identifier,datestamp,deleted,dc_json,source_url,listed_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(record_id) DO UPDATE SET
                         oai_identifier=excluded.oai_identifier,
                         datestamp=excluded.datestamp, deleted=excluded.deleted,
                         dc_json=excluded.dc_json, source_url=excluded.source_url,
                         listed_at=excluded.listed_at""",
                    (
                        record["record_id"], record["oai_identifier"], record["datestamp"],
                        int(record["deleted"]), json.dumps(record["dc"], ensure_ascii=False),
                        record["source_url"], now,
                    ),
                )
                if changed:
                    db.execute(
                        "UPDATE items SET enriched_datestamp=NULL,error=NULL WHERE record_id=?",
                        (record["record_id"],),
                    )
                    db.execute(
                        "UPDATE documents SET classification='pending_reconcile' WHERE record_id=?",
                        (record["record_id"],),
                    )
                if record["deleted"]:
                    db.execute(
                        "UPDATE documents SET classification='source_deleted' WHERE record_id=?",
                        (record["record_id"],),
                    )
            token_node = root.find(".//oai:ListRecords/oai:resumptionToken", NS)
            token = (token_node.text or "").strip() if token_node is not None else ""
            set_state(db, "inventory_token", token)
            set_state(db, "inventory_run_response_date", response_date)
            db.commit()
            pages += 1
            records_seen += len(records)
            logging.info("Inventory: %s pages, %s records this run", pages, records_seen)
            if not token:
                set_state(db, "last_inventory_response_date", response_date)
                set_state(db, "inventory_run_from", "")
                set_state(db, "last_inventory_at", utc_now())
                db.commit()
                logging.info("Inventory synchronization complete")
                break
            if max_pages is not None and pages >= max_pages:
                logging.info("Inventory checkpointed with an OAI resumption token")
                break
            if delay:
                time.sleep(delay)


def json_export_url(record_id: int) -> str:
    return f"{BASE_URL}/cgi/export/eprint/{record_id}/JSON/aptranslations-eprint-{record_id}.js"


def fetch_enrichment(record_id: int) -> dict[str, Any]:
    response = session().get(json_export_url(record_id), timeout=120)
    response.raise_for_status()
    metadata = response.json()
    raw_language = ""
    languages = metadata.get("document_language") or []
    if languages and isinstance(languages[0], dict):
        raw_language = str(languages[0].get("type") or "")
    language = LANGUAGE.get(raw_language, "")
    documents = []
    for document in metadata.get("documents") or []:
        if document.get("security") not in (None, "public"):
            continue
        number = int(document.get("pos") or document.get("placement") or 1)
        main = str(document.get("main") or "")
        files = document.get("files") or []
        file_record = next((f for f in files if f.get("filename") == main), files[0] if files else {})
        filename = str(file_record.get("filename") or main)
        mime_type = str(file_record.get("mime_type") or document.get("mime_type") or "")
        if not filename or (mime_type != "application/pdf" and not filename.lower().endswith(".pdf")):
            continue
        source_url = f"{BASE_URL}/{record_id}/{number}/{quote(filename, safe='')}"
        documents.append({
            "document_number": number,
            "filename": filename,
            "source_url": source_url,
            "mime_type": mime_type or "application/pdf",
            "expected_size": int(file_record.get("filesize") or 0) or None,
            "source_md5": file_record.get("hash") if file_record.get("hash_type") == "MD5" else None,
        })
    return {"metadata": metadata, "language": language, "documents": documents}


def intended_identifier(language: str, record_id: int, document_number: int) -> str:
    if language == "hin":
        return f"apu.anuvadasampada.hin.{record_id}.{document_number}"
    if language == "kan":
        return f"apu.anuvadasampada.kan.{record_id}"
    return ""


def enrich(db_path: Path, workers: int, limit: int | None, refresh: bool) -> None:
    with closing(connect_db(db_path)) as db:
        where = "deleted=0 AND (enriched_datestamp IS NULL OR error IS NOT NULL"
        params: list[Any] = []
        if refresh:
            marker = get_state(db, "enrich_refresh_marker")
            if not marker:
                marker = utc_now()
                set_state(db, "enrich_refresh_marker", marker)
                db.commit()
            where += " OR enriched_datestamp < ?"
            params.append(marker)
        else:
            marker = get_state(db, "enrich_refresh_marker")
            if marker:
                where += " OR enriched_datestamp < ?"
                params.append(marker)
        where += ")"
        sql = f"SELECT record_id,datestamp FROM items WHERE {where} ORDER BY record_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = db.execute(sql, params).fetchall()
        if not rows:
            if marker:
                set_state(db, "enrich_refresh_marker", "")
                set_state(db, "last_enrichment_refresh_at", utc_now())
                db.commit()
            logging.info("No records require enrichment")
            return
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_enrichment, row["record_id"]): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                record_id = row["record_id"]
                try:
                    result = future.result()
                    language = result["language"]
                    db.execute(
                        """UPDATE items SET metadata_json=?,language=?,enriched_datestamp=?,error=NULL
                           WHERE record_id=?""",
                        (json.dumps(result["metadata"], ensure_ascii=False), language, row["datestamp"], record_id),
                    )
                    found_numbers = []
                    for document in result["documents"]:
                        number = document["document_number"]
                        found_numbers.append(number)
                        ia_id = intended_identifier(language, record_id, number)
                        classification = "pending_reconcile" if ia_id else "unsupported_language"
                        db.execute(
                            """INSERT INTO documents
                               (record_id,document_number,filename,source_url,mime_type,
                                expected_size,source_md5,ia_identifier,classification,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(record_id,document_number) DO UPDATE SET
                                 filename=excluded.filename,source_url=excluded.source_url,
                                 mime_type=excluded.mime_type,expected_size=excluded.expected_size,
                                 source_md5=excluded.source_md5,ia_identifier=excluded.ia_identifier,
                                 classification=CASE
                                   WHEN documents.source_url<>excluded.source_url
                                     OR documents.ia_identifier<>excluded.ia_identifier
                                   THEN excluded.classification ELSE documents.classification END,
                                 updated_at=excluded.updated_at""",
                            (
                                record_id, number, document["filename"], document["source_url"],
                                document["mime_type"], document["expected_size"], document["source_md5"],
                                ia_id, classification, utc_now(),
                            ),
                        )
                    if found_numbers:
                        placeholders = ",".join("?" for _ in found_numbers)
                        db.execute(
                            f"DELETE FROM documents WHERE record_id=? AND document_number NOT IN ({placeholders})",
                            (record_id, *found_numbers),
                        )
                    else:
                        db.execute("DELETE FROM documents WHERE record_id=?", (record_id,))
                except Exception as exc:
                    db.execute("UPDATE items SET error=? WHERE record_id=?", (str(exc), record_id))
                    logging.error("%s: %s", record_id, exc)
                done += 1
                if done % 25 == 0 or done == len(rows):
                    db.commit()
                    logging.info("Enrichment: %s/%s", done, len(rows))
        if marker:
            remaining = db.execute(
                "SELECT COUNT(*) FROM items WHERE deleted=0 AND (enriched_datestamp IS NULL OR enriched_datestamp < ? OR error IS NOT NULL)",
                (marker,),
            ).fetchone()[0]
            if remaining == 0:
                set_state(db, "enrich_refresh_marker", "")
                set_state(db, "last_enrichment_refresh_at", utc_now())
                db.commit()


def fetch_existing_ia(rows: int = 1000) -> list[dict[str, Any]]:
    page = 1
    found: list[dict[str, Any]] = []
    while True:
        response = session().get(
            IA_SEARCH_URL,
            params={
                "q": "collection:AzimPremjiUniversity AND identifier:apu.anuvadasampada.*",
                "fl[]": ["identifier", "source", "source-url"],
                "rows": rows,
                "page": page,
                "sort[]": "identifier asc",
                "output": "json",
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()["response"]
        found.extend(payload.get("docs") or [])
        logging.info("IA inventory: %s/%s", len(found), payload.get("numFound", len(found)))
        if len(found) >= int(payload.get("numFound") or 0) or not payload.get("docs"):
            return found
        page += 1


def reconcile_ia(db_path: Path) -> None:
    existing = fetch_existing_ia()
    refreshed_at = utc_now()
    with closing(connect_db(db_path)) as db:
        db.execute("DELETE FROM ia_existing")
        for item in existing:
            identifier = item.get("identifier", "")
            match = IA_IDENTIFIER_RE.match(identifier)
            record_id = int(match.group(2)) if match else None
            document_number = int(match.group(3)) if match and match.group(3) else None
            file_url = item.get("source-url") or ""
            url_record, url_document = source_parts(file_url)
            db.execute(
                """INSERT OR REPLACE INTO ia_existing
                   (identifier,source_url,source_file_url,source_record_id,
                    source_document_number,refreshed_at) VALUES(?,?,?,?,?,?)""",
                (
                    identifier, item.get("source") or "", file_url,
                    url_record or record_id, url_document or document_number, refreshed_at,
                ),
            )
        documents = db.execute("SELECT * FROM documents").fetchall()
        existing_ids = {row["identifier"] for row in db.execute("SELECT identifier FROM ia_existing")}
        by_url = {
            row["source_file_url"]: row["identifier"]
            for row in db.execute("SELECT identifier,source_file_url FROM ia_existing")
            if row["source_file_url"]
        }
        by_record: dict[int, list[str]] = {}
        for row in db.execute("SELECT identifier,source_record_id FROM ia_existing WHERE source_record_id IS NOT NULL"):
            by_record.setdefault(row["source_record_id"], []).append(row["identifier"])
        source_document_counts = {
            row["record_id"]: row["count"]
            for row in db.execute("SELECT record_id,COUNT(*) AS count FROM documents GROUP BY record_id")
        }
        for document in documents:
            matched = None
            if document["ia_identifier"] in existing_ids:
                matched = document["ia_identifier"]
            elif document["source_url"] in by_url:
                matched = by_url[document["source_url"]]
            elif source_document_counts.get(document["record_id"]) == 1 and document["record_id"] in by_record:
                # Old Kannada items omit the document number. A record-level
                # fallback is unambiguous only when the source has one PDF.
                matched = by_record[document["record_id"]][0]
            classification = "existing_ia" if matched else (
                "missing_ia" if document["ia_identifier"] else "unsupported_language"
            )
            db.execute(
                "UPDATE documents SET classification=?,existing_ia_identifier=?,updated_at=? WHERE record_id=? AND document_number=?",
                (classification, matched, utc_now(), document["record_id"], document["document_number"]),
            )
        set_state(db, "last_ia_reconcile_at", refreshed_at)
        set_state(db, "ia_existing_count", len(existing))
        db.commit()
    logging.info("Reconciled %s existing IA items", len(existing))


def safe_component(value: str) -> str:
    value = value.replace("/", "_").replace("\\", "_").replace("\x00", "")
    value = re.sub(r"[\r\n\t]+", " ", value).strip(" .") or "document.pdf"
    while len(value.encode("utf-8")) > 220:
        value = value[:-1]
    return value


def download_path(root: Path, row: sqlite3.Row) -> Path:
    return root / str(row["record_id"]) / f"{row['document_number']:03d}_{safe_component(row['filename'])}"


def existing_local_file(row: sqlite3.Row) -> bool:
    if not row["path"] or row["download_status"] not in ("downloaded", "already_downloaded"):
        return False
    path = Path(row["path"])
    return path.is_file() and bool(row["size"]) and path.stat().st_size == row["size"]


def download_one(root: Path, row: sqlite3.Row, execute: bool) -> dict[str, Any]:
    destination = download_path(root, row)
    result = {"record_id": row["record_id"], "document_number": row["document_number"], "path": str(destination.resolve())}
    if not execute:
        return {**result, "status": "planned"}
    if existing_local_file(row):
        return {**result, "status": "already_downloaded", "size": row["size"], "sha256": row["sha256"]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    partial_size = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={partial_size}-"} if partial_size else {}
    response = session().get(row["source_url"], headers=headers, stream=True, timeout=(30, 900))
    response.raise_for_status()
    resumed = partial_size > 0 and response.status_code == 206
    if resumed:
        with partial.open("rb") as existing:
            for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                digest.update(chunk)
                md5.update(chunk)
        size = partial_size
        mode = "ab"
    else:
        size = 0
        mode = "wb"
    try:
        with partial.open(mode) as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    digest.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
        if row["expected_size"] and size != row["expected_size"]:
            raise IOError(f"size mismatch: {size} != {row['expected_size']}")
        if row["source_md5"] and md5.hexdigest().lower() != row["source_md5"].lower():
            raise IOError("source MD5 mismatch")
        partial.replace(destination)
        return {**result, "status": "downloaded", "size": size, "sha256": digest.hexdigest()}
    except Exception:
        # Preserve an incomplete transfer for a future HTTP Range resume. A
        # completed-but-invalid transfer cannot be trusted and starts afresh.
        if row["expected_size"] and partial.exists() and partial.stat().st_size >= row["expected_size"]:
            partial.unlink(missing_ok=True)
        raise


def download(db_path: Path, root: Path, workers: int, limit: int | None, execute: bool) -> None:
    with closing(connect_db(db_path)) as db:
        rows = db.execute(
            """SELECT d.* FROM documents d JOIN items i USING(record_id)
               WHERE d.classification='missing_ia' AND i.deleted=0
               ORDER BY d.record_id,d.document_number"""
        ).fetchall()
        rows = [row for row in rows if not existing_local_file(row)]
        if limit is not None:
            rows = rows[:limit]
        logging.info("%s %s missing documents", "Downloading" if execute else "Would download", len(rows))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(download_one, root, row, execute): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                    if execute:
                        db.execute(
                            """UPDATE documents SET path=?,size=?,sha256=?,download_status=?,
                               download_error=NULL,updated_at=? WHERE record_id=? AND document_number=?""",
                            (
                                result["path"], result.get("size"), result.get("sha256"), result["status"],
                                utc_now(), row["record_id"], row["document_number"],
                            ),
                        )
                        db.commit()
                except Exception as exc:
                    db.execute(
                        "UPDATE documents SET download_status='failed',download_error=?,updated_at=? WHERE record_id=? AND document_number=?",
                        (str(exc), utc_now(), row["record_id"], row["document_number"]),
                    )
                    db.commit()
                    logging.error("%s/%s: %s", row["record_id"], row["document_number"], exc)


def creators(metadata: dict[str, Any]) -> list[str]:
    result = []
    for creator in metadata.get("creators") or []:
        name = creator.get("name") or {}
        text = ", ".join(x for x in (name.get("family"), name.get("given")) if x)
        if text:
            result.append(text)
    return result


def ia_metadata(
    item: sqlite3.Row, document: sqlite3.Row, collections: list[str]
) -> dict[str, Any]:
    metadata = json.loads(item["metadata_json"] or "{}")
    subject = list(metadata.get("subjects") or [])
    subject.extend(["Anuvada Sampada", "Azim Premji University"])
    result = {
        "collection": collections,
        "title": metadata.get("title"),
        "creator": creators(metadata),
        "date": metadata.get("date"),
        "description": metadata.get("abstract"),
        "publisher": metadata.get("publisher") or "Azim Premji University",
        "language": item["language"],
        "mediatype": "texts",
        "subject": list(dict.fromkeys(subject)),
        "type": metadata.get("type"),
        "source": item["source_url"],
        "source-url": document["source_url"],
        "licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/",
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def ia_item_exists(identifier: str) -> bool:
    response = session().get(f"https://archive.org/metadata/{quote(identifier)}", timeout=60)
    response.raise_for_status()
    return bool(response.json().get("metadata", {}).get("identifier"))


def metadata_arguments(metadata: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key, value in metadata.items():
        values = value if isinstance(value, list) else [value]
        for entry in values:
            result.append(f"--metadata={key}:{entry}")
    return result


def upload(
    db_path: Path, limit: int | None, collections: list[str], execute: bool
) -> None:
    with closing(connect_db(db_path)) as db:
        rows = db.execute(
            """SELECT d.*,i.metadata_json,i.language,i.source_url AS item_source_url,
                      i.deleted,i.error AS item_error
               FROM documents d JOIN items i USING(record_id)
               WHERE d.classification='missing_ia'
                 AND d.download_status IN ('downloaded','already_downloaded')
                 AND (d.upload_status IS NULL OR d.upload_status NOT IN ('uploaded','already_exists'))
                 AND i.deleted=0 ORDER BY d.record_id,d.document_number"""
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]
        logging.info("%s %s downloaded missing documents", "Uploading" if execute else "Would upload", len(rows))
        for row in rows:
            identifier = row["ia_identifier"]
            try:
                if ia_item_exists(identifier):
                    db.execute(
                        """UPDATE documents SET classification='existing_ia',existing_ia_identifier=?,
                           upload_status='already_exists',upload_error=NULL,updated_at=?
                           WHERE record_id=? AND document_number=?""",
                        (identifier, utc_now(), row["record_id"], row["document_number"]),
                    )
                    db.commit()
                    logging.info("Already exists: %s", identifier)
                    continue
                item = db.execute("SELECT * FROM items WHERE record_id=?", (row["record_id"],)).fetchone()
                command = [
                    "ia", "upload", identifier, row["path"],
                    *metadata_arguments(ia_metadata(item, row, collections)),
                    "--checksum", "--verify",
                ]
                if not execute:
                    logging.info("Would upload %s from %s", identifier, row["path"])
                    continue
                result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
                if result.returncode:
                    raise RuntimeError((result.stderr or result.stdout).strip())
                db.execute(
                    """UPDATE documents SET classification='existing_ia',existing_ia_identifier=?,
                       upload_status='uploaded',upload_error=NULL,updated_at=?
                       WHERE record_id=? AND document_number=?""",
                    (identifier, utc_now(), row["record_id"], row["document_number"]),
                )
                db.commit()
                logging.info("Uploaded %s", identifier)
            except Exception as exc:
                db.execute(
                    "UPDATE documents SET upload_status='failed',upload_error=?,updated_at=? WHERE record_id=? AND document_number=?",
                    (str(exc), utc_now(), row["record_id"], row["document_number"]),
                )
                db.commit()
                logging.error("%s: %s", identifier, exc)


def collect_status(db_path: Path) -> dict[str, Any]:
    with closing(connect_db(db_path)) as db:
        scalar = lambda sql: db.execute(sql).fetchone()[0]
        stats: dict[str, Any] = {
            "inventory_records": scalar("SELECT COUNT(*) FROM items"),
            "deleted_source_records": scalar("SELECT COUNT(*) FROM items WHERE deleted=1"),
            "enriched_records": scalar("SELECT COUNT(*) FROM items WHERE enriched_datestamp IS NOT NULL"),
            "enrichment_errors": scalar("SELECT COUNT(*) FROM items WHERE error IS NOT NULL"),
            "pdf_documents": scalar("SELECT COUNT(*) FROM documents"),
            "existing_on_ia": scalar("SELECT COUNT(*) FROM documents WHERE classification='existing_ia'"),
            "missing_on_ia": scalar("SELECT COUNT(*) FROM documents WHERE classification='missing_ia'"),
            "pending_ia_reconcile": scalar("SELECT COUNT(*) FROM documents WHERE classification='pending_reconcile'"),
            "unsupported_language": scalar("SELECT COUNT(*) FROM documents WHERE classification='unsupported_language'"),
            "downloaded_missing": scalar("SELECT COUNT(*) FROM documents WHERE classification='missing_ia' AND download_status IN ('downloaded','already_downloaded')"),
            "download_failures": scalar("SELECT COUNT(*) FROM documents WHERE download_status='failed'"),
            "uploaded_this_run": scalar("SELECT COUNT(*) FROM documents WHERE upload_status='uploaded'"),
            "upload_failures": scalar("SELECT COUNT(*) FROM documents WHERE upload_status='failed'"),
            "ia_collection_snapshot": int(get_state(db, "ia_existing_count", "0") or 0),
            "last_ia_reconcile_at": get_state(db, "last_ia_reconcile_at"),
            "inventory_resumable": bool(get_state(db, "inventory_token")),
            "enrichment_refresh_active": bool(get_state(db, "enrich_refresh_marker")),
        }
    return stats


def print_status(db_path: Path, as_json: bool) -> None:
    stats = collect_status(db_path)
    if as_json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in stats.items():
        print(f"{key.replace('_', ' ').title():<32} {value:,}" if isinstance(value, int) else f"{key.replace('_', ' ').title():<32} {value}")


def export(db_path: Path, output: Path, csv_path: Path | None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    exported = []
    with closing(connect_db(db_path)) as db, temporary.open("w", encoding="utf-8") as stream:
        rows = db.execute(
            """SELECT i.record_id,d.document_number,d.filename,d.source_url,d.mime_type,
                      d.expected_size,d.source_md5,d.ia_identifier,d.classification,
                      d.existing_ia_identifier,d.path,d.size,d.sha256,d.download_status,
                      d.download_error,d.upload_status,d.upload_error,d.updated_at,
                      i.oai_identifier,i.datestamp,i.deleted,i.dc_json,i.metadata_json,
                      i.language,i.source_url AS record_url,i.error AS enrichment_error
               FROM items i LEFT JOIN documents d USING(record_id)
               ORDER BY i.record_id,d.document_number"""
        ).fetchall()
        for row in rows:
            record = dict(row)
            record["dc"] = json.loads(record.pop("dc_json") or "{}")
            record["metadata"] = json.loads(record.pop("metadata_json") or "{}")
            exported.append(record)
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(output)
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "record_id", "document_number", "filename", "source_url", "language",
            "ia_identifier", "classification", "existing_ia_identifier", "download_status",
            "upload_status", "record_url", "datestamp", "enrichment_error",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(exported)
    logging.info("Exported %s document records", len(exported))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("anuvada_data/anuvada.sqlite3"))
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    inv = commands.add_parser("inventory", help="Resume an OAI inventory/change harvest")
    inv.add_argument("--full", action="store_true", help="Start a new full harvest")
    inv.add_argument("--restart", action="store_true", help="Restart the current incremental pass")
    inv.add_argument("--max-pages", type=int, help="Stop after N pages, preserving the resume token")
    inv.add_argument("--delay", type=float, default=0.25)

    enr = commands.add_parser("enrich", help="Resume authoritative JSON metadata and file harvesting")
    enr.add_argument("--workers", type=int, default=4)
    enr.add_argument("--limit", type=int)
    enr.add_argument("--refresh", action="store_true", help="Start/resume a full metadata refresh")

    commands.add_parser("reconcile-ia", help="Refresh IA inventory and mark only missing documents")
    status = commands.add_parser("status", help="Show source, IA, download, and upload counts")
    status.add_argument("--json", action="store_true")

    dl = commands.add_parser("download", help="Download only IA-missing PDFs; preview by default")
    dl.add_argument("--root", type=Path, default=Path("anuvada_corpus"))
    dl.add_argument("--workers", type=int, default=2)
    dl.add_argument("--limit", type=int)
    dl.add_argument("--execute", action="store_true")

    up = commands.add_parser("ia-upload", help="Upload downloaded missing PDFs; preview by default")
    up.add_argument(
        "--collection",
        action="append",
        dest="collections",
        help=(
            "IA collection; repeat for more than one. Defaults to "
            "AzimPremjiUniversity and ServantsOfKnowledge"
        ),
    )
    up.add_argument("--limit", type=int)
    up.add_argument("--execute", action="store_true")

    exp = commands.add_parser("export", help="Export the document inventory and IA decisions")
    exp.add_argument("--jsonl", type=Path, default=Path("anuvada_data/anuvada_records.jsonl"))
    exp.add_argument("--csv", type=Path, default=Path("anuvada_data/anuvada_records.csv"))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.command == "inventory":
        inventory(args.db, args.full, args.restart, args.max_pages, args.delay)
    elif args.command == "enrich":
        enrich(args.db, args.workers, args.limit, args.refresh)
    elif args.command == "reconcile-ia":
        reconcile_ia(args.db)
    elif args.command == "status":
        print_status(args.db, args.json)
    elif args.command == "download":
        download(args.db, args.root, args.workers, args.limit, args.execute)
    elif args.command == "ia-upload":
        collections = args.collections or ["AzimPremjiUniversity", "ServantsOfKnowledge"]
        upload(args.db, args.limit, collections, args.execute)
    elif args.command == "export":
        export(args.db, args.jsonl, args.csv)


if __name__ == "__main__":
    main()
