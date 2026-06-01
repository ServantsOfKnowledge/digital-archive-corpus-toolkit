#!/usr/bin/env python3
"""
Detects PDF errors from upload logs and handles redownloading + retrying.

Usage:
  python3 tdl_pdf_recovery.py [--log-file logs/periodicals_upload.log] [--retry]

The script:
1. Parses upload logs for "Uploaded content is unacceptable. - error checking pdf file"
2. Creates a redownload list with category and article IDs
3. Redownloads the items from source
4. Retries uploading with --retry-failed flag
"""

import json
import re
import subprocess
from pathlib import Path
from datetime import datetime
import shutil

CORPUS = Path("tdl_corpus")
REDOWNLOAD_FILE = Path("pdf_redownload.json")
RECOVERY_LOG = Path("logs/pdf_recovery.log")

def log_message(msg: str):
    """Log to both console and recovery log"""
    print(msg)
    RECOVERY_LOG.parent.mkdir(exist_ok=True)
    with open(RECOVERY_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {msg}\n")

def detect_pdf_errors(log_file: str) -> dict:
    """Parse upload log for PDF errors and extract item identifiers"""
    errors = {"Book": [], "Periodical": [], "Document": []}

    if not Path(log_file).exists():
        log_message(f"ERROR: Log file not found: {log_file}")
        return errors

    content = Path(log_file).read_text()

    # The identifier appears before this in RuntimeError output
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "error checking pdf file" in line:
            # Look backwards for the identifier
            for j in range(i, max(0, i-20), -1):
                match = re.search(r"tdl\.(\d+|[a-zA-Z0-9\-]+)", lines[j])
                if match:
                    identifier = match.group(1)
                    # Determine category from context
                    for cat in ["Book", "Periodical", "Document"]:
                        if f" {cat}:" in content[max(0, content.find(lines[j])-200):content.find(lines[j])+100]:
                            errors[cat].append(identifier)
                            break
                    else:
                        # Default to Periodical if uploading periodicals
                        if "periodicals_upload.log" in log_file or "Periodical" in log_file:
                            errors["Periodical"].append(identifier)
                        else:
                            errors["Book"].append(identifier)
                    break

    return errors

def find_folder_by_id(category: str, article_id: str) -> Path:
    """Find the folder for a given article ID"""
    cat_path = CORPUS / category
    if not cat_path.exists():
        return None

    # Article ID is the numeric prefix in folder name
    for folder in cat_path.iterdir():
        if not folder.is_dir():
            continue
        # Folder name format: {article_id}_{title}
        if folder.name.startswith(f"{article_id}_"):
            return folder
    return None

def mark_for_redownload(errors: dict):
    """Create redownload list and remove corrupted PDFs"""
    to_redownload = []
    removed_count = 0

    for category, article_ids in errors.items():
        for article_id in article_ids:
            folder = find_folder_by_id(category, article_id)
            if not folder:
                log_message(f"  WARN: Folder not found for {category}/{article_id}")
                continue

            # Remove corrupted PDFs
            pdfs = list(folder.glob("*.pdf"))
            if pdfs:
                for pdf in pdfs:
                    pdf.unlink()
                    removed_count += 1
                    log_message(f"  OK: Removed corrupted PDF: {pdf.name}")

            # Add to redownload list
            to_redownload.append({
                "category": category,
                "article_id": article_id,
                "folder": folder.name,
                "detected_time": datetime.now().isoformat(),
                "reason": "PDF error during upload - error checking pdf file"
            })

    # Save redownload list
    if to_redownload:
        with open(REDOWNLOAD_FILE, "w") as f:
            json.dump(to_redownload, f, indent=2)
        log_message(f"\nOK: Marked {len(to_redownload)} items for redownload")
        log_message(f"OK: Removed {removed_count} corrupted PDFs")

    return to_redownload

def construct_article_urls(items: list) -> list:
    """Construct TDL article URLs from item information"""
    # URL pattern: https://www.tamildigitallibrary.in/Articles/{article_id}
    # or with title slug: https://www.tamildigitallibrary.in/Articles/{slug}-{article_id}
    urls = []
    for item in items:
        article_id = item["article_id"]
        # Try simple format first (will be redirected if needed)
        url = f"https://www.tamildigitallibrary.in/Articles/{article_id}"
        urls.append(url)
    return urls

def redownload_items():
    """Redownload marked items using tdl_downloader"""
    if not REDOWNLOAD_FILE.exists():
        log_message("No items marked for redownload")
        return False

    items = json.loads(REDOWNLOAD_FILE.read_text())
    if not items:
        log_message("Redownload list is empty")
        return False

    # Build URL list for downloader
    urls = construct_article_urls(items)

    log_message(f"\n=== Starting redownload of {len(items)} items ===\n")

    # Save URLs to temporary file
    url_file = Path(".temp_redownload_urls.txt")
    with open(url_file, "w") as f:
        f.write("\n".join(urls))

    # Run downloader with specific URLs and output to special directory
    # Use --skip-existing to avoid re-processing items already in tdl_corpus
    cmd = [
        "python3", "tdl_downloader.py", "download",
        "--urls", str(url_file),
        "--dir", "tdl_redownload_temp",
        "--workers", "3",
        "--skip-existing", "tdl_corpus",
        "--cat-name", "recovered"
    ]

    log_message(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Clean up temp file
    url_file.unlink()

    if result.returncode == 0:
        log_message(f"\nOK: Redownload completed successfully")

        # Move redownloaded items back to corpus
        log_message(f"\nMoving recovered items back to corpus...")
        recovered_dir = Path("tdl_redownload_temp/recovered")
        if recovered_dir.exists():
            for item in items:
                src = find_recovered_folder(recovered_dir, item["article_id"])
                dst = CORPUS / item["category"] / item["folder"]
                if src.exists():
                    # Remove old corrupted folder
                    if dst.exists():
                        shutil.rmtree(dst)
                    # Move new one
                    shutil.move(str(src), str(dst))
                    log_message(f"  OK: Moved {item['folder']}")
                else:
                    log_message(f"  WARN: Recovered folder not found for {item['article_id']}")

        return True
    else:
        log_message(f"\nERROR: Redownload failed:")
        log_message(f"  stdout: {result.stdout[:500]}")
        log_message(f"  stderr: {result.stderr[:500]}")
        return False

def find_recovered_folder(recovered_dir: Path, article_id: str) -> Path:
    """Find a redownloaded folder by article ID."""
    for folder in recovered_dir.iterdir():
        if folder.is_dir() and folder.name.startswith(f"{article_id}_"):
            return folder
    return recovered_dir / article_id

def retry_failed_uploads(log_file: str):
    """Retry uploads using the uploader's --retry-failed flag"""
    log_message(f"\n=== Retrying failed uploads ===\n")
    if "periodical" in log_file.lower():
        cat = "Periodical"
    elif "book" in log_file.lower():
        cat = "Book"
    else:
        log_message(f"WARN: Could not infer category from {log_file}; skipping retry")
        return

    log_message(f"Retrying {cat} uploads...")
    cmd = ["python3", "tdl_upload.py", "--cat", cat, "--retry-failed",
           "--workers", "3", "--no-collection-check"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log_message(f"OK: {cat} retry completed")
    else:
        log_message(f"WARN: {cat} retry had issues: {result.stderr[:200]}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Detect and recover from PDF upload errors")
    parser.add_argument("--log-file", default="logs/periodicals_upload.log",
                       help="Upload log file to scan for errors")
    parser.add_argument("--retry", action="store_true",
                       help="Also perform redownload and retry (automatic mode)")
    args = parser.parse_args()

    log_message(f"\n{'='*60}")
    log_message(f"PDF Recovery Scanner")
    log_message(f"{'='*60}\n")

    # Step 1: Detect errors
    log_message(f"Scanning log for PDF errors: {args.log_file}")
    errors = detect_pdf_errors(args.log_file)

    total_errors = sum(len(v) for v in errors.values())
    if total_errors == 0:
        log_message("OK: No PDF errors detected")
        return

    log_message(f"\nERROR: Found {total_errors} PDF errors:")
    for cat, ids in errors.items():
        if ids:
            log_message(f"  {cat}: {len(ids)} errors")

    # Step 2: Mark for redownload
    log_message(f"\nMarking items for redownload...")
    to_redownload = mark_for_redownload(errors)

    if not args.retry:
        log_message(f"\nTo redownload and retry, run:")
        log_message(f"  python3 tdl_pdf_recovery.py --log-file {args.log_file} --retry")
        return

    # Step 3: Redownload
    if not redownload_items():
        log_message("\nERROR: Redownload failed, aborting retry")
        return

    # Step 4: Retry uploads
    retry_failed_uploads(args.log_file)

    log_message(f"\n{'='*60}")
    log_message(f"PDF Recovery Complete")
    log_message(f"{'='*60}\n")

if __name__ == "__main__":
    main()
