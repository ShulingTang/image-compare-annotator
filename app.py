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
:root { --bg:#111827; --panel:#1f2937; --text:#f9fafb; --muted:#9ca3af; --green:#22c55e; --red:#ef4444; --blue:#3b82f6; }
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
.app { display:flex; height:100vh; }
.sidebar { width:360px; background:#0f172a; padding:16px; border-right:1px solid #1e293b; overflow:auto; }
.main { flex:1; display:flex; flex-direction:column; min-width:0; }
.card { background:var(--panel); border:1px solid #334155; border-radius:12px; padding:12px; margin-bottom:12px; }
.title { font-size:18px; font-weight:700; }
.hint { color:var(--muted); font-size:12px; }
.row { display:flex; gap:8px; align-items:center; }
.col { display:flex; flex-direction:column; gap:8px; }
input[type="text"], input[type="number"] { width:100%; padding:10px 12px; border-radius:8px; background:#111827; color:var(--text); border:1px solid #475569; }
button { background:#2563eb; color:#fff; border:none; border-radius:8px; padding:10px 12px; cursor:pointer; }
button.secondary { background:#475569; }
button.good { background:var(--green); }
button.bad { background:var(--red); }
.toolbar { padding:12px 16px; border-bottom:1px solid #1e293b; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.viewer { flex:1; min-height:0; display:flex; flex-direction:column; padding:16px; gap:12px; overflow:hidden; }
.compare-wrap { flex:1; min-height:320px; background:#020617; border:3px solid #334155; border-radius:14px; overflow:auto; display:flex; align-items:center; justify-content:center; position:relative; resize:both; }
.compare-stage { position:relative; display:inline-block; max-width:100%; }
.compare-stage img { display:block; max-width:min(1600px, 78vw); max-height:68vh; object-fit:contain; user-select:none; }
.overlay { position:absolute; inset:0 auto 0 0; overflow:hidden; pointer-events:none; }
.overlay img { display:block; }
.slider { width:min(800px, 70vw); }
.thumbs { display:flex; gap:10px; overflow:auto; padding-bottom:4px; }
.thumb { min-width:180px; width:180px; background:#111827; border:2px solid #334155; border-radius:12px; padding:8px; cursor:pointer; }
.thumb.active { border-color:var(--blue); }
.thumb.good { border-color:var(--green); }
.thumb.bad { border-color:var(--red); }
.thumb img, .thumb .missing { width:100%; height:110px; object-fit:contain; background:#020617; border-radius:8px; display:flex; align-items:center; justify-content:center; color:var(--muted); }
.stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.stat { background:#111827; border-radius:10px; padding:10px; border:1px solid #334155; }
.stat .k { font-size:12px; color:var(--muted); }
.stat .v { font-size:18px; font-weight:700; margin-top:4px; }
.badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }
.badge.good { background:rgba(34,197,94,.16); color:#86efac; }
.badge.bad { background:rgba(239,68,68,.16); color:#fca5a5; }
.badge.none { background:rgba(148,163,184,.16); color:#cbd5e1; }
.hidden { display:none; }
.section-title { font-size:13px; color:#cbd5e1; font-weight:700; }
.mode-switch { display:flex; gap:8px; margin-top:8px; }
.mode-btn.active { outline:2px solid var(--blue); }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="card">
      <div class="title">图片对比标注工具</div>
      <div class="hint" style="margin-top:6px;">支持服务端目录模式 + 浏览器本地读图模式。</div>
      <div class="mode-switch">
        <button id="modeServerBtn" class="secondary mode-btn active">服务端目录</button>
        <button id="modeLocalBtn" class="secondary mode-btn">浏览器本地</button>
      </div>
    </div>
    <div class="card col" id="serverModePanel">
      <div class="section-title">服务端目录模式</div>
      <div>
        <label>原图目录</label>
        <input id="rawDir" type="text" placeholder="/path/to/raw" />
      </div>
      <div id="effectDirList" class="col"></div>
      <div class="row">
        <button id="addEffectBtn" class="secondary">+ 增加效果图目录</button>
        <button id="loadBtn">加载数据</button>
      </div>
    </div>
    <div class="card col hidden" id="localModePanel">
      <div class="section-title">浏览器本地模式</div>
      <div class="hint">图片不上传，浏览器按需读取；标注结果实时保存到服务端。</div>
      <div>
        <label>原图文件夹</label>
        <input id="rawFolderInput" type="file" webkitdirectory directory multiple />
      </div>
      <div id="localEffectInputs" class="col"></div>
      <div class="row">
        <button id="addLocalEffectBtn" class="secondary">+ 增加效果图文件夹</button>
        <button id="loadLocalBtn">加载本地数据</button>
      </div>
      <div>
        <label>可选：导入之前导出的结果 JSON</label>
        <input id="importAnnotationInput" type="file" accept="application/json,.json" />
      </div>
    </div>
    <div class="card">
      <div class="row" style="justify-content:space-between"><strong>最近使用记录</strong><button id="reloadRecentBtn" class="secondary">刷新</button></div>
      <div id="recentList" class="col" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <div class="row" style="justify-content:space-between"><strong>当前状态</strong><span id="saveStatus" class="hint">未保存</span></div>
      <div style="margin-top:10px" id="currentResult"></div>
    </div>
    <div class="card">
      <strong>统计</strong>
      <div class="stats" style="margin-top:10px">
        <div class="stat"><div class="k">合格率</div><div class="v" id="qualifiedRate">0%</div></div>
        <div class="stat"><div class="k">标注进度</div><div class="v" id="progressRate">0%</div></div>
        <div class="stat"><div class="k">已标列数</div><div class="v" id="labeledCount">0/0</div></div>
        <div class="stat"><div class="k">完成组数</div><div class="v" id="groupCount">0/0</div></div>
      </div>
    </div>
    <div class="card col">
      <div class="section-title">导出</div>
      <div class="row">
        <button id="exportQualifiedBtn" class="secondary">导出合格文件名</button>
        <button id="exportUnqualifiedBtn" class="secondary">导出不合格文件名</button>
      </div>
      <button id="exportSummaryBtn" class="secondary">导出汇总数据</button>
    </div>
    <div class="card hint">快捷键：= 合格 / - 不合格 / ←↑ 上一组 / →↓ 下一组 / 1,2,3 选列 / Tab 切列</div>
  </aside>
  <main class="main">
    <div class="toolbar">
      <button id="prevBtn" class="secondary">上一组</button>
      <button id="nextBtn" class="secondary">下一组</button>
      <button id="markGoodBtn" class="good">标合格 (=)</button>
      <button id="markBadBtn" class="bad">标不合格 (-)</button>
      <label class="row"><input id="overlayToggle" type="checkbox" checked /> 启用叠加对比</label>
      <input id="slider" class="slider" type="range" min="0" max="100" value="50" />
      <span id="groupTitle" class="hint">未加载</span>
    </div>
    <div class="viewer">
      <div id="compareWrap" class="compare-wrap">
        <div id="emptyState" class="hint">请先加载目录</div>
        <div id="compareStage" class="compare-stage hidden">
          <img id="rawImage" alt="raw" />
          <div id="overlay" class="overlay"><img id="effectImage" alt="effect" /></div>
        </div>
      </div>
      <div class="thumbs" id="thumbs"></div>
    </div>
  </main>
</div>
<script>
const state = { session:null, groupIndex:0, effectIndex:0, mode:'server-path', localFiles:new Map(), saveStatus:'未保存', sliderToggleRight:false };

function setSaveStatus(text){ document.getElementById('saveStatus').textContent = text; }
function setMode(mode){
  state.mode = mode;
  document.getElementById('serverModePanel').classList.toggle('hidden', mode !== 'server-path');
  document.getElementById('localModePanel').classList.toggle('hidden', mode !== 'browser-local');
  document.getElementById('modeServerBtn').classList.toggle('active', mode === 'server-path');
  document.getElementById('modeLocalBtn').classList.toggle('active', mode === 'browser-local');
}
function effectInput(idx, value='') {
  const wrap = document.createElement('div');
  wrap.className = 'row';
  wrap.innerHTML = `<input type="text" placeholder="效果图目录 ${idx+1}" value="${value}"><button class="secondary" data-remove="${idx}">删</button>`;
  return wrap;
}
function localEffectInput(idx) {
  const wrap = document.createElement('div');
  wrap.className = 'col';
  wrap.innerHTML = `<label>效果图文件夹 ${idx+1}</label><div class="row"><input type="file" data-local-effect="${idx}" webkitdirectory directory multiple /><button class="secondary" data-remove-local="${idx}">删</button></div>`;
  return wrap;
}
function renderEffectInputs(values=[]) {
  const box = document.getElementById('effectDirList'); box.innerHTML = '';
  const arr = values.length ? values : [''];
  arr.forEach((v, i) => box.appendChild(effectInput(i, v)));
  box.querySelectorAll('[data-remove]').forEach(btn => btn.onclick = () => { const vals = getEffectDirs(); vals.splice(Number(btn.dataset.remove), 1); renderEffectInputs(vals.length ? vals : ['']); });
}
function renderLocalEffectInputs(count=1){
  const box = document.getElementById('localEffectInputs'); box.innerHTML='';
  for(let i=0;i<count;i++) box.appendChild(localEffectInput(i));
  box.querySelectorAll('[data-remove-local]').forEach(btn => btn.onclick = () => renderLocalEffectInputs(Math.max(1,count-1)));
}
function getEffectDirs() { return [...document.querySelectorAll('#effectDirList input')].map(x => x.value.trim()).filter(Boolean); }
async function api(path, options={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...options });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || '请求失败');
  return data;
}
async function loadRecent(){
  const data = await api('/api/recent');
  const list = document.getElementById('recentList'); list.innerHTML='';
  (data.items || []).forEach(item => {
    const b = document.createElement('button');
    b.className = 'secondary'; b.style.textAlign='left';
    b.textContent = `${item.mode === 'browser-local' ? '[本地]' : '[目录]'} ${item.label}`;
    b.onclick = () => {
      if(item.mode === 'server-path'){
        setMode('server-path');
        document.getElementById('rawDir').value = item.raw_dir || '';
        renderEffectInputs(item.effect_dirs || ['']);
      }
    };
    list.appendChild(b);
  });
  if(!list.children.length) list.innerHTML = '<div class="hint">暂无记录</div>';
}
function badge(label){
  if(label==='qualified') return '<span class="badge good">合格</span>';
  if(label==='unqualified') return '<span class="badge bad">不合格</span>';
  return '<span class="badge none">未标注</span>';
}
function currentGroup(){ return state.session?.groups?.[state.groupIndex]; }
function currentEffect(){ return currentGroup()?.effects?.[state.effectIndex]; }
function renderStats(){
  const s = state.session?.stats; if(!s) return;
  document.getElementById('qualifiedRate').textContent = (s.qualified_rate*100).toFixed(1)+'%';
  document.getElementById('progressRate').textContent = (s.progress*100).toFixed(1)+'%';
  document.getElementById('labeledCount').textContent = `${s.labeled}/${s.total_effect_slots}`;
  document.getElementById('groupCount').textContent = `${s.completed_groups}/${s.total_groups}`;
}
function renderCurrent(){
  const group = currentGroup(); const effect = currentEffect();
  document.getElementById('groupTitle').textContent = group ? `组 ${state.groupIndex+1}/${state.session.groups.length}：${group.name} | 当前列 ${state.effectIndex+1}` : '未加载';
  const box = document.getElementById('currentResult');
  if(!group || !effect){ box.innerHTML = '<div class="hint">暂无数据</div>'; return; }
  box.innerHTML = `<div>模式：<strong>${state.session.mode === 'browser-local' ? '浏览器本地' : '服务端目录'}</strong></div><div style="margin-top:8px">当前组：<strong>${group.name}</strong></div><div style="margin-top:8px">当前列：第 ${state.effectIndex+1} 列</div><div style="margin-top:8px">结果：${badge(effect.label)}</div><div style="margin-top:8px">保存位置：<div class="hint">${state.session.annotation_path}</div></div>`;
}
function fileUrl(key){
  if(!key) return null;
  const file = state.localFiles.get(key);
  return file ? URL.createObjectURL(file) : null;
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
  const thumbs = document.getElementById('thumbs'); thumbs.innerHTML='';
  const group = currentGroup(); if(!group) return;
  group.effects.forEach((ef, idx) => {
    const src = getEffectSrc(ef);
    const div = document.createElement('div');
    div.className = `thumb ${idx===state.effectIndex?'active':''} ${ef.label==='qualified'?'good':''} ${ef.label==='unqualified'?'bad':''}`;
    div.innerHTML = `${src ? `<img src="${src}">` : `<div class="missing">缺失</div>`}<div style="margin-top:8px">列 ${idx+1}</div><div class="hint">${ef.display_name || '无对应文件'}</div><div style="margin-top:6px">${badge(ef.label)}</div>`;
    div.onclick = async () => { state.effectIndex = idx; await persistView(); renderAll(); };
    thumbs.appendChild(div);
  });
}
function applyBorder(label){
  const wrap = document.getElementById('compareWrap');
  wrap.style.borderColor = label === 'qualified' ? '#22c55e' : label === 'unqualified' ? '#ef4444' : '#334155';
}
function renderCompare(){
  const group = currentGroup(); const effect = currentEffect();
  const empty = document.getElementById('emptyState'); const stage = document.getElementById('compareStage');
  if(!group || !effect){ empty.classList.remove('hidden'); stage.classList.add('hidden'); return; }
  const rawSrc = getRawSrc(group); const effSrc = getEffectSrc(effect);
  if(!rawSrc){ empty.classList.remove('hidden'); stage.classList.add('hidden'); return; }
  empty.classList.add('hidden'); stage.classList.remove('hidden');
  const raw = document.getElementById('rawImage'); const eff = document.getElementById('effectImage');
  raw.src = rawSrc; eff.src = effSrc || rawSrc;
  const enabled = document.getElementById('overlayToggle').checked && !!effSrc;
  const pct = Number(document.getElementById('slider').value);
  const overlay = document.getElementById('overlay');
  overlay.style.width = enabled ? pct + '%' : '0%'; overlay.style.display = enabled ? 'block' : 'none';
  applyBorder(effect.label);
}
function renderAll(){ renderStats(); renderCurrent(); renderThumbs(); renderCompare(); }
async function persistView(){
  if(!state.session) return;
  setSaveStatus('保存中...');
  await api('/api/view', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_index: state.groupIndex, effect_index: state.effectIndex }) });
  setSaveStatus('已保存');
}
async function loadServerSession(){
  const raw_dir = document.getElementById('rawDir').value.trim();
  const effect_dirs = getEffectDirs();
  const data = await api('/api/load', { method:'POST', body: JSON.stringify({ mode:'server-path', raw_dir, effect_dirs }) });
  state.session = data; state.groupIndex = Math.min(data.annotation.last_group_index || 0, Math.max(data.groups.length-1,0)); state.effectIndex = data.groups[state.groupIndex]?.effects?.length ? Math.min(data.annotation.last_selected_effect_index || 0, data.groups[state.groupIndex].effects.length-1) : 0; setSaveStatus('已保存'); renderAll(); await loadRecent();
}
function collectFiles(fileList, prefix){
  const files = [...fileList].filter(f => /\.(jpg|jpeg|png|webp|bmp|gif)$/i.test(f.name));
  files.sort((a,b)=>a.name.localeCompare(b.name));
  return files.map((f, idx) => { const key = `${prefix}:${idx}:${f.webkitRelativePath || f.name}`; state.localFiles.set(key, f); return { name:f.name, rel_path:f.webkitRelativePath || f.name, size:f.size, client_file_key:key }; });
}
async function loadLocalSession(){
  const rawInput = document.getElementById('rawFolderInput');
  if(!rawInput.files?.length) throw new Error('请先选择原图文件夹');
  state.localFiles.clear();
  const raw_files = collectFiles(rawInput.files, 'raw');
  const effect_dirs = [];
  const localInputs = [...document.querySelectorAll('#localEffectInputs input[type=file]')];
  localInputs.forEach((input, idx) => { effect_dirs.push({ name: input.files?.[0]?.webkitRelativePath?.split('/')[0] || `effect-${idx+1}`, files: collectFiles(input.files || [], `effect-${idx}`) }); });
  let imported_annotation = null;
  const imp = document.getElementById('importAnnotationInput').files?.[0];
  if(imp) imported_annotation = JSON.parse(await imp.text());
  const data = await api('/api/load', { method:'POST', body: JSON.stringify({ mode:'browser-local', dataset_name: rawInput.files[0].webkitRelativePath?.split('/')[0] || 'browser-local', raw_files, effect_dirs, imported_annotation }) });
  state.session = data; state.groupIndex = Math.min(data.annotation.last_group_index || 0, Math.max(data.groups.length-1,0)); state.effectIndex = data.groups[state.groupIndex]?.effects?.length ? Math.min(data.annotation.last_selected_effect_index || 0, data.groups[state.groupIndex].effects.length-1) : 0; setSaveStatus('已保存'); renderAll(); await loadRecent();
}
async function mark(label){
  const g=currentGroup(); const e=currentEffect(); if(!g || !e) return;
  setSaveStatus('保存中...');
  const data = await api('/api/mark', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_name:g.name, effect_index:state.effectIndex, label, group_index:state.groupIndex }) });
  state.session = data; setSaveStatus('已保存'); renderAll();
}
async function goGroup(delta){
  if(!state.session) return;
  const group = currentGroup();
  if(group && group.effects.length > 1){
    const anyMarked = group.effects.some(x => x.label === 'qualified' || x.label === 'unqualified');
    if(anyMarked && !group.finalized){
      setSaveStatus('保存中...');
      state.session = await api('/api/finalize', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir, group_name: group.name, selected_index: state.effectIndex, group_index: state.groupIndex }) });
    }
  }
  state.groupIndex = Math.max(0, Math.min(state.session.groups.length - 1, state.groupIndex + delta)); state.effectIndex = 0; await persistView();
  const payload = state.session.mode === 'browser-local' ? { mode:'browser-local', dataset_id: state.session.dataset_id } : { mode:'server-path', raw_dir: state.session.raw_dir, effect_dirs: state.session.effect_dirs };
  const data = await api('/api/reload', { method:'POST', body: JSON.stringify(payload) });
  state.session = data; state.groupIndex = Math.min(data.annotation.last_group_index || state.groupIndex, Math.max(data.groups.length - 1, 0)); state.effectIndex = data.groups[state.groupIndex]?.effects?.length ? Math.min(data.annotation.last_selected_effect_index || 0, data.groups[state.groupIndex].effects.length - 1) : 0; setSaveStatus('已保存'); renderAll();
}
function downloadBlob(name, content, type){ const blob = new Blob([content], {type}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 1000); }
async function exportData(kind){
  if(!state.session) return;
  const data = await api('/api/export', { method:'POST', body: JSON.stringify({ mode: state.session.mode, dataset_key: state.session.mode === 'browser-local' ? state.session.dataset_id : state.session.raw_dir }) });
  if(kind==='qualified') downloadBlob(`${data.dataset_name || 'dataset'}-qualified.txt`, data.qualified_names.join('\n'), 'text/plain;charset=utf-8');
  else if(kind==='unqualified') downloadBlob(`${data.dataset_name || 'dataset'}-unqualified.txt`, data.unqualified_names.join('\n'), 'text/plain;charset=utf-8');
  else downloadBlob(`${data.dataset_name || 'dataset'}-summary.json`, JSON.stringify(data, null, 2), 'application/json;charset=utf-8');
}
window.addEventListener('keydown', async (e) => {
  const tag = document.activeElement?.tagName;
  const isTyping = ['INPUT','TEXTAREA'].includes(tag) && document.activeElement?.id !== 'slider';
  if(isTyping) return;
  if(e.key === '='){ e.preventDefault(); await mark('qualified'); }
  else if(e.key === '-'){ e.preventDefault(); await mark('unqualified'); }
  else if(e.key === 'ArrowRight' || e.key === 'ArrowDown'){ e.preventDefault(); document.body.focus?.(); await goGroup(1); }
  else if(e.key === 'ArrowLeft' || e.key === 'ArrowUp'){ e.preventDefault(); document.body.focus?.(); await goGroup(-1); }
  else if(e.key === ' ' || e.code === 'Space'){
    e.preventDefault();
    const slider = document.getElementById('slider');
    slider.value = state.sliderToggleRight ? 0 : 100;
    state.sliderToggleRight = !state.sliderToggleRight;
    renderCompare();
  }
  else if(e.key === 'Tab'){ e.preventDefault(); const g=currentGroup(); if(!g || !g.effects.length) return; state.effectIndex = (state.effectIndex + 1) % g.effects.length; await persistView(); renderAll(); }
  else if(['1','2','3'].includes(e.key)){ const idx = Number(e.key)-1; const g=currentGroup(); if(g && idx < g.effects.length){ e.preventDefault(); state.effectIndex = idx; await persistView(); renderAll(); } }
}, true);
document.getElementById('slider').addEventListener('keydown', (e)=>{
  if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown',' ','Spacebar','=','-'].includes(e.key) || e.code === 'Space') e.preventDefault();
});
document.getElementById('slider').addEventListener('pointerup', ()=>{ document.body.focus?.(); });
document.getElementById('slider').addEventListener('click', ()=>{ document.body.focus?.(); });
document.getElementById('modeServerBtn').onclick = ()=>setMode('server-path');
document.getElementById('modeLocalBtn').onclick = ()=>setMode('browser-local');
document.getElementById('addEffectBtn').onclick = ()=>{ const vals=getEffectDirs(); vals.push(''); renderEffectInputs(vals); };
document.getElementById('addLocalEffectBtn').onclick = ()=> renderLocalEffectInputs(document.querySelectorAll('#localEffectInputs input[type=file]').length + 1);
document.getElementById('loadBtn').onclick = ()=>loadServerSession().catch(err=>alert(err.message));
document.getElementById('loadLocalBtn').onclick = ()=>loadLocalSession().catch(err=>alert(err.message));
document.getElementById('reloadRecentBtn').onclick = loadRecent;
document.getElementById('markGoodBtn').onclick = ()=>mark('qualified');
document.getElementById('markBadBtn').onclick = ()=>mark('unqualified');
document.getElementById('prevBtn').onclick = ()=>goGroup(-1);
document.getElementById('nextBtn').onclick = ()=>goGroup(1);
document.getElementById('slider').oninput = () => { renderCompare(); };
document.getElementById('overlayToggle').onchange = renderCompare;
document.getElementById('exportQualifiedBtn').onclick = ()=>exportData('qualified');
document.getElementById('exportUnqualifiedBtn').onclick = ()=>exportData('unqualified');
document.getElementById('exportSummaryBtn').onclick = ()=>exportData('summary');
renderEffectInputs(['']); renderLocalEffectInputs(1); loadRecent(); setSaveStatus('未保存');
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
