#!/usr/bin/env python3
"""
Local web dashboard for TDL corpus operations.
Shows status of all upload/download/unzip tasks with start/stop/restart controls.

Usage:
  python3 tdl_dashboard.py [--port 8080]
"""

import json, os, subprocess, sys, time, signal, re, shutil
from pathlib import Path
from threading import Thread
from datetime import datetime
from typing import Optional

import flask

HERE = Path(__file__).parent
CORPUS = HERE / "tdl_corpus"
EXTERNAL = Path("/Volumes/BMShri Back/tdl_corpus")
LOGS_DIR = HERE / "logs"

# ── Category definitions: (sort_id, name, total_expected, source) ──
# source: "local" = CORPUS, "external" = EXTERNAL
CATEGORIES_TABLE = [
    (2, "Video", 10, "local"),
    (4, "Photograph", 53, "local"),
    (7, "Excavation", 55, "local"),
    (14, "Painting", 65, "local"),
    (13, "HistoricalMonument", 124, "local"),
    (1, "Audio", 125, "local"),
    (6, "PreHistoric", 166, "local"),
    (12, "CopperPlate", 192, "local"),
    (5, "AuthorBio", 196, "local"),
    (9, "ReligiousPlace", 370, "local"),
    (11, "Coin", 673, "local"),
    (10, "Sculpture", 1645, "local"),
    (3, "Map", 1862, "local"),
    (8, "Inscription", 1922, "local"),
    (27, "Document", 4769, "local"),
    (22, "Palmleaf", 5387, "local"),
    (21, "Periodical", 29951, "external"),
    (20, "Book", 42043, "local"),
]

def count_items(cat_dir: Path) -> int:
    if not cat_dir.exists():
        return 0
    return sum(1 for p in cat_dir.iterdir() if p.is_dir())

def dir_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
    return total / (1024**3)

def disk_free_str(path: Path) -> str:
    usage = shutil.disk_usage(path)
    gb = usage.free / (1024**3)
    return f"{gb:.0f}G" if gb > 100 else f"{gb:.1f}G"

# ── Process definitions ──────────────────────────────────────────────
PROCESSES = {
    "download_book": {
        "label": "Book Download",
        "workers": 3,
        "categories": ["Book"],
        "cmd": [
            "python3", str(HERE / "tdl_downloader.py"),
            "fetch", "--cat-id", "20",
            "--dir", str(HERE / "tdl_corpus"),
            "--workers", "3", "--resume",
        ],
        "log": LOGS_DIR / "books.log",
        "match": r"tdl_downloader.*cat-id 20",
    },
    "download_periodical": {
        "label": "Periodical Download",
        "workers": 3,
        "categories": ["Periodical"],
        "cmd": [
            "python3", str(HERE / "tdl_downloader.py"),
            "fetch", "--cat-id", "21",
            "--dir", str(HERE / "tdl_corpus"),
            "--workers", "3", "--resume",
            "--skip-existing", "/Volumes/BMShri Back/tdl_corpus",
        ],
        "log": LOGS_DIR / "periodical.log",
        "match": r"tdl_downloader.*cat-id 21",
    },
    "upload_book": {
        "label": "Book Upload",
        "workers": 3,
        "categories": ["Book"],
        "cmd": [
            "python3", "-u", str(HERE / "tdl_upload.py"),
            "--cat", "Book", "--workers", "3", "--no-collection-check",
        ],
        "log": LOGS_DIR / "books_upload.log",
        "match": r"tdl_upload.*--cat Book",
    },
    "upload_bigcats": {
        "label": "Big Cats Upload",
        "workers": 5,
        "categories": ["Sculpture", "Palmleaf", "Map", "Inscription"],
        "cmd": [
            "python3", "-u", str(HERE / "tdl_upload.py"),
            "--cat", "Sculpture", "Palmleaf", "Map", "Inscription",
            "--workers", "5", "--no-collection-check",
        ],
        "log": LOGS_DIR / "big_cats_upload.log",
        "match": r"tdl_upload.*Sculpture.*Palmleaf",
    },
    "upload_periodical_ext": {
        "label": "Periodical Upload (External)",
        "workers": 2,
        "categories": ["Periodical"],
        "cmd": [
            "python3", "-u", str(HERE / "tdl_upload.py"),
            "--cat", "Periodical",
            "--corpus", "/Volumes/BMShri Back/tdl_corpus",
            "--workers", "2", "--no-collection-check",
        ],
        "log": LOGS_DIR / "periodical_upload.log",
        "match": r"tdl_upload.*Periodical.*BMShri",
    },
    "unzip_push": {
        "label": "Unzip + Push",
        "workers": 2,
        "categories": ["All (uploaded items)"],
        "cmd": [
            "python3", "-u", str(HERE / "tdl_unzip_and_push.py"),
            "--workers", "2",
        ],
        "retry_cmd": [
            "python3", "-u", str(HERE / "tdl_unzip_and_push.py"),
            "--retry", "--workers", "2",
        ],
        "log": LOGS_DIR / "unzip_push.log",
        "match": r"tdl_unzip_and_push",
    },
}

PROGRESS_FILE = HERE / "upload_progress.json"
UNZIP_DONE_FILE = HERE / "unzip_done.json"
UNZIP_FAILED_FILE = HERE / "unzip_failed.json"
DOWNLOAD_PROGRESS = HERE / "tdl_corpus" / "progress.json"

# ── Helpers ──────────────────────────────────────────────────────────

def find_pid(match_pattern: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.splitlines():
            if re.search(match_pattern, line) and "grep" not in line:
                # ps aux: USER PID %CPU ...  PID is column 1
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    return int(parts[1])
    except Exception:
        pass
    return None

def get_uptime(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return "?"

def tail_log(path: Path, n: int = 5) -> list:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
        return [l.rstrip("\n\r") for l in lines[-n:] if l.strip()]
    except Exception:
        return []

def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

# ── Flask app ────────────────────────────────────────────────────────

app = flask.Flask(__name__)

@app.route("/")
def index():
    return flask.render_template_string(HTML)

@app.route("/api/status")
def api_status():
    statuses = {}
    for key, proc in PROCESSES.items():
        pid = find_pid(proc["match"])
        statuses[key] = {
            "label": proc["label"],
            "pid": pid,
            "running": pid is not None,
            "uptime": get_uptime(pid) if pid else None,
            "workers": proc.get("workers", "?"),
            "categories": proc.get("categories", []),
            "has_retry": "retry_cmd" in proc,
            "log": tail_log(proc["log"], 6),
        }

    up = read_json(PROGRESS_FILE)
    ud = read_json(UNZIP_DONE_FILE)
    uf = read_json(UNZIP_FAILED_FILE)
    dp = read_json(DOWNLOAD_PROGRESS)

    uniq_failed = len(set(f.get("identifier", "") for f in uf)) if isinstance(uf, list) else 0

    # ── Category table (like tdl_status.py) ──
    uploaded_by_cat = up.get("uploaded_by_cat", {})
    cat_rows = []
    total_dl = 0
    total_all = 0
    total_ul = 0
    for cid, name, total, source in CATEGORIES_TABLE:
        base = EXTERNAL / name if source == "external" else CORPUS / name
        dl = count_items(base)
        sz = dir_size_gb(base)
        ul = uploaded_by_cat.get(name, 0)
        rem = ul - dl if ul >= dl else 0
        pct = round(dl / total * 100) if total else 0
        cat_rows.append({
            "name": name, "dl": dl, "total": total,
            "pct": pct, "size_gb": round(sz, 1),
            "ul": ul, "rem": rem, "source": source,
        })
        total_dl += dl
        total_all += total
        total_ul += ul

    # remaining uploaded items not assigned to any known category
    assigned_cats = {c[1] for c in CATEGORIES_TABLE}
    remaining_ul = sum(v for k, v in uploaded_by_cat.items() if k not in assigned_cats)
    total_ul += remaining_ul

    corpus_free = disk_free_str(CORPUS)
    ext_free = disk_free_str(EXTERNAL) if EXTERNAL.exists() else "-"

    return {
        "processes": statuses,
        "upload": {
            "total": len(up.get("uploaded", [])),
            "by_cat": uploaded_by_cat,
            "failed": len(up.get("failed", [])),
        },
        "download": {
            "completed": len(dp.get("completed", [])),
            "pending": len(dp.get("pending", [])),
            "failed": len(dp.get("failed", [])),
        },
        "unzip": {
            "done": len(ud) if isinstance(ud, list) else 0,
            "failed": uniq_failed,
        },
        "upload_progress": up,
        "download_progress": dp,
        "categories_table": {
            "rows": cat_rows,
            "totals": {
                "dl": total_dl, "all": total_all,
                "ul": total_ul, "rem": sum(r["rem"] for r in cat_rows),
            },
            "free": {"corpus": corpus_free, "external": ext_free},
            "remaining_ul": remaining_ul,
        },
        "ts": datetime.now().isoformat(),
    }

@app.route("/api/action", methods=["POST"])
def api_action():
    data = flask.request.get_json()
    key = data.get("process")
    action = data.get("action")

    if key not in PROCESSES:
        return {"ok": False, "error": "unknown process"}, 400

    proc = PROCESSES[key]
    pid = find_pid(proc["match"])

    if action == "stop":
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                return {"ok": True, "msg": f"Sent SIGTERM to PID {pid}"}
            except ProcessLookupError:
                return {"ok": True, "msg": "Process already gone"}
        return {"ok": False, "msg": "Not running"}

    elif action == "start":
        if pid:
            return {"ok": False, "msg": f"Already running (PID {pid})"}
        log_path = proc["log"]
        cmd_str = " ".join(proc["cmd"])
        full_cmd = f"nohup {cmd_str} >> {log_path} 2>&1 &\n"
        subprocess.run(full_cmd, shell=True, capture_output=True, timeout=10)
        time.sleep(2)
        new_pid = find_pid(proc["match"])
        return {"ok": True, "msg": f"Started PID {new_pid}" if new_pid else "Started (check log)"}

    elif action == "retry":
        if pid:
            return {"ok": False, "msg": f"Already running (PID {pid}) — stop first"}
        retry_cmd = proc.get("retry_cmd")
        if not retry_cmd:
            return {"ok": False, "msg": "No retry command defined"}
        log_path = proc["log"]
        with open(log_path, "a") as f:
            f.write(f"\n--- retry-failed {datetime.now().isoformat()} ---\n")
        cmd_str = " ".join(retry_cmd)
        full_cmd = f"nohup {cmd_str} >> {log_path} 2>&1 &\n"
        subprocess.run(full_cmd, shell=True, capture_output=True, timeout=10)
        time.sleep(2)
        new_pid = find_pid(proc["match"])
        return {"ok": True, "msg": f"Retry started PID {new_pid}" if new_pid else "Retry started (check log)"}

    elif action == "restart":
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
            except ProcessLookupError:
                pass
        log_path = proc["log"]
        with open(log_path, "a") as f:
            f.write(f"\n--- restart {datetime.now().isoformat()} ---\n")
        cmd_str = " ".join(proc["cmd"])
        full_cmd = f"nohup {cmd_str} >> {log_path} 2>&1 &\n"
        subprocess.run(full_cmd, shell=True, capture_output=True, timeout=10)
        time.sleep(2)
        new_pid = find_pid(proc["match"])
        return {"ok": True, "msg": f"Restarted PID {new_pid}" if new_pid else "Restarted (check log)"}

    return {"ok": False, "error": "unknown action"}, 400


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TDL Corpus Dashboard — Servants Of Knowledge</title>
<link href="https://fonts.googleapis.com/css?family=Open+Sans+Condensed:700|Open+Sans:400,600" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Open Sans', -apple-system, sans-serif; background: #0a0e14; color: #e6edf3; padding: 0; }

/* ── Header ── */
.header { display: flex; align-items: center; gap: 14px; padding: 12px 24px; background: #161b22; border-bottom: 1px solid #30363d; flex-wrap: wrap; }
.header .sok-logo { height: 38px; width: 38px; border-radius: 50%; flex-shrink: 0; }
.header .sok-name { font-family: 'Open Sans Condensed', sans-serif; font-size: 1.2em; color: #3fb950; font-weight: 700; letter-spacing: 0.5px; }
.header .title { margin-left: auto; color: #8b949e; font-size: 0.82em; }
.header .title strong { color: #e6edf3; }

/* ── Summary Bar ── */
.summary { display: flex; gap: 1px; background: #30363d; margin: 0; flex-wrap: wrap; }
.summary .stat { flex: 1; min-width: 100px; background: #161b22; padding: 12px 8px; text-align: center; cursor: default; transition: background 0.15s; }
.summary .stat:hover { background: #1c2333; }
.summary .stat .num { font-size: 1.6em; font-weight: 700; line-height: 1.2; }
.summary .stat .lbl { font-size: 0.68em; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 2px; }
.summary .stat .sub { font-size: 0.6em; color: #484f58; margin-top: 1px; }

/* ── Section ── */
.section { padding: 14px 20px 6px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.section h2 { font-size: 0.85em; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
.section .tag { font-size: 0.7em; background: #21262d; color: #8b949e; padding: 2px 8px; border-radius: 8px; }

/* ── Process Grid ── */
.grid { display: flex; flex-direction: column; gap: 6px; padding: 0 20px 16px; }

.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; transition: border-color 0.2s; }
.card.running { border-color: #238636; }
.card.stopped { border-color: #30363d; }

.card-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid #21262d; }
.card-header .status-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.card-header .status-dot.running { background: #3fb950; box-shadow: 0 0 6px #3fb95088; }
.card-header .status-dot.stopped { background: #da3633; }
.card-header h3 { font-size: 0.85em; font-weight: 600; flex: 1; }
.card-header .pid { font-size: 0.68em; color: #8b949e; font-family: 'SF Mono', Menlo, monospace; white-space: nowrap; }

.card-body { padding: 8px 12px; }
.card-body .meta-row { display: flex; gap: 6px; align-items: center; margin-bottom: 4px; font-size: 0.78em; flex-wrap: wrap; }
.card-body .meta-row .label { color: #8b949e; min-width: 50px; }
.card-body .meta-row .value { color: #e6edf3; }
.card-body .meta-row .value .cat-inline { color: #8b949e; font-size: 0.9em; }

.ts { padding: 4px 20px 14px; color: #484f58; font-size: 0.68em; display: flex; gap: 16px; flex-wrap: wrap; }

/* ── Category Table ── */
.table-wrap { padding: 0 20px 16px; overflow-x: auto; }
.table-wrap table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
.table-wrap th { text-align: left; padding: 8px 6px; border-bottom: 2px solid #30363d; color: #8b949e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; cursor: pointer; user-select: none; }
.table-wrap th:hover { color: #e6edf3; }
.table-wrap th.sorted { color: #58a6ff; }
.table-wrap td { padding: 5px 6px; border-bottom: 1px solid #21262d; white-space: nowrap; }
.table-wrap tr:hover td { background: #1c2333; }
.table-wrap .num { font-family: 'SF Mono', Menlo, monospace; text-align: right; }
.table-wrap .pct-bar { display: inline-block; width: 50px; height: 5px; background: #21262d; border-radius: 3px; vertical-align: middle; margin-right: 6px; overflow: hidden; }
.table-wrap .pct-bar .fill { height: 100%; border-radius: 3px; transition: width 0.8s ease; }
.table-wrap .src-tag { display: inline-block; background: #1c2333; border: 1px solid #30363d; border-radius: 3px; padding: 0 5px; font-size: 0.85em; }
.table-wrap .src-tag.local { color: #58a6ff; border-color: #1c3a5c; }
.table-wrap .src-tag.external { color: #d29922; border-color: #4d3c14; }
.table-wrap .totals-row td { font-weight: 700; border-top: 2px solid #30363d; border-bottom: none; padding-top: 8px; }
.table-wrap .totals-row .free { color: #3fb950; }

.collapse-toggle { cursor: pointer; user-select: none; font-size: 0.72em; color: #58a6ff; background: none; border: 1px solid #30363d; border-radius: 4px; padding: 3px 10px; margin-left: 12px; transition: all 0.15s; }
.collapse-toggle:hover { background: #21262d; }

@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
</style>
</head>
<body>

<div class="header">
  <img src="https://ia801906.us.archive.org/0/items/ServantsOfKnowledge/ServantsOfKnowledge_itemimage.jpg" alt="SoK" class="sok-logo">
  <span class="sok-name">Servants Of Knowledge</span>
  <span class="title"><strong>TDL Corpus</strong> — Upload &amp; Download Dashboard</span>
</div>

<div class="summary" id="summary"></div>

<div class="section">
  <h2>📊 Categories</h2>
  <button class="collapse-toggle" id="catToggle" onclick="toggleCats()">Show</button>
</div>
<div class="table-wrap" id="catTableWrap" style="display:none">
  <table id="catTable">
    <thead><tr id="catHead"></tr></thead>
    <tbody id="catBody"></tbody>
  </table>
</div>

<div class="section">
  <h2>📦 Processes</h2>
  <span class="tag" id="runningCount"></span>
  <span class="tag" id="workersTotal"></span>
</div>
<div class="grid" id="processes"></div>

<div class="ts" id="ts"></div>

<script>
const POLL_MS = 4000;
let statusData = null;
let prevUploaded = 0;
let catSortCol = null;
let catSortAsc = true;

async function poll() {
  try {
    const r = await fetch('/api/status');
    statusData = await r.json();
    render();
  } catch(e) {
    document.getElementById('ts').textContent = '⚠ Poll error: ' + e.message;
  }
}

function toggleCats() {
  const w = document.getElementById('catTableWrap');
  const b = document.getElementById('catToggle');
  const show = w.style.display === 'none';
  w.style.display = show ? '' : 'none';
  b.textContent = show ? 'Hide' : 'Show';
}

function sortCats(col) {
  if (catSortCol === col) { catSortAsc = !catSortAsc; }
  else { catSortCol = col; catSortAsc = true; }
  renderCatTable();
}

function renderCatTable() {
  const d = statusData;
  if (!d || !d.categories_table) return;
  const ct = d.categories_table;
  const rows = ct.rows;

  // sort
  const sorted = [...rows];
  if (catSortCol !== null) {
    sorted.sort((a, b) => {
      let va = a[catSortCol], vb = b[catSortCol];
      if (typeof va === 'string') va = va.toLowerCase();
      if (typeof vb === 'string') vb = vb.toLowerCase();
      if (va < vb) return catSortAsc ? -1 : 1;
      if (va > vb) return catSortAsc ? 1 : -1;
      return 0;
    });
  }

  // available sources
  const srcLabels = { local: 'Local', external: 'External' };

  // header
  const cols = [
    { key: 'name', label: 'Category', align: 'left' },
    { key: null, label: 'DL / Total', align: 'right' },
    { key: 'pct', label: '%', align: 'right' },
    { key: 'size_gb', label: 'Size', align: 'right' },
    { key: 'ul', label: 'Ul', align: 'right' },
    { key: 'rem', label: 'Rem', align: 'right' },
    { key: 'source', label: 'Source', align: 'left' },
  ];
  const thead = document.getElementById('catHead');
  thead.innerHTML = cols.map(c => {
    const cls = c.key === catSortCol ? 'sorted' : '';
    const arrow = c.key === catSortCol ? (catSortAsc ? ' ▲' : ' ▼') : '';
    const click = c.key ? `onclick="sortCats('${c.key}')"` : '';
    return `<th ${click} class="${cls}" style="text-align:${c.align}">${c.label}${arrow}</th>`;
  }).join('');

  // body rows
  const tbody = document.getElementById('catBody');
  tbody.innerHTML = sorted.map(r => {
    const pctColor = r.pct >= 100 ? '#3fb950' : r.pct >= 50 ? '#d29922' : '#58a6ff';
    const srcCls = r.source === 'external' ? 'external' : 'local';
    return `<tr>
      <td>${escHtml(r.name)}</td>
      <td class="num">${r.dl.toLocaleString()} / ${r.total.toLocaleString()}</td>
      <td class="num">
        <span class="pct-bar"><span class="fill" style="width:${Math.min(r.pct,100)}%;background:${pctColor}"></span></span>
        ${r.pct}%
      </td>
      <td class="num">${r.size_gb > 0 ? r.size_gb + 'G' : '-'}</td>
      <td class="num">${r.ul > 0 ? r.ul.toLocaleString() : '-'}</td>
      <td class="num">${r.rem > 0 ? r.rem.toLocaleString() : '-'}</td>
      <td><span class="src-tag ${srcCls}">${srcLabels[r.source] || r.source}</span></td>
    </tr>`;
  }).join('');

  // totals row
  const t = ct.totals;
  const rem_ = t.rem > 0 ? t.rem.toLocaleString() : '-';
  const footerHtml = `<tr class="totals-row">
    <td>TOTAL</td>
    <td class="num">${t.dl.toLocaleString()} / ${t.all.toLocaleString()}</td>
    <td class="num">${t.all ? Math.round(t.dl/t.all*100) + '%' : '-'}</td>
    <td class="num free">Free: ${ct.free.corpus}</td>
    <td class="num">${t.ul.toLocaleString()}</td>
    <td class="num">${rem_}</td>
    <td>${ct.free.external !== '-' ? '<span class="src-tag external">Ext: ' + ct.free.external + '</span>' : '-'}</td>
  </tr>`;
  if (ct.remaining_ul > 0) {
    footerHtml += `<tr class="totals-row" style="color:#8b949e;font-size:0.9em">
      <td>unassigned</td><td></td><td></td><td></td>
      <td class="num">${ct.remaining_ul}</td><td></td><td></td>
    </tr>`;
  }
  tbody.insertAdjacentHTML('beforeend', footerHtml);
}

function render() {
  const d = statusData;
  if (!d) return;

  const up = d.upload;
  const uz = d.unzip;
  const totalFailed = up.failed + uz.failed;
  const rate = up.total - prevUploaded;
  prevUploaded = up.total;

  const processes = Object.values(d.processes);
  const running = processes.filter(p => p.running);
  const totalWorkers = running.reduce((s, p) => s + (parseInt(p.workers) || 0), 0);

  document.getElementById('runningCount').textContent = running.length + '/' + processes.length + ' running';
  document.getElementById('workersTotal').textContent = totalWorkers + ' workers';

  document.getElementById('summary').innerHTML = `
    <div class="stat" style="border-left:3px solid #58a6ff">
      <div class="num" style="color:#58a6ff">${up.total}</div>
      <div class="lbl">Uploaded</div>
      <div class="sub">${rate > 0 ? '+' + rate : ''}</div>
    </div>
    <div class="stat" style="border-left:3px solid #3fb950">
      <div class="num" style="color:#3fb950">${d.download.completed}</div>
      <div class="lbl">Downloaded</div>
    </div>
    <div class="stat" style="border-left:3px solid #d29922">
      <div class="num" style="color:#d29922">${uz.done}</div>
      <div class="lbl">Unzipped</div>
    </div>
    <div class="stat" style="border-left:3px solid ${totalFailed > 0 ? '#da3633' : '#30363d'}">
      <div class="num" style="color:${totalFailed > 0 ? '#da3633' : '#8b949e'}">${totalFailed}</div>
      <div class="lbl">Failed</div>
    </div>
    <div class="stat" style="border-left:3px solid #8b949e">
      <div class="num" style="color:#8b949e">${Object.keys(d.upload_progress?.uploaded_by_cat || {}).length}</div>
      <div class="lbl">Categories</div>
    </div>`;

  // ── Per-process breakdown ──
  const progress = d.upload_progress?.uploaded_by_cat || {};

  let html = '';
  for (const [key, p] of Object.entries(d.processes)) {
    const isRunning = p.running;
    const dotClass = isRunning ? 'running' : 'stopped';
    const statusLabel = isRunning ? 'Running' : 'Stopped';
    const pidInfo = isRunning ? `PID ${p.pid} · ${p.uptime}` : '—';
    const cardClass = isRunning ? 'running' : 'stopped';
    const startDisabled = isRunning ? 'disabled' : '';
    const stopDisabled = !isRunning ? 'disabled' : '';

    const catStr = (p.categories || []).join(', ');

    // Retry button only for tasks that have retry_cmd AND there are failed items
    const showRetry = p.has_retry && ((key === 'unzip_push' && uz.failed > 0) || (key.includes('upload') && up.failed > 0));

    html += `
      <div class="card ${cardClass}" id="proc-${key}">
        <div class="card-header">
          <span class="status-dot ${dotClass}" title="${statusLabel}"></span>
          <h3>${p.label}</h3>
          <span class="pid">${pidInfo}</span>
        </div>
        <div class="card-body">
          <div class="meta-row"><span class="label">Workers</span><span class="value">${p.workers}</span></div>
          <div class="meta-row"><span class="label">Category</span><span class="value"><span class="cat-inline">${escHtml(catStr)}</span></span></div>
          <div class="meta-row" style="margin-top:2px"><span class="label">Status</span><span class="value" style="color:${isRunning ? '#3fb950' : '#da3633'};font-weight:600">${statusLabel}</span></div>
        </div>
        <div class="actions">
          <button class="btn-start" onclick="doAction('${key}','start')" ${startDisabled}>▶ Start</button>
          <button class="btn-stop" onclick="doAction('${key}','stop')" ${stopDisabled}>⏹ Stop</button>
          <button class="btn-restart" onclick="doAction('${key}','restart')">🔄 Restart</button>
          ${showRetry ? `<button class="btn-retry" onclick="doAction('${key}','retry')" ${startDisabled}>↻ Retry</button>` : ''}
        </div>
      </div>`;
  }
  document.getElementById('processes').innerHTML = html;
  document.getElementById('ts').innerHTML = `
    <span>Last updated: ${d.ts}</span>
    <span>Poll every ${POLL_MS/1000}s</span>
    <span>${running.length} running · ${totalWorkers} active workers</span>`;

  renderCatTable();
}

async function doAction(process, action) {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳...';
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({process, action}),
    });
    const result = await r.json();
    if (!result.ok && result.msg) alert(result.msg);
  } catch(e) { alert('Error: ' + e.message); }
  setTimeout(poll, 1500);
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

poll();
setInterval(poll, POLL_MS);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    print(f"Dashboard at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
