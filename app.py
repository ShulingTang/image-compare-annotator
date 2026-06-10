#!/usr/bin/env python3
import csv
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket
import webbrowser
import threading
import time

APP_STATE_FILE = ".recent_sessions.json"
ANNOTATION_FILE = "annotations.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
DEFAULT_PORT = int(os.environ.get("ANNOTATOR_PORT", "8765"))
AUTO_OPEN_BROWSER = os.environ.get("ANNOTATOR_AUTO_OPEN", "1") != "0"
DATASET_STATE_DIR = ".dataset_states"


def norm_path(p: str) -> str:
    return str(Path(os.path.expanduser(p)).resolve())


def stem_map(folder: Path):
    out = {}
    if not folder.exists() or not folder.is_dir():
        return out
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
            out[item.stem] = str(item.resolve())
    return out


def app_root() -> Path:
    return Path(__file__).resolve().parent


def dataset_state_dir() -> Path:
    p = app_root() / DATASET_STATE_DIR
    p.mkdir(exist_ok=True)
    return p


def dataset_state_path(dataset_id: str) -> Path:
    return dataset_state_dir() / f"{dataset_id}.json"


def empty_annotation(meta: dict):
    return {
        "version": 2,
        "mode": meta["mode"],
        "dataset_id": meta["dataset_id"],
        "dataset_name": meta.get("dataset_name", "dataset"),
        "raw_dir": meta.get("raw_dir"),
        "effect_dirs": meta.get("effect_dirs", []),
        "records": {},
        "last_group_index": 0,
        "last_selected_effect_index": 0,
        "meta": meta,
    }


def load_annotation_for_path(raw_dir: str):
    raw_dir = Path(raw_dir)
    path = raw_dir / ANNOTATION_FILE
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("records", {})
                data.setdefault("last_group_index", 0)
                data.setdefault("last_selected_effect_index", 0)
                return data, path
        except Exception:
            pass
    meta = {
        "mode": "server-path",
        "dataset_id": norm_path(str(raw_dir)),
        "dataset_name": raw_dir.name,
        "raw_dir": str(raw_dir),
        "effect_dirs": [],
    }
    return empty_annotation(meta), path


def save_annotation_for_path(raw_dir: str, data):
    _, path = load_annotation_for_path(raw_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def load_annotation_for_local(meta: dict):
    path = dataset_state_path(meta["dataset_id"])
    uploaded = meta.get("uploaded_annotation")
    if uploaded:
        data = uploaded
        data.setdefault("version", 2)
        data["mode"] = "browser-local"
        data["dataset_id"] = meta["dataset_id"]
        data["dataset_name"] = meta.get("dataset_name", data.get("dataset_name", "dataset"))
        data["meta"] = meta
        data.setdefault("records", {})
        data.setdefault("last_group_index", 0)
        data.setdefault("last_selected_effect_index", 0)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data, path
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("version", 2)
                data.setdefault("records", {})
                data.setdefault("last_group_index", 0)
                data.setdefault("last_selected_effect_index", 0)
                data["meta"] = meta
                return data, path
        except Exception:
            pass
    data = empty_annotation(meta)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data, path


def save_annotation_for_local(dataset_id: str, data):
    path = dataset_state_path(dataset_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def compute_local_dataset_id(raw_files, effect_dirs):
    payload = {
        "raw": sorted([f["name"] for f in raw_files]),
        "effects": [
            {
                "name": d.get("name", f"effect-{idx+1}"),
                "files": sorted([f["name"] for f in d.get("files", [])]),
            }
            for idx, d in enumerate(effect_dirs)
        ],
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build_stats(groups):
    total_effect_slots = 0
    labeled_effect_slots = 0
    qualified = 0
    for g in groups:
        for ef in g["effects"]:
            total_effect_slots += 1
            if ef.get("label") in ("qualified", "unqualified"):
                labeled_effect_slots += 1
                if ef["label"] == "qualified":
                    qualified += 1
    return {
        "qualified": qualified,
        "labeled": labeled_effect_slots,
        "total_effect_slots": total_effect_slots,
        "qualified_rate": (qualified / labeled_effect_slots) if labeled_effect_slots else 0,
        "progress": (labeled_effect_slots / total_effect_slots) if total_effect_slots else 0,
        "completed_groups": sum(1 for g in groups if g.get("finalized")),
        "total_groups": len(groups),
    }


def build_path_session(raw_dir: str, effect_dirs: list[str]):
    raw_dir = norm_path(raw_dir)
    effect_dirs = [norm_path(p) for p in effect_dirs if p.strip()]
    raw_map = stem_map(Path(raw_dir))
    effect_maps = [stem_map(Path(p)) for p in effect_dirs]
    all_effect_names = set().union(*[set(m.keys()) for m in effect_maps]) if effect_maps else set()
    names = sorted(set(raw_map.keys()) | all_effect_names)

    annotation, annotation_path = load_annotation_for_path(raw_dir)
    annotation.update({
        "mode": "server-path",
        "dataset_id": raw_dir,
        "dataset_name": Path(raw_dir).name,
        "raw_dir": raw_dir,
        "effect_dirs": effect_dirs,
    })

    groups = []
    for name in names:
        rec = annotation["records"].setdefault(name, {"effects": {}, "finalized": False})
        effects = []
        for idx, effect_dir in enumerate(effect_dirs):
            effects.append({
                "index": idx,
                "dir": effect_dir,
                "path": effect_maps[idx].get(name),
                "missing": effect_maps[idx].get(name) is None,
                "label": rec["effects"].get(str(idx)),
                "display_name": Path(effect_maps[idx].get(name, "")).name if effect_maps[idx].get(name) else None,
            })
        groups.append({
            "name": name,
            "raw_path": raw_map.get(name),
            "raw_missing": raw_map.get(name) is None,
            "effects": effects,
            "finalized": bool(rec.get("finalized", False)),
        })

    save_annotation_for_path(raw_dir, annotation)
    return {
        "mode": "server-path",
        "raw_dir": raw_dir,
        "effect_dirs": effect_dirs,
        "groups": groups,
        "annotation": annotation,
        "annotation_path": str(annotation_path),
        "stats": build_stats(groups),
    }


def build_local_session(payload: dict):
    raw_files = payload.get("raw_files", [])
    effect_dirs = payload.get("effect_dirs", [])
    dataset_id = compute_local_dataset_id(raw_files, effect_dirs)
    meta = {
        "mode": "browser-local",
        "dataset_id": dataset_id,
        "dataset_name": payload.get("dataset_name") or f"browser-local-{dataset_id}",
        "raw_count": len(raw_files),
        "effect_dir_names": [d.get("name", f"效果图{idx+1}") for idx, d in enumerate(effect_dirs)],
        "uploaded_annotation": payload.get("imported_annotation"),
        "raw_files": raw_files,
        "effect_dirs_payload": effect_dirs,
    }
    raw_map = {Path(f["name"]).stem: f for f in raw_files}
    effect_maps = []
    for d in effect_dirs:
        effect_maps.append({Path(f["name"]).stem: f for f in d.get("files", [])})
    all_effect_names = set().union(*[set(m.keys()) for m in effect_maps]) if effect_maps else set()
    names = sorted(set(raw_map.keys()) | all_effect_names)

    annotation, annotation_path = load_annotation_for_local(meta)
    groups = []
    for name in names:
        rec = annotation["records"].setdefault(name, {"effects": {}, "finalized": False})
        effects = []
        for idx, d in enumerate(effect_dirs):
            file_info = effect_maps[idx].get(name)
            effects.append({
                "index": idx,
                "dir": d.get("name", f"效果图{idx+1}"),
                "path": None,
                "missing": file_info is None,
                "label": rec["effects"].get(str(idx)),
                "display_name": file_info.get("name") if file_info else None,
                "client_file_key": file_info.get("client_file_key") if file_info else None,
            })
        raw_info = raw_map.get(name)
        groups.append({
            "name": name,
            "raw_path": None,
            "raw_missing": raw_info is None,
            "raw_display_name": raw_info.get("name") if raw_info else None,
            "raw_client_file_key": raw_info.get("client_file_key") if raw_info else None,
            "effects": effects,
            "finalized": bool(rec.get("finalized", False)),
        })
    save_annotation_for_local(dataset_id, annotation)
    return {
        "mode": "browser-local",
        "dataset_id": dataset_id,
        "raw_dir": None,
        "effect_dirs": [d.get("name", f"效果图{idx+1}") for idx, d in enumerate(effect_dirs)],
        "groups": groups,
        "annotation": annotation,
        "annotation_path": str(annotation_path),
        "stats": build_stats(groups),
    }


def load_annotation_by_mode(mode: str, dataset_key: str):
    if mode == "browser-local":
        path = dataset_state_path(dataset_key)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), path
    return load_annotation_for_path(dataset_key)


def save_annotation_by_mode(mode: str, dataset_key: str, data: dict):
    if mode == "browser-local":
        return save_annotation_for_local(dataset_key, data)
    return save_annotation_for_path(dataset_key, data)


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_available_port(start_port: int) -> int:
    port = start_port
    while not is_port_free(port):
        port += 1
    return port


def export_payload(annotation: dict):
    rows = []
    qualified_names = []
    unqualified_names = []
    for group_name, rec in annotation.get("records", {}).items():
        for k, v in sorted(rec.get("effects", {}).items(), key=lambda x: int(x[0])):
            row = {"group_name": group_name, "effect_index": int(k), "label": v}
            rows.append(row)
            name = f"{group_name}\tcol{int(k)+1}"
            if v == "qualified":
                qualified_names.append(name)
            elif v == "unqualified":
                unqualified_names.append(name)
    total = len(rows)
    qualified = len(qualified_names)
    unqualified = len(unqualified_names)
    return {
        "dataset_id": annotation.get("dataset_id"),
        "dataset_name": annotation.get("dataset_name"),
        "mode": annotation.get("mode"),
        "total_groups": len(annotation.get("records", {})),
        "total_columns": total,
        "labeled_columns": total,
        "qualified_columns": qualified,
        "unqualified_columns": unqualified,
        "qualified_rate": (qualified / total) if total else 0,
        "progress": 1 if total else 0,
        "qualified_names": qualified_names,
        "unqualified_names": unqualified_names,
        "records": rows,
    }


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<title>图片对比标注工具</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root{
  --bg:#0b0d12; --bg-elev:#10131b; --surface:#161a24; --surface-2:#1c2130; --surface-3:#232a3a;
  --border:rgba(255,255,255,.08); --border-strong:rgba(255,255,255,.16);
  --text:#e9ebf0; --muted:#9aa3b4; --dim:#6b7484;
  --accent:#5b6cf0; --accent-2:#4453d6; --accent-soft:rgba(91,108,240,.16);
  --green:#22c55e; --green-soft:rgba(34,197,94,.15);
  --red:#ef4444; --red-soft:rgba(239,68,68,.15);
  --amber:#f59e0b;
  --radius:14px; --radius-sm:10px; --radius-xs:8px;
  --ease:cubic-bezier(.16,1,.3,1); --dur:200ms;
  --shadow:0 10px 34px rgba(0,0,0,.45); --shadow-sm:0 2px 10px rgba(0,0,0,.35);
  --font:'Inter',system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
}
*{ box-sizing:border-box; }
html,body{ height:100%; }
body{ margin:0; font-family:var(--font); background:
  radial-gradient(1200px 700px at 80% -10%, rgba(91,108,240,.10), transparent 60%),
  radial-gradient(900px 600px at -10% 110%, rgba(34,197,94,.06), transparent 55%),
  var(--bg);
  color:var(--text); font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased; }
::-webkit-scrollbar{ width:10px; height:10px; }
::-webkit-scrollbar-thumb{ background:#2a3140; border-radius:99px; border:2px solid transparent; background-clip:padding-box; }
::-webkit-scrollbar-thumb:hover{ background:#36405468; }
button{ font-family:inherit; }
.app{ display:flex; height:100vh; min-height:0; }

/* ---------- Sidebar ---------- */
.sidebar{ width:340px; flex:0 0 340px; background:linear-gradient(180deg,var(--bg-elev),#0c0f16);
  border-right:1px solid var(--border); padding:14px; overflow-y:auto; display:flex; flex-direction:column; gap:12px; }
.brand{ display:flex; align-items:center; gap:10px; }
.brand .logo{ width:34px; height:34px; border-radius:10px; background:linear-gradient(135deg,var(--accent),#7c8cff);
  display:grid; place-items:center; box-shadow:0 6px 18px rgba(91,108,240,.4); flex:0 0 auto; }
.brand .logo svg{ width:20px; height:20px; color:#fff; }
.brand .title{ font-size:15px; font-weight:700; letter-spacing:.2px; }
.brand .sub{ font-size:11px; color:var(--dim); margin-top:1px; }
.card{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:13px; }
.card.tight{ padding:10px; }
.section-label{ font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:var(--dim); font-weight:600; margin-bottom:10px; display:flex; align-items:center; justify-content:space-between; }
.field{ display:flex; flex-direction:column; gap:6px; margin-bottom:10px; }
.field:last-child{ margin-bottom:0; }
label.lbl{ font-size:12px; color:var(--muted); font-weight:500; }
input[type="text"], input[type="number"]{ width:100%; padding:9px 11px; border-radius:var(--radius-xs); background:#0c0f16;
  color:var(--text); border:1px solid var(--border-strong); font-size:13px; transition:border-color var(--dur), box-shadow var(--dur); }
input[type="text"]:focus, input[type="number"]:focus{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
input[type="file"]{ width:100%; font-size:12px; color:var(--muted); padding:7px; border:1px dashed var(--border-strong);
  border-radius:var(--radius-xs); background:#0c0f16; cursor:pointer; }
input[type="file"]::file-selector-button{ font-family:inherit; margin-right:10px; padding:6px 12px; border-radius:7px; border:none;
  background:var(--surface-3); color:var(--text); cursor:pointer; font-size:12px; }

.btn{ display:inline-flex; align-items:center; justify-content:center; gap:7px; background:var(--accent); color:#fff; border:none;
  border-radius:var(--radius-xs); padding:9px 13px; cursor:pointer; font-size:13px; font-weight:600;
  transition:transform var(--dur) var(--ease), background var(--dur), box-shadow var(--dur), opacity var(--dur); white-space:nowrap; }
.btn svg{ width:16px; height:16px; }
.btn:hover{ background:var(--accent-2); }
.btn:active{ transform:scale(.96); }
.btn:focus-visible{ outline:none; box-shadow:0 0 0 3px var(--accent-soft); }
.btn.block{ width:100%; }
.btn.ghost{ background:var(--surface-2); color:var(--text); border:1px solid var(--border-strong); }
.btn.ghost:hover{ background:var(--surface-3); }
.btn.good{ background:var(--green); } .btn.good:hover{ background:#1ba34d; }
.btn.bad{ background:var(--red); } .btn.bad:hover{ background:#d63a3a; }
.btn.sm{ padding:7px 10px; font-size:12px; }
.btn.icon{ padding:8px; width:36px; height:36px; }
.btn:disabled{ opacity:.45; cursor:not-allowed; transform:none; }

/* ---------- Mode tabs ---------- */
.tabs{ display:flex; background:#0c0f16; border:1px solid var(--border); border-radius:var(--radius-xs); padding:3px; gap:3px; }
.tab{ flex:1; padding:8px; border:none; background:transparent; color:var(--muted); border-radius:7px; cursor:pointer;
  font-size:12px; font-weight:600; transition:background var(--dur), color var(--dur); }
.tab.active{ background:var(--surface-3); color:var(--text); box-shadow:var(--shadow-sm); }

/* ---------- Stats / progress ---------- */
.progress-track{ height:8px; background:#0c0f16; border-radius:99px; overflow:hidden; border:1px solid var(--border); }
.progress-fill{ height:100%; background:linear-gradient(90deg,var(--accent),#7c8cff); border-radius:99px;
  width:0%; transition:width 400ms var(--ease); }
.stats{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:11px; }
.stat{ background:#0c0f16; border:1px solid var(--border); border-radius:var(--radius-sm); padding:9px 10px; }
.stat .k{ font-size:11px; color:var(--dim); } .stat .v{ font-size:17px; font-weight:700; margin-top:3px; font-variant-numeric:tabular-nums; }

.badge{ display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:99px; font-size:11px; font-weight:700; }
.badge.good{ background:var(--green-soft); color:#86efac; } .badge.bad{ background:var(--red-soft); color:#fca5a5; }
.badge.none{ background:rgba(148,163,184,.14); color:#cbd5e1; }
.badge .dot{ width:6px; height:6px; border-radius:99px; background:currentColor; }

/* ---------- Recent / navigator lists ---------- */
.list{ display:flex; flex-direction:column; gap:6px; max-height:240px; overflow-y:auto; }
.list-item{ text-align:left; width:100%; background:#0c0f16; border:1px solid var(--border); color:var(--text);
  border-radius:var(--radius-xs); padding:8px 10px; cursor:pointer; font-size:12px; display:flex; align-items:center; gap:8px;
  transition:background var(--dur), border-color var(--dur); }
.list-item:hover{ background:var(--surface-2); border-color:var(--border-strong); }
.list-item.active{ border-color:var(--accent); background:var(--accent-soft); }
.list-item .nm{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.list-item .idx{ color:var(--dim); font-variant-numeric:tabular-nums; font-size:11px; }
.tag{ font-size:10px; padding:1px 6px; border-radius:5px; background:var(--surface-3); color:var(--muted); flex:0 0 auto; }
.chips{ display:flex; gap:6px; margin-bottom:9px; flex-wrap:wrap; }
.chip{ padding:5px 10px; border-radius:99px; border:1px solid var(--border-strong); background:transparent; color:var(--muted);
  font-size:11px; cursor:pointer; transition:all var(--dur); font-weight:600; }
.chip.active{ background:var(--accent-soft); border-color:var(--accent); color:#c7ceff; }
.hint{ color:var(--dim); font-size:11px; line-height:1.6; }
.empty-mini{ color:var(--dim); font-size:12px; text-align:center; padding:14px 6px; }

/* ---------- Main ---------- */
.main{ flex:1; display:flex; flex-direction:column; min-width:0; position:relative; }
.loadbar{ position:absolute; top:0; left:0; right:0; height:2px; overflow:hidden; z-index:50; opacity:0; transition:opacity 200ms; }
.loadbar.on{ opacity:1; }
.loadbar::after{ content:''; position:absolute; height:100%; width:40%; background:linear-gradient(90deg,transparent,var(--accent),transparent);
  animation:slide 1s infinite linear; }
@keyframes slide{ 0%{ left:-40%; } 100%{ left:100%; } }

.toolbar{ display:flex; align-items:center; gap:8px; padding:11px 16px; border-bottom:1px solid var(--border);
  background:rgba(16,19,27,.7); backdrop-filter:blur(8px); flex-wrap:wrap; }
.tgroup{ display:flex; align-items:center; gap:6px; }
.divider{ width:1px; height:24px; background:var(--border-strong); margin:0 3px; }
.segmented{ display:flex; background:#0c0f16; border:1px solid var(--border-strong); border-radius:var(--radius-xs); padding:3px; gap:3px; }
.seg{ display:inline-flex; align-items:center; gap:6px; padding:6px 11px; border:none; background:transparent; color:var(--muted);
  border-radius:7px; cursor:pointer; font-size:12px; font-weight:600; transition:all var(--dur); }
.seg svg{ width:15px; height:15px; } .seg.active{ background:var(--surface-3); color:var(--text); }
.zoom-level{ min-width:46px; text-align:center; font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
.group-title{ margin-left:auto; font-size:13px; color:var(--muted); display:flex; align-items:center; gap:10px; }
.group-title b{ color:var(--text); }
.switch{ display:inline-flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); cursor:pointer; user-select:none; }
.switch input{ position:absolute; opacity:0; pointer-events:none; }
.switch .track{ width:34px; height:19px; border-radius:99px; background:#2a3140; position:relative; transition:background var(--dur); }
.switch .track::after{ content:''; position:absolute; top:2px; left:2px; width:15px; height:15px; border-radius:99px; background:#fff;
  transition:transform var(--dur) var(--ease); }
.switch input:checked + .track{ background:var(--accent); } .switch input:checked + .track::after{ transform:translateX(15px); }

/* ---------- Viewer / stage ---------- */
.viewer{ flex:1; min-height:0; display:flex; flex-direction:column; padding:16px; gap:13px; overflow:hidden; }
.stage-wrap{ flex:1; min-height:0; border-radius:var(--radius); border:2px solid var(--border-strong);
  background:repeating-conic-gradient(#0c0f16 0% 25%, #0e1219 0% 50%) 50%/22px 22px; position:relative; overflow:hidden;
  display:flex; align-items:center; justify-content:center; transition:border-color var(--dur); touch-action:none; }
.stage-wrap.good{ border-color:var(--green); } .stage-wrap.bad{ border-color:var(--red); }

.empty-state{ display:flex; flex-direction:column; align-items:center; gap:12px; color:var(--dim); padding:30px; text-align:center; }
.empty-state svg{ width:54px; height:54px; opacity:.5; }
.empty-state .es-t{ font-size:15px; color:var(--muted); font-weight:600; }

/* slider mode */
.slider-stage{ position:relative; display:inline-block; cursor:grab; will-change:transform; }
.slider-stage.panning{ cursor:grabbing; }
.slider-stage img{ display:block; max-width:min(1600px, calc(100vw - 420px)); max-height:calc(100vh - 240px);
  object-fit:contain; user-select:none; -webkit-user-drag:none; }
.slider-stage .eff{ position:absolute; inset:0; width:100%; height:100%; object-fit:contain; }
.handle{ position:absolute; top:0; bottom:0; width:2px; background:#fff; box-shadow:0 0 0 1px rgba(0,0,0,.4); cursor:ew-resize; z-index:3; }
.handle::before{ content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:30px; height:30px;
  border-radius:99px; background:#fff; box-shadow:0 2px 10px rgba(0,0,0,.5); }
.handle::after{ content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:14px; height:14px;
  background:var(--accent); border-radius:99px; }
.side-tag{ position:absolute; top:8px; padding:3px 9px; border-radius:99px; font-size:11px; font-weight:700; z-index:4;
  background:rgba(11,13,18,.78); backdrop-filter:blur(4px); pointer-events:none; }
.side-tag.l{ left:8px; color:#cbd5e1; } .side-tag.r{ right:8px; color:#c7ceff; }

/* side-by-side mode */
.side-stage{ display:flex; gap:12px; align-items:center; justify-content:center; width:100%; height:100%; padding:8px; }
.side-pane{ flex:1; min-width:0; height:100%; display:flex; flex-direction:column; gap:6px; align-items:center; justify-content:center; overflow:hidden; }
.side-pane .pane-label{ font-size:11px; font-weight:700; color:var(--muted); flex:0 0 auto; }
.side-pane .pane-img{ flex:1; min-height:0; display:flex; align-items:center; justify-content:center; cursor:grab; will-change:transform; }
.side-pane .pane-img.panning{ cursor:grabbing; }
.side-pane img{ max-width:100%; max-height:100%; object-fit:contain; user-select:none; -webkit-user-drag:none; display:block; }

.missing-box{ display:flex; align-items:center; justify-content:center; color:var(--dim); font-size:13px;
  width:240px; height:160px; border:1px dashed var(--border-strong); border-radius:var(--radius-sm); }

/* ---------- Thumbnails ---------- */
.thumbs{ display:flex; gap:10px; overflow-x:auto; padding-bottom:4px; flex:0 0 auto; }
.thumb{ position:relative; min-width:158px; width:158px; background:var(--surface); border:2px solid var(--border-strong);
  border-radius:var(--radius-sm); padding:8px; cursor:pointer; transition:transform var(--dur) var(--ease), border-color var(--dur); }
.thumb:hover{ transform:translateY(-2px); }
.thumb.active{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
.thumb.good{ border-color:var(--green); } .thumb.bad{ border-color:var(--red); }
.thumb .pic, .thumb .missing{ width:100%; height:96px; object-fit:contain; background:#0c0f16; border-radius:7px; display:flex;
  align-items:center; justify-content:center; color:var(--dim); font-size:12px; }
.thumb .col-no{ font-size:12px; font-weight:600; margin-top:7px; display:flex; align-items:center; justify-content:space-between; }
.thumb .fn{ color:var(--dim); font-size:10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px; }

/* ---------- Toast ---------- */
.toast-wrap{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); display:flex; flex-direction:column; gap:8px;
  z-index:200; align-items:center; }
.toast{ display:flex; align-items:center; gap:9px; background:var(--surface-2); border:1px solid var(--border-strong);
  color:var(--text); padding:10px 15px; border-radius:99px; font-size:13px; font-weight:500; box-shadow:var(--shadow);
  animation:toastin 260ms var(--ease); }
.toast svg{ width:16px; height:16px; }
.toast.good{ border-color:rgba(34,197,94,.5); } .toast.good svg{ color:var(--green); }
.toast.bad{ border-color:rgba(239,68,68,.5); } .toast.bad svg{ color:var(--red); }
.toast.info svg{ color:var(--accent); }
@keyframes toastin{ from{ opacity:0; transform:translateY(10px) scale(.96); } to{ opacity:1; transform:none; } }

/* ---------- Modal ---------- */
.modal{ position:fixed; inset:0; background:rgba(5,7,12,.6); backdrop-filter:blur(4px); z-index:150; display:grid;
  place-items:center; padding:20px; animation:fade 200ms var(--ease); }
@keyframes fade{ from{ opacity:0; } to{ opacity:1; } }
.modal-card{ background:var(--surface); border:1px solid var(--border-strong); border-radius:var(--radius); padding:22px;
  width:min(460px,100%); box-shadow:var(--shadow); animation:pop 240ms var(--ease); }
@keyframes pop{ from{ opacity:0; transform:scale(.94) translateY(8px); } to{ opacity:1; transform:none; } }
.modal-card h3{ margin:0 0 14px; font-size:16px; display:flex; align-items:center; justify-content:space-between; }
.kbd-row{ display:flex; align-items:center; justify-content:space-between; padding:7px 0; border-bottom:1px solid var(--border); font-size:13px; }
.kbd-row:last-child{ border-bottom:none; }
.kbd-row .keys{ display:flex; gap:5px; }
kbd{ font-family:var(--font); background:#0c0f16; border:1px solid var(--border-strong); border-bottom-width:2px;
  border-radius:6px; padding:2px 8px; font-size:12px; color:var(--text); min-width:24px; text-align:center; }
.x-btn{ background:transparent; border:none; color:var(--muted); cursor:pointer; padding:4px; border-radius:6px; }
.x-btn:hover{ background:var(--surface-3); color:var(--text); } .x-btn svg{ width:18px; height:18px; }

.hidden{ display:none !important; }
@media (prefers-reduced-motion: reduce){ *{ animation-duration:.001ms !important; transition-duration:.001ms !important; } }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M12 3v18"/><path d="M3 9h6"/><path d="M3 15h6"/></svg></div>
      <div><div class="title">图片对比标注</div><div class="sub">Image Compare Annotator</div></div>
    </div>

    <div class="card tight">
      <div class="tabs">
        <button id="modeServerBtn" class="tab active">服务端目录</button>
        <button id="modeLocalBtn" class="tab">浏览器本地</button>
      </div>
    </div>

    <div class="card" id="serverModePanel">
      <div class="section-label">服务端目录模式</div>
      <div class="field">
        <label class="lbl">原图目录</label>
        <input id="rawDir" type="text" placeholder="/path/to/raw" />
      </div>
      <div id="effectDirList"></div>
      <div class="tgroup" style="margin-top:10px; gap:8px;">
        <button id="addEffectBtn" class="btn ghost sm">+ 效果图目录</button>
        <button id="loadBtn" class="btn sm" style="margin-left:auto;">加载数据</button>
      </div>
    </div>

    <div class="card hidden" id="localModePanel">
      <div class="section-label">浏览器本地模式</div>
      <div class="hint" style="margin-bottom:10px;">图片不上传，浏览器按需读取；标注结果实时保存到服务端。</div>
      <div class="field">
        <label class="lbl">原图文件夹</label>
        <input id="rawFolderInput" type="file" webkitdirectory directory multiple />
      </div>
      <div id="localEffectInputs"></div>
      <div class="tgroup" style="margin:10px 0; gap:8px;">
        <button id="addLocalEffectBtn" class="btn ghost sm">+ 效果图文件夹</button>
        <button id="loadLocalBtn" class="btn sm" style="margin-left:auto;">加载本地数据</button>
      </div>
      <div class="field">
        <label class="lbl">可选：导入之前导出的结果 JSON</label>
        <input id="importAnnotationInput" type="file" accept="application/json,.json" />
      </div>
    </div>

    <div class="card">
      <div class="section-label">最近使用 <button id="reloadRecentBtn" class="x-btn" title="刷新"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button></div>
      <div id="recentList" class="list"></div>
    </div>

    <div class="card">
      <div class="section-label">进度概览</div>
      <div class="progress-track"><div id="progressFill" class="progress-fill"></div></div>
      <div class="stats">
        <div class="stat"><div class="k">合格率</div><div class="v" id="qualifiedRate">0%</div></div>
        <div class="stat"><div class="k">标注进度</div><div class="v" id="progressRate">0%</div></div>
        <div class="stat"><div class="k">已标列数</div><div class="v" id="labeledCount">0/0</div></div>
        <div class="stat"><div class="k">完成组数</div><div class="v" id="groupCount">0/0</div></div>
      </div>
    </div>

    <div class="card">
      <div class="section-label">分组导航</div>
      <div class="field">
        <input id="groupSearch" type="text" placeholder="搜索文件名…" />
      </div>
      <div class="chips">
        <button class="chip active" data-filter="all">全部</button>
        <button class="chip" data-filter="todo">未完成</button>
        <button class="chip" data-filter="done">已完成</button>
      </div>
      <div id="groupNav" class="list"><div class="empty-mini">加载数据后显示</div></div>
    </div>

    <div class="card">
      <div class="section-label">导出</div>
      <div class="tgroup" style="gap:8px; margin-bottom:8px;">
        <button id="exportQualifiedBtn" class="btn ghost sm" style="flex:1;">合格名单</button>
        <button id="exportUnqualifiedBtn" class="btn ghost sm" style="flex:1;">不合格名单</button>
      </div>
      <button id="exportSummaryBtn" class="btn ghost sm block">导出汇总 JSON</button>
    </div>

    <div class="card tight">
      <div class="tgroup" style="justify-content:space-between;">
        <span class="hint">需要帮助？</span>
        <button id="helpBtn" class="btn ghost sm">快捷键 ?</button>
      </div>
    </div>
  </aside>

  <main class="main">
    <div id="loadbar" class="loadbar"></div>
    <div class="toolbar">
      <div class="tgroup">
        <button id="prevBtn" class="btn ghost icon" title="上一组 (←)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></button>
        <button id="nextBtn" class="btn ghost icon" title="下一组 (→)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg></button>
        <button id="nextTodoBtn" class="btn ghost sm" title="跳到下一个未完成 (N)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m13 17 5-5-5-5"/><path d="M6 17V7"/></svg>下一个未完成</button>
      </div>
      <div class="divider"></div>
      <div class="tgroup">
        <button id="markGoodBtn" class="btn good sm" title="标合格 (=)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>合格</button>
        <button id="markBadBtn" class="btn bad sm" title="标不合格 (-)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>不合格</button>
      </div>
      <div class="divider"></div>
      <div class="segmented">
        <button id="modeSliderSeg" class="seg active" title="滑块叠加对比"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/></svg>滑块</button>
        <button id="modeSideSeg" class="seg" title="并排对比"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18" rx="1.5"/><rect x="14" y="3" width="7" height="18" rx="1.5"/></svg>并排</button>
      </div>
      <div class="divider"></div>
      <div class="tgroup">
        <button id="zoomOutBtn" class="btn ghost icon" title="缩小"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/></svg></button>
        <span id="zoomLevel" class="zoom-level">100%</span>
        <button id="zoomInBtn" class="btn ghost icon" title="放大"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/></svg></button>
        <button id="zoomResetBtn" class="btn ghost icon" title="重置视图 (F)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/></svg></button>
      </div>
      <div class="group-title" id="groupTitle">未加载</div>
    </div>

    <div class="viewer">
      <div id="stageWrap" class="stage-wrap">
        <div id="emptyState" class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M12 3v18"/><circle cx="7.5" cy="8" r="1.2" fill="currentColor"/><path d="m4 15 2.5-2.5 2 2"/></svg>
          <div class="es-t">请先在左侧加载图片目录</div>
          <div class="hint">支持服务端目录或浏览器本地文件夹</div>
        </div>

        <!-- slider mode -->
        <div id="sliderStage" class="slider-stage hidden">
          <span class="side-tag l">原图</span>
          <span class="side-tag r">效果图</span>
          <img id="rawImage" alt="原图" />
          <img id="effImage" class="eff" alt="效果图" />
          <div id="sliderHandle" class="handle"></div>
        </div>

        <!-- side by side mode -->
        <div id="sideStage" class="side-stage hidden">
          <div class="side-pane">
            <div class="pane-label">原图</div>
            <div class="pane-img" id="sideRawPane"></div>
          </div>
          <div class="side-pane">
            <div class="pane-label" id="sideEffLabel">效果图</div>
            <div class="pane-img" id="sideEffPane"></div>
          </div>
        </div>
      </div>
      <div class="thumbs" id="thumbs"></div>
    </div>
  </main>
</div>

<div id="toastWrap" class="toast-wrap"></div>

<div id="helpModal" class="modal hidden">
  <div class="modal-card">
    <h3>快捷键<button class="x-btn" id="helpClose"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button></h3>
    <div class="kbd-row"><span>标合格</span><span class="keys"><kbd>=</kbd></span></div>
    <div class="kbd-row"><span>标不合格</span><span class="keys"><kbd>-</kbd></span></div>
    <div class="kbd-row"><span>上一组 / 下一组</span><span class="keys"><kbd>←</kbd><kbd>↑</kbd> / <kbd>→</kbd><kbd>↓</kbd></span></div>
    <div class="kbd-row"><span>跳到下一个未完成</span><span class="keys"><kbd>N</kbd></span></div>
    <div class="kbd-row"><span>选择第 1/2/3 列</span><span class="keys"><kbd>1</kbd><kbd>2</kbd><kbd>3</kbd></span></div>
    <div class="kbd-row"><span>逐列切换</span><span class="keys"><kbd>Tab</kbd></span></div>
    <div class="kbd-row"><span>滑块快速翻转 (原图↔效果)</span><span class="keys"><kbd>Space</kbd></span></div>
    <div class="kbd-row"><span>切换滑块 / 并排模式</span><span class="keys"><kbd>S</kbd></span></div>
    <div class="kbd-row"><span>重置缩放/平移</span><span class="keys"><kbd>F</kbd></span></div>
    <div class="kbd-row"><span>滚轮缩放 · 拖拽平移</span><span class="hint">在图片区域</span></div>
  </div>
</div>

<script>
const SVG = {
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  x:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
  info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>'
};
const state = { session:null, groupIndex:0, effectIndex:0, mode:'server-path', localFiles:new Map(), objUrls:new Map(),
  compareMode:'slider', sliderPct:50, sliderToggleRight:false, zoom:{scale:1,x:0,y:0}, filter:'all', search:'', busy:0 };

const $ = (id) => document.getElementById(id);

/* ---------- feedback ---------- */
function toast(msg, type='info'){
  const wrap = $('toastWrap');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `${SVG[type==='good'?'check':type==='bad'?'x':'info']}<span>${msg}</span>`;
  wrap.appendChild(el);
  setTimeout(()=>{ el.style.transition='opacity .3s, transform .3s'; el.style.opacity='0'; el.style.transform='translateY(8px)'; setTimeout(()=>el.remove(),300); }, 2400);
}
function setBusy(on){ state.busy = Math.max(0, state.busy + (on?1:-1)); $('loadbar').classList.toggle('on', state.busy>0); }

/* ---------- mode panels ---------- */
function setMode(mode){
  state.mode = mode;
  $('serverModePanel').classList.toggle('hidden', mode !== 'server-path');
  $('localModePanel').classList.toggle('hidden', mode !== 'browser-local');
  $('modeServerBtn').classList.toggle('active', mode === 'server-path');
  $('modeLocalBtn').classList.toggle('active', mode === 'browser-local');
}
function effectInput(idx, value=''){
  const wrap = document.createElement('div');
  wrap.className = 'field';
  wrap.innerHTML = `<label class="lbl">效果图目录 ${idx+1}</label><div class="tgroup" style="gap:8px;"><input type="text" placeholder="/path/to/effect-${idx+1}" value="${value}"><button class="btn ghost icon" data-remove="${idx}" title="删除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button></div>`;
  return wrap;
}
function localEffectInput(idx){
  const wrap = document.createElement('div');
  wrap.className = 'field';
  wrap.innerHTML = `<label class="lbl">效果图文件夹 ${idx+1}</label><div class="tgroup" style="gap:8px;"><input type="file" data-local-effect="${idx}" webkitdirectory directory multiple /><button class="btn ghost icon" data-remove-local="${idx}" title="删除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button></div>`;
  return wrap;
}
function renderEffectInputs(values=[]){
  const box = $('effectDirList'); box.innerHTML = '';
  const arr = values.length ? values : [''];
  arr.forEach((v,i)=> box.appendChild(effectInput(i,v)));
  box.querySelectorAll('[data-remove]').forEach(btn => btn.onclick = ()=>{ const vals = getEffectDirs(); vals.splice(Number(btn.dataset.remove),1); renderEffectInputs(vals.length?vals:['']); });
}
function renderLocalEffectInputs(count=1){
  const box = $('localEffectInputs'); box.innerHTML = '';
  for(let i=0;i<count;i++) box.appendChild(localEffectInput(i));
  box.querySelectorAll('[data-remove-local]').forEach(btn => btn.onclick = ()=> renderLocalEffectInputs(Math.max(1,count-1)));
}
function getEffectDirs(){ return [...document.querySelectorAll('#effectDirList input[type=text]')].map(x=>x.value.trim()).filter(Boolean); }

/* ---------- api ---------- */
async function api(path, options={}){
  setBusy(true);
  try{
    const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...options });
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || '请求失败');
    return data;
  } finally { setBusy(false); }
}
async function loadRecent(){
  const data = await api('/api/recent');
  const list = $('recentList'); list.innerHTML = '';
  (data.items || []).forEach(item => {
    const b = document.createElement('button');
    b.className = 'list-item';
    const isLocal = item.mode === 'browser-local';
    b.innerHTML = `<span class="tag">${isLocal?'本地':'目录'}</span><span class="nm">${item.label}</span>`;
    b.onclick = ()=>{
      if(item.mode === 'server-path'){
        setMode('server-path');
        $('rawDir').value = item.raw_dir || '';
        renderEffectInputs(item.effect_dirs && item.effect_dirs.length ? item.effect_dirs : ['']);
        toast('已填入目录，点击「加载数据」', 'info');
      } else {
        setMode('browser-local');
        toast('本地模式需重新选择文件夹', 'info');
      }
    };
    list.appendChild(b);
  });
  if(!list.children.length) list.innerHTML = '<div class="empty-mini">暂无记录</div>';
}
function badge(label){
  if(label==='qualified') return '<span class="badge good"><span class="dot"></span>合格</span>';
  if(label==='unqualified') return '<span class="badge bad"><span class="dot"></span>不合格</span>';
  return '<span class="badge none">未标注</span>';
}
function currentGroup(){ return state.session?.groups?.[state.groupIndex]; }
function currentEffect(){ return currentGroup()?.effects?.[state.effectIndex]; }
function isGroupDone(g){
  if(!g) return true;
  if(g.finalized) return true;
  if(!g.effects.length) return true;
  return g.effects.every(e => e.missing || e.label==='qualified' || e.label==='unqualified');
}

/* ---------- render ---------- */
function renderStats(){
  const s = state.session?.stats; if(!s) return;
  $('qualifiedRate').textContent = (s.qualified_rate*100).toFixed(1)+'%';
  $('progressRate').textContent = (s.progress*100).toFixed(1)+'%';
  $('labeledCount').textContent = `${s.labeled}/${s.total_effect_slots}`;
  $('groupCount').textContent = `${s.completed_groups}/${s.total_groups}`;
  $('progressFill').style.width = (s.progress*100).toFixed(1)+'%';
}
function renderGroupTitle(){
  const g = currentGroup();
  $('groupTitle').innerHTML = g
    ? `<span>组 <b>${state.groupIndex+1}</b> / ${state.session.groups.length}</span><span>·</span><span title="${g.name}"><b>${g.name}</b></span>${g.effects.length>1?`<span>· 第 <b>${state.effectIndex+1}</b> 列</span>`:''}`
    : '未加载';
}
function renderGroupNav(){
  const box = $('groupNav');
  if(!state.session){ box.innerHTML = '<div class="empty-mini">加载数据后显示</div>'; return; }
  const q = state.search.toLowerCase();
  const items = [];
  state.session.groups.forEach((g, i) => {
    if(q && !g.name.toLowerCase().includes(q)) return;
    const done = isGroupDone(g);
    if(state.filter==='todo' && done) return;
    if(state.filter==='done' && !done) return;
    items.push({ g, i, done });
  });
  if(!items.length){ box.innerHTML = '<div class="empty-mini">无匹配分组</div>'; return; }
  const capped = items.slice(0, 500);
  box.innerHTML = '';
  capped.forEach(({g,i,done})=>{
    const b = document.createElement('button');
    b.className = `list-item ${i===state.groupIndex?'active':''}`;
    const st = done ? '<span class="badge good" style="padding:1px 6px;"><span class="dot"></span></span>'
                    : '<span class="badge none" style="padding:1px 6px;">·</span>';
    b.innerHTML = `<span class="idx">${i+1}</span><span class="nm">${g.name}</span>${st}`;
    b.onclick = ()=> setGroupTo(i);
    box.appendChild(b);
  });
  if(items.length > 500){ const m = document.createElement('div'); m.className='empty-mini'; m.textContent=`仅显示前 500 / 共 ${items.length}`; box.appendChild(m); }
}
function fileUrl(key){
  if(!key) return null;
  if(state.objUrls.has(key)) return state.objUrls.get(key);
  const file = state.localFiles.get(key);
  if(!file) return null;
  const url = URL.createObjectURL(file);
  state.objUrls.set(key, url);
  return url;
}
function getRawSrc(group){
  if(state.session.mode === 'browser-local') return fileUrl(group.raw_client_file_key);
  return group.raw_path ? '/api/file?path=' + encodeURIComponent(group.raw_path) : null;
}
function getEffectSrc(effect){
  if(state.session.mode === 'browser-local') return fileUrl(effect.client_file_key);
  return effect.path ? '/api/file?path=' + encodeURIComponent(effect.path) : null;
}
function renderThumbs(){
  const thumbs = $('thumbs'); thumbs.innerHTML = '';
  const group = currentGroup(); if(!group) return;
  group.effects.forEach((ef, idx) => {
    const src = getEffectSrc(ef);
    const div = document.createElement('div');
    div.className = `thumb ${idx===state.effectIndex?'active':''} ${ef.label==='qualified'?'good':''} ${ef.label==='unqualified'?'bad':''}`;
    div.innerHTML = `${src ? `<img class="pic" src="${src}">` : `<div class="missing">缺失</div>`}`
      + `<div class="col-no"><span>列 ${idx+1}</span>${badge(ef.label)}</div>`
      + `<div class="fn">${ef.display_name || '无对应文件'}</div>`;
    div.onclick = async ()=>{ state.effectIndex = idx; await persistView(); renderAll(); };
    thumbs.appendChild(div);
  });
}
function applyZoom(){
  const t = state.zoom;
  const tr = `translate(${t.x}px,${t.y}px) scale(${t.scale})`;
  document.querySelectorAll('.ztrans').forEach(el => { el.style.transform = tr; });
  $('zoomLevel').textContent = Math.round(t.scale*100)+'%';
}
function resetZoom(){ state.zoom = {scale:1,x:0,y:0}; applyZoom(); }
function applyBorder(label){
  const wrap = $('stageWrap');
  wrap.classList.toggle('good', label==='qualified');
  wrap.classList.toggle('bad', label==='unqualified');
}
function renderCompare(){
  const group = currentGroup(); const effect = currentEffect();
  const empty = $('emptyState'), slider = $('sliderStage'), side = $('sideStage');
  if(!group || !effect){ empty.classList.remove('hidden'); slider.classList.add('hidden'); side.classList.add('hidden'); return; }
  const rawSrc = getRawSrc(group); const effSrc = getEffectSrc(effect);
  if(!rawSrc && !effSrc){ empty.classList.remove('hidden'); slider.classList.add('hidden'); side.classList.add('hidden'); return; }
  empty.classList.add('hidden');
  const effLabel = state.session.effect_dirs?.[state.effectIndex] || `效果图 ${state.effectIndex+1}`;

  if(state.compareMode === 'slider'){
    side.classList.add('hidden'); slider.classList.remove('hidden');
    slider.classList.add('ztrans');
    const raw = $('rawImage'), eff = $('effImage');
    raw.src = rawSrc || effSrc; eff.src = effSrc || rawSrc;
    eff.style.display = effSrc ? 'block' : 'none';
    const pct = state.sliderPct;
    eff.style.clipPath = `inset(0 0 0 ${pct}%)`;
    $('sliderHandle').style.left = pct + '%';
    $('sliderHandle').style.display = effSrc ? 'block' : 'none';
    slider.querySelector('.side-tag.r').textContent = effLabel;
  } else {
    slider.classList.remove('hidden','ztrans'); slider.classList.add('hidden');
    side.classList.remove('hidden');
    $('sideEffLabel').textContent = effLabel;
    const rawPane = $('sideRawPane'), effPane = $('sideEffPane');
    rawPane.className = 'pane-img ztrans'; effPane.className = 'pane-img ztrans';
    rawPane.innerHTML = rawSrc ? `<img src="${rawSrc}">` : '<div class="missing-box">原图缺失</div>';
    effPane.innerHTML = effSrc ? `<img src="${effSrc}">` : '<div class="missing-box">效果图缺失</div>';
  }
  applyZoom();
  applyBorder(effect.label);
}
function renderAll(){ renderStats(); renderGroupTitle(); renderGroupNav(); renderThumbs(); renderCompare(); }

/* ---------- persistence ---------- */
async function persistView(){
  if(!state.session) return;
  await api('/api/view', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_index: state.groupIndex, effect_index: state.effectIndex }) });
}
function syncIndexFromSession(data, fallbackGroup){
  state.groupIndex = Math.min(fallbackGroup ?? (data.annotation.last_group_index || 0), Math.max(data.groups.length-1, 0));
  const eff = data.groups[state.groupIndex]?.effects?.length;
  state.effectIndex = eff ? Math.min(data.annotation.last_selected_effect_index || 0, eff-1) : 0;
}
async function loadServerSession(){
  const raw_dir = $('rawDir').value.trim();
  if(!raw_dir) throw new Error('请填写原图目录');
  const effect_dirs = getEffectDirs();
  const data = await api('/api/load', { method:'POST', body: JSON.stringify({ mode:'server-path', raw_dir, effect_dirs }) });
  state.session = data; syncIndexFromSession(data); resetZoom(); renderAll(); await loadRecent();
  toast(`已加载 ${data.groups.length} 组图片`, 'good');
}
function collectFiles(fileList, prefix){
  const files = [...fileList].filter(f => /\.(jpg|jpeg|png|webp|bmp|gif)$/i.test(f.name));
  files.sort((a,b)=>a.name.localeCompare(b.name));
  return files.map((f, idx)=>{ const key = `${prefix}:${idx}:${f.webkitRelativePath || f.name}`; state.localFiles.set(key, f); return { name:f.name, rel_path:f.webkitRelativePath || f.name, size:f.size, client_file_key:key }; });
}
async function loadLocalSession(){
  const rawInput = $('rawFolderInput');
  if(!rawInput.files?.length) throw new Error('请先选择原图文件夹');
  state.objUrls.forEach(u => { try{ URL.revokeObjectURL(u); }catch(_){} });
  state.objUrls.clear(); state.localFiles.clear();
  const raw_files = collectFiles(rawInput.files, 'raw');
  const effect_dirs = [];
  const localInputs = [...document.querySelectorAll('#localEffectInputs input[type=file]')];
  localInputs.forEach((input, idx)=>{ effect_dirs.push({ name: input.files?.[0]?.webkitRelativePath?.split('/')[0] || `effect-${idx+1}`, files: collectFiles(input.files || [], `effect-${idx}`) }); });
  let imported_annotation = null;
  const imp = $('importAnnotationInput').files?.[0];
  if(imp) imported_annotation = JSON.parse(await imp.text());
  const data = await api('/api/load', { method:'POST', body: JSON.stringify({ mode:'browser-local', dataset_name: rawInput.files[0].webkitRelativePath?.split('/')[0] || 'browser-local', raw_files, effect_dirs, imported_annotation }) });
  state.session = data; syncIndexFromSession(data); resetZoom(); renderAll(); await loadRecent();
  toast(`已加载 ${data.groups.length} 组本地图片`, 'good');
}
async function mark(label){
  const g = currentGroup(); const e = currentEffect(); if(!g || !e) return;
  const data = await api('/api/mark', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_name:g.name, effect_index:state.effectIndex, label, group_index:state.groupIndex }) });
  state.session = data; renderAll();
  toast(label==='qualified' ? '已标记为合格' : '已标记为不合格', label==='qualified' ? 'good' : 'bad');
}
async function commitCurrentGroup(){
  const group = currentGroup();
  if(group && group.effects.length > 1){
    const anyMarked = group.effects.some(x => x.label==='qualified' || x.label==='unqualified');
    if(anyMarked && !group.finalized){
      state.session = await api('/api/finalize', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_name: group.name, selected_index: state.effectIndex, group_index: state.groupIndex }) });
    }
  }
}
async function reloadSession(){
  const payload = state.session.mode === 'browser-local'
    ? { mode:'browser-local', dataset_id: state.session.dataset_id }
    : { mode:'server-path', raw_dir: state.session.raw_dir, effect_dirs: state.session.effect_dirs };
  const data = await api('/api/reload', { method:'POST', body: JSON.stringify(payload) });
  state.session = data; syncIndexFromSession(data);
}
async function setGroupTo(idx){
  if(!state.session) return;
  await commitCurrentGroup();
  state.groupIndex = Math.max(0, Math.min(state.session.groups.length-1, idx));
  state.effectIndex = 0; resetZoom(); await persistView();
  await reloadSession(); renderAll();
}
async function goGroup(delta){ if(!state.session) return; await setGroupTo(state.groupIndex + delta); }
async function goNextTodo(){
  if(!state.session) return;
  const n = state.session.groups.length;
  for(let off=1; off<=n; off++){
    const i = (state.groupIndex + off) % n;
    if(!isGroupDone(state.session.groups[i])){ await setGroupTo(i); toast('已跳到下一个未完成', 'info'); return; }
  }
  toast('全部分组已完成', 'good');
}

/* ---------- export ---------- */
function downloadBlob(name, content, type){ const blob = new Blob([content], {type}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 1000); }
async function exportData(kind){
  if(!state.session){ toast('请先加载数据', 'info'); return; }
  const data = await api('/api/export', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir }) });
  if(kind==='qualified') downloadBlob(`${data.dataset_name || 'dataset'}-qualified.txt`, data.qualified_names.join('\n'), 'text/plain;charset=utf-8');
  else if(kind==='unqualified') downloadBlob(`${data.dataset_name || 'dataset'}-unqualified.txt`, data.unqualified_names.join('\n'), 'text/plain;charset=utf-8');
  else downloadBlob(`${data.dataset_name || 'dataset'}-summary.json`, JSON.stringify(data, null, 2), 'application/json;charset=utf-8');
  toast('已导出', 'good');
}

/* ---------- zoom & pan ---------- */
function setupZoomPan(){
  const wrap = $('stageWrap');
  wrap.addEventListener('wheel', (e)=>{
    if(!state.session) return;
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width/2;
    const cy = e.clientY - rect.top - rect.height/2;
    const factor = e.deltaY < 0 ? 1.12 : 1/1.12;
    const prev = state.zoom.scale;
    const next = Math.max(1, Math.min(8, prev*factor));
    if(next === prev) return;
    state.zoom.x = cx - (next/prev)*(cx - state.zoom.x);
    state.zoom.y = cy - (next/prev)*(cy - state.zoom.y);
    state.zoom.scale = next;
    if(next === 1){ state.zoom.x = 0; state.zoom.y = 0; }
    applyZoom();
  }, { passive:false });

  let panning = false, sx=0, sy=0, ox=0, oy=0, panEl=null;
  wrap.addEventListener('pointerdown', (e)=>{
    if(!state.session) return;
    if(e.target.id === 'sliderHandle') return; // handle has its own drag
    const t = e.target.closest('.ztrans'); if(!t) return;
    panning = true; panEl = t; sx = e.clientX; sy = e.clientY; ox = state.zoom.x; oy = state.zoom.y;
    t.classList.add('panning'); wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener('pointermove', (e)=>{
    if(!panning) return;
    state.zoom.x = ox + (e.clientX - sx); state.zoom.y = oy + (e.clientY - sy); applyZoom();
  });
  const endPan = ()=>{ if(panning){ panning=false; document.querySelectorAll('.panning').forEach(el=>el.classList.remove('panning')); } };
  wrap.addEventListener('pointerup', endPan);
  wrap.addEventListener('pointercancel', endPan);
  wrap.addEventListener('dblclick', ()=>{ if(state.session) resetZoom(); });
}

/* ---------- slider handle drag ---------- */
function setupSliderHandle(){
  const handle = $('sliderHandle'); const stage = $('sliderStage');
  let dragging = false;
  const move = (clientX)=>{
    const rect = stage.getBoundingClientRect();
    const pct = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
    state.sliderPct = pct; renderCompare();
  };
  handle.addEventListener('pointerdown', (e)=>{ e.stopPropagation(); dragging = true; handle.setPointerCapture(e.pointerId); });
  handle.addEventListener('pointermove', (e)=>{ if(dragging){ e.stopPropagation(); move(e.clientX); } });
  const end = (e)=>{ if(dragging){ dragging=false; try{ handle.releasePointerCapture(e.pointerId); }catch(_){} } };
  handle.addEventListener('pointerup', end);
  handle.addEventListener('pointercancel', end);
}

/* ---------- compare mode ---------- */
function setCompareMode(mode){
  state.compareMode = mode;
  $('modeSliderSeg').classList.toggle('active', mode==='slider');
  $('modeSideSeg').classList.toggle('active', mode==='side');
  resetZoom(); renderCompare();
}

/* ---------- keyboard ---------- */
window.addEventListener('keydown', async (e)=>{
  if(!$('helpModal').classList.contains('hidden') && (e.key==='Escape')){ $('helpModal').classList.add('hidden'); return; }
  const tag = document.activeElement?.tagName;
  const isTyping = ['INPUT','TEXTAREA'].includes(tag);
  if(isTyping) return;
  const k = e.key;
  if(k === '='){ e.preventDefault(); await mark('qualified'); }
  else if(k === '-'){ e.preventDefault(); await mark('unqualified'); }
  else if(k === 'ArrowRight' || k === 'ArrowDown'){ e.preventDefault(); await goGroup(1); }
  else if(k === 'ArrowLeft' || k === 'ArrowUp'){ e.preventDefault(); await goGroup(-1); }
  else if(k === 'n' || k === 'N'){ e.preventDefault(); await goNextTodo(); }
  else if(k === 's' || k === 'S'){ e.preventDefault(); setCompareMode(state.compareMode==='slider'?'side':'slider'); }
  else if(k === 'f' || k === 'F'){ e.preventDefault(); resetZoom(); }
  else if(k === '?'){ e.preventDefault(); $('helpModal').classList.toggle('hidden'); }
  else if(k === 'Escape'){ $('helpModal').classList.add('hidden'); }
  else if(k === ' ' || e.code === 'Space'){
    e.preventDefault();
    if(state.compareMode==='slider'){ state.sliderPct = state.sliderToggleRight ? 0 : 100; state.sliderToggleRight = !state.sliderToggleRight; renderCompare(); }
  }
  else if(k === 'Tab'){ e.preventDefault(); const g=currentGroup(); if(!g || !g.effects.length) return; state.effectIndex = (state.effectIndex+1) % g.effects.length; await persistView(); renderAll(); }
  else if(['1','2','3','4','5','6','7','8','9'].includes(k)){ const idx = Number(k)-1; const g=currentGroup(); if(g && idx < g.effects.length){ e.preventDefault(); state.effectIndex = idx; await persistView(); renderAll(); } }
}, true);

/* ---------- wiring ---------- */
$('modeServerBtn').onclick = ()=>setMode('server-path');
$('modeLocalBtn').onclick = ()=>setMode('browser-local');
$('addEffectBtn').onclick = ()=>{ const vals=getEffectDirs(); vals.push(''); renderEffectInputs(vals); };
$('addLocalEffectBtn').onclick = ()=> renderLocalEffectInputs(document.querySelectorAll('#localEffectInputs input[type=file]').length + 1);
$('loadBtn').onclick = ()=>loadServerSession().catch(err=>toast(err.message,'bad'));
$('loadLocalBtn').onclick = ()=>loadLocalSession().catch(err=>toast(err.message,'bad'));
$('reloadRecentBtn').onclick = ()=>loadRecent().catch(()=>{});
$('markGoodBtn').onclick = ()=>mark('qualified').catch(err=>toast(err.message,'bad'));
$('markBadBtn').onclick = ()=>mark('unqualified').catch(err=>toast(err.message,'bad'));
$('prevBtn').onclick = ()=>goGroup(-1).catch(err=>toast(err.message,'bad'));
$('nextBtn').onclick = ()=>goGroup(1).catch(err=>toast(err.message,'bad'));
$('nextTodoBtn').onclick = ()=>goNextTodo().catch(err=>toast(err.message,'bad'));
$('modeSliderSeg').onclick = ()=>setCompareMode('slider');
$('modeSideSeg').onclick = ()=>setCompareMode('side');
$('zoomInBtn').onclick = ()=>{ state.zoom.scale = Math.min(8, state.zoom.scale*1.25); applyZoom(); };
$('zoomOutBtn').onclick = ()=>{ state.zoom.scale = Math.max(1, state.zoom.scale/1.25); if(state.zoom.scale===1){ state.zoom.x=0; state.zoom.y=0; } applyZoom(); };
$('zoomResetBtn').onclick = resetZoom;
$('exportQualifiedBtn').onclick = ()=>exportData('qualified').catch(err=>toast(err.message,'bad'));
$('exportUnqualifiedBtn').onclick = ()=>exportData('unqualified').catch(err=>toast(err.message,'bad'));
$('exportSummaryBtn').onclick = ()=>exportData('summary').catch(err=>toast(err.message,'bad'));
$('helpBtn').onclick = ()=> $('helpModal').classList.remove('hidden');
$('helpClose').onclick = ()=> $('helpModal').classList.add('hidden');
$('helpModal').onclick = (e)=>{ if(e.target.id==='helpModal') $('helpModal').classList.add('hidden'); };
$('groupSearch').oninput = (e)=>{ state.search = e.target.value; renderGroupNav(); };
document.querySelectorAll('.chip[data-filter]').forEach(chip => chip.onclick = ()=>{
  state.filter = chip.dataset.filter;
  document.querySelectorAll('.chip[data-filter]').forEach(c=>c.classList.toggle('active', c===chip));
  renderGroupNav();
});

setupZoomPan(); setupSliderHandle();
renderEffectInputs(['']); renderLocalEffectInputs(1); loadRecent().catch(()=>{});
</script>
</body></html>
'''


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._html(INDEX_HTML)
        if parsed.path == "/api/recent":
            return self._json({"items": self.server.app.load_recent()})
        if parsed.path == "/favicon.svg":
            icon_path = app_root() / "favicon.svg"
            if icon_path.exists():
                data = icon_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json({"error": "favicon not found"}, 404)
        if parsed.path == "/api/file":
            qs = parse_qs(parsed.query)
            path_value = qs.get("path", [""])[0]
            path = Path(unquote(path_value))
            if not path.exists() or not path.is_file():
                return self._json({"error": "file not found"}, 404)
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        data = self._read_json()
        try:
            if parsed.path == "/api/load":
                mode = data.get("mode", "server-path")
                if mode == "browser-local":
                    session = build_local_session(data)
                    self.server.app.save_recent_local(session)
                    return self._json(session)
                session = build_path_session(data["raw_dir"], data.get("effect_dirs", []))
                self.server.app.save_recent_path(session["raw_dir"], session["effect_dirs"])
                return self._json(session)
            if parsed.path == "/api/reload":
                mode = data.get("mode", "server-path")
                if mode == "browser-local":
                    path = dataset_state_path(data["dataset_id"])
                    with open(path, "r", encoding="utf-8") as f:
                        ann = json.load(f)
                    meta = ann.get("meta", {})
                    return self._json(build_local_session({
                        "dataset_name": ann.get("dataset_name"),
                        "raw_files": meta.get("raw_files", []),
                        "effect_dirs": meta.get("effect_dirs_payload", []),
                        "imported_annotation": ann,
                    }))
                return self._json(build_path_session(data["raw_dir"], data.get("effect_dirs", [])))
            if parsed.path in {"/api/mark", "/api/finalize", "/api/view", "/api/export"}:
                mode = data.get("mode", "server-path")
                dataset_key = data.get("dataset_key")
                ann, _ = load_annotation_by_mode(mode, dataset_key)
                if parsed.path == "/api/mark":
                    rec = ann["records"].setdefault(data["group_name"], {"effects": {}, "finalized": False})
                    rec["effects"][str(int(data["effect_index"]))] = data["label"]
                    ann["last_group_index"] = int(data.get("group_index", 0))
                    ann["last_selected_effect_index"] = int(data["effect_index"])
                    save_annotation_by_mode(mode, dataset_key, ann)
                elif parsed.path == "/api/finalize":
                    rec = ann["records"].setdefault(data["group_name"], {"effects": {}, "finalized": False})
                    chosen = str(int(data["selected_index"]))
                    chosen_label = rec["effects"].get(chosen)
                    if chosen_label in ("qualified", "unqualified"):
                        total_cols = len(ann.get("effect_dirs", []) or ann.get("meta", {}).get("effect_dir_names", []))
                        target = "unqualified" if chosen_label == "qualified" else "qualified"
                        for i in range(total_cols):
                            key = str(i)
                            if key not in rec["effects"]:
                                rec["effects"][key] = target
                        rec["finalized"] = True
                    ann["last_group_index"] = int(data.get("group_index", 0)) + 1
                    ann["last_selected_effect_index"] = 0
                    save_annotation_by_mode(mode, dataset_key, ann)
                elif parsed.path == "/api/view":
                    ann["last_group_index"] = int(data.get("group_index", 0))
                    ann["last_selected_effect_index"] = int(data.get("effect_index", 0))
                    save_annotation_by_mode(mode, dataset_key, ann)
                    return self._json({"ok": True})
                elif parsed.path == "/api/export":
                    return self._json(export_payload(ann))

                if mode == "browser-local":
                    meta = ann.get("meta", {})
                    return self._json(build_local_session({
                        "dataset_name": ann.get("dataset_name"),
                        "raw_files": meta.get("raw_files", []),
                        "effect_dirs": meta.get("effect_dirs_payload", []),
                        "imported_annotation": ann,
                    }))
                return self._json(build_path_session(dataset_key, ann.get("effect_dirs", [])))
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)


class App:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / APP_STATE_FILE

    def load_recent(self):
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8")).get("items", [])
            except Exception:
                return []
        return []

    def save_recent(self, item):
        items = [x for x in self.load_recent() if x.get("key") != item.get("key")]
        items.insert(0, item)
        self.state_path.write_text(json.dumps({"items": items[:10]}, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_recent_path(self, raw_dir, effect_dirs):
        self.save_recent({"key": f"server:{raw_dir}", "mode": "server-path", "label": f"{raw_dir} | {len(effect_dirs)}列", "raw_dir": raw_dir, "effect_dirs": effect_dirs})

    def save_recent_local(self, session):
        self.save_recent({"key": f"local:{session['dataset_id']}", "mode": "browser-local", "label": f"{session['annotation'].get('dataset_name')} | {len(session.get('effect_dirs', []))}列"})


def maybe_open_browser(port: int):
    if not AUTO_OPEN_BROWSER:
        return

    def _open():
        time.sleep(1.0)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main():
    root = app_root()
    app = App(root)
    port = find_available_port(DEFAULT_PORT)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.app = app
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} is busy, switched to {port}")
    print(f"Server running: http://0.0.0.0:{port}")
    maybe_open_browser(port)
    server.serve_forever()


if __name__ == "__main__":
    main()
