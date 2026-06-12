"""
DocConverter FastAPI 主服务 - 批量版
"""
import os, json, logging, threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import get_config, reload_config, config_to_json
from scanner import scan_directory, get_statistics
from batch import start_batch, stop_batch, get_batch_status, clear_batch
from watcher import start_watcher, stop_watcher, get_watcher_status
from weknora_client import WeKnoraClient
from converters import registry
from converters.ofd import OFDConverter
from converters.wps import WPSConverter
from converters.docx_converter import DOCXConverter
from converters.xlsx_converter import XLSXConverter
from converters.et_converter import ETConverter
from converters.pdf_converter import PDFConverter
from converters.htm_converter import HTMConverter
from converters.pptx_converter import PPTXConverter
from converters.placeholder_converter import PlaceholderConverter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("docconverter")

# ── 注册转换器 ──
registry.register(OFDConverter())
registry.register(WPSConverter())
registry.register(DOCXConverter())
registry.register(XLSXConverter())
registry.register(ETConverter())
registry.register(PDFConverter())
registry.register(HTMConverter())
registry.register(PPTXConverter())
registry.register(PlaceholderConverter("PPT(旧版)", ['.ppt'], ""))
registry.register(PlaceholderConverter("DPS(WPS演示)", ['.dps'], ""))
registry.register(PlaceholderConverter("PDF(图片型)", ['.pdf'], ""))

# ── 应用 ──
app = FastAPI(title="DocConverter", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== API 端点 ====================


@app.get("/api/health")
def health():
    config = get_config()
    wk = WeKnoraClient(config)
    return {
        "status": "ok",
        "version": "2.0.0",
        "formats": len(registry.list_supported_extensions()),
        "weknora_configured": wk.is_configured(),
        "watcher_running": get_watcher_status()["running"],
    }


@app.get("/api/formats")
def list_formats():
    return JSONResponse({"formats": registry.get_all_formats()})


# ── 单文件转换 ──


@app.post("/api/convert")
async def convert_file(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if not ext:
        raise HTTPException(400, "无法识别文件扩展名")

    converter = registry.get(ext)
    if not converter:
        raise HTTPException(400, f"不支持的格式: {ext}")

    config = get_config()
    upload_dir = Path(config["converter"]["upload_dir"])
    upload_dir.mkdir(parents=True, exist_ok=True)

    temp_path = upload_dir / f"upload_{os.urandom(4).hex()}{ext}"
    content = await file.read()
    temp_path.write_bytes(content)

    try:
        result = converter.convert(str(temp_path))
        return JSONResponse({
            "filename": file.filename,
            "format": ext,
            "converter": converter.display_name,
            "length": len(result),
            "markdown": result,
        })
    except Exception as e:
        raise HTTPException(500, f"转换失败: {str(e)}")
    finally:
        if not config["converter"]["keep_uploaded"] and temp_path.exists():
            temp_path.unlink()


# ── 目录扫描 ──


@app.post("/api/scan")
def scan_source_dir(source_dir: str = Query(None)):
    """扫描源目录，返回可转换的文件列表"""
    config = get_config()
    source = source_dir or config["scanner"]["source_dir"]

    files = scan_directory(source_dir=source)
    stats = get_statistics(files)

    return JSONResponse({
        "source_dir": source,
        "files_found": len(files),
        "statistics": stats,
        "files": [f.to_dict() for f in files],
    })


# ── 批量转换 ──


@app.post("/api/batch/start")
def start_batch_conversion():
    """启动批处理转换"""
    config = get_config()
    files = scan_directory()
    if not files:
        raise HTTPException(400, "源目录没有可转换的文件")

    # 后台异步执行
    def run_batch():
        wk = WeKnoraClient()

        def progress_callback(completed, total, task):
            # 如果启用了 WeKnora 推送，在转换完成后立即推送
            if wk.is_configured() and wk.auto_push:
                if task.status == "success" and task.markdown:
                    wk.push_manual(
                        title=Path(task.file_info.path).stem,
                        content=task.markdown,
                        source_path=str(task.file_info.path),
                        source_format=task.converter or "",
                    )

        start_batch(files, progress_callback=progress_callback)

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()

    return {"status": "started", "total_files": len(files)}


@app.get("/api/batch/status")
def batch_status():
    return get_batch_status()


@app.post("/api/batch/stop")
def batch_stop():
    stop_batch()
    return {"status": "stopping"}


@app.post("/api/batch/clear")
def batch_clear():
    clear_batch()
    return {"status": "cleared"}


# ── 文件监听 ──


@app.get("/api/watcher/status")
def watcher_status():
    return get_watcher_status()


@app.post("/api/watcher/start")
def watcher_start():
    wk = WeKnoraClient()

    def on_new_files(files):
        """新文件出现时自动启动批量转换"""
        logger.info(f"监控触发: {len(files)} 个新文件")

        def run_auto():
            def progress(completed, total, task):
                if wk.is_configured() and wk.auto_push and task.status == "success" and task.markdown:
                    wk.push_manual(
                        title=Path(task.file_info.path).stem,
                        content=task.markdown,
                        source_path=str(task.file_info.path),
                        source_format=task.converter or "",
                    )
            start_batch(files, progress_callback=progress)

        threading.Thread(target=run_auto, daemon=True).start()

    result = start_watcher(on_new_files=on_new_files)
    return result


@app.post("/api/watcher/stop")
def watcher_stop():
    return stop_watcher()


# ── WeKnora ──


@app.post("/api/weknora/test")
def weknora_test():
    """测试 WeKnora 连接"""
    client = WeKnoraClient()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置 (设置 WEKNORA_URL 环境变量)"}
    return client.test_connection()


@app.get("/api/weknora/status")
def weknora_status():
    client = WeKnoraClient()
    return {
        "configured": client.is_configured(),
        "url": client.api_url if client.api_url else "",
        "auto_push": client.auto_push,
        "dataset_id": client.dataset_id,
    }


# ── 配置 ──


@app.get("/api/config")
def get_config_api():
    return get_config()


@app.post("/api/config/reload")
def reload_config_api():
    cfg = reload_config()
    return {"status": "reloaded"}


# ── 输出文件 ──


@app.get("/api/output")
def list_output(path: str = Query("")):
    """列出输出目录的文件"""
    config = get_config()
    output_dir = Path(config["converter"]["output_dir"])
    target = output_dir / path if path else output_dir

    if not target.exists() or not str(target.resolve()).startswith(str(output_dir.resolve())):
        raise HTTPException(404, "路径不存在")

    if target.is_dir():
        items = []
        for p in sorted(target.iterdir()):
            items.append({
                "name": p.name,
                "path": str(p.relative_to(output_dir)),
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return {"path": path or "/", "items": items, "is_dir": True}
    else:
        content = target.read_text(encoding="utf-8")
        return {
            "path": str(target.relative_to(output_dir)),
            "is_dir": False,
            "size": target.stat().st_size,
            "content": content,
        }


# ── Web UI ──


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(UI_HTML)


# ==================== UI ====================

UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DocConverter 批量文档转换</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#0f172a; color:#e2e8f0; min-height:100vh; }
.nav { background:#1e293b; border-bottom:1px solid #334155; display:flex;
       padding:0 20px; position:sticky; top:0; z-index:100; overflow-x:auto; }
.nav a { padding:14px 20px; color:#94a3b8; text-decoration:none; font-size:14px;
         border-bottom:2px solid transparent; white-space:nowrap; cursor:pointer; }
.nav a:hover { color:#e2e8f0; }
.nav a.active { color:#38bdf8; border-bottom-color:#38bdf8; }
.container { max-width:1200px; margin:0 auto; padding:20px; }
.tab { display:none; }
.tab.active { display:block; }
h1 { font-size:22px; margin-bottom:16px; color:#f1f5f9; }
.card { background:#1e293b; border-radius:10px; border:1px solid #334155;
        padding:16px; margin-bottom:16px; }
.card-title { font-size:15px; font-weight:600; color:#94a3b8; margin-bottom:12px;
              text-transform:uppercase; letter-spacing:0.5px; }
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
.stat { text-align:center; padding:12px; background:#0f172a; border-radius:8px; }
.stat-value { font-size:24px; font-weight:700; color:#38bdf8; }
.stat-label { font-size:12px; color:#64748b; margin-top:4px; }
.btn { display:inline-flex; align-items:center; gap:6px; padding:8px 18px;
       border:none; border-radius:6px; font-size:14px; cursor:pointer;
       transition:all .15s; font-weight:500; }
.btn-primary { background:#2563eb; color:white; }
.btn-primary:hover { background:#1d4ed8; }
.btn-primary:disabled { opacity:.5; cursor:not-allowed; }
.btn-danger { background:#dc2626; color:white; }
.btn-danger:hover { background:#b91c1c; }
.btn-success { background:#16a34a; color:white; }
.btn-success:hover { background:#15803d; }
.btn-outline { background:transparent; border:1px solid #334155; color:#94a3b8; }
.btn-outline:hover { border-color:#38bdf8; color:#38bdf8; }
.btn-group { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:10px 12px; color:#94a3b8; font-weight:500;
     border-bottom:1px solid #334155; font-size:12px; text-transform:uppercase; }
td { padding:10px 12px; border-bottom:1px solid #1e293b; }
tr:hover td { background:#1e293b; }
.status-pending { color:#facc15; }
.status-running { color:#38bdf8; }
.status-success { color:#22c55e; }
.status-failed { color:#ef4444; }
.status-skipped { color:#64748b; }
.progress-bar { height:6px; background:#1e293b; border-radius:3px; overflow:hidden;
                margin:8px 0; }
.progress-fill { height:100%; background:linear-gradient(90deg,#2563eb,#38bdf8);
                 border-radius:3px; transition:width .3s; }
.upload-box { border:2px dashed #334155; border-radius:10px; padding:40px 20px;
              text-align:center; cursor:pointer; transition:all .2s; margin-bottom:16px; }
.upload-box:hover { border-color:#38bdf8; background:rgba(56,189,248,.05); }
.upload-box.dragover { border-color:#38bdf8; background:rgba(56,189,248,.1); }
.upload-icon { font-size:40px; margin-bottom:8px; }
pre { background:#0f172a; padding:14px; border-radius:6px; overflow-x:auto;
      font-size:13px; line-height:1.5; max-height:500px; overflow-y:auto; }
.formats-tags { display:flex; flex-wrap:wrap; gap:6px; }
.tag { padding:3px 10px; border-radius:12px; font-size:12px;
       border:1px solid #334155; color:#94a3b8; }
.tag-ready { background:rgba(34,197,94,.1); border-color:#22c55e; color:#22c55e; }
.tag-pending { background:rgba(250,204,21,.1); border-color:#facc15; color:#facc15; }
.config-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; }
.config-item { padding:10px; background:#0f172a; border-radius:6px; }
.config-key { font-size:11px; color:#64748b; font-family:monospace; }
.config-val { font-size:13px; margin-top:2px; word-break:break-all; }
#result { display:none; margin-top:16px; }
#error { display:none; color:#ef4444; padding:12px; background:rgba(239,68,68,.1);
         border-radius:6px; margin-top:12px; }
#spinner { display:none; text-align:center; padding:20px; }
@keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.spinner { animation:spin 1s linear infinite; display:inline-block; font-size:32px; }
.meta { display:flex; gap:16px; color:#94a3b8; font-size:13px; margin-bottom:12px; }
.footer { text-align:center; padding:20px; color:#475569; font-size:12px; }
</style>
</head>
<body>

<div class="nav" id="nav">
  <a class="active" onclick="switchTab('convert')">📄 转换</a>
  <a onclick="switchTab('batch')">📦 批量</a>
  <a onclick="switchTab('files')">📁 文件</a>
  <a onclick="switchTab('watch')">👁 监听</a>
  <a onclick="switchTab('config')">⚙ 配置</a>
  <a onclick="switchTab('weknora')">🔗 WeKnora</a>
</div>

<div class="container">

<!-- 单文件转换 -->
<div id="tab-convert" class="tab active">
  <h1>📄 文档格式转换</h1>
  <div class="upload-box" id="uploadBox" onclick="document.getElementById('fileInput').click()">
    <div class="upload-icon">📤</div>
    <div style="font-size:15px;color:#94a3b8">点击上传或拖拽文件</div>
    <div style="font-size:12px;color:#64748b;margin-top:6px">
      支持 OFD / WPS / DOCX / XLSX / ET / PDF / HTML / PPTX
    </div>
  </div>
  <input type="file" id="fileInput" onchange="uploadFile(this)" style="display:none">
  <div class="formats-tags" id="formatTags"></div>
  <div id="spinner"><div class="spinner">⏳</div><p style="color:#94a3b8;margin-top:8px">转换中...</p></div>
  <div id="result">
    <div class="card">
      <div class="meta" id="fileMeta"></div>
      <pre id="markdownOutput"></pre>
    </div>
    <button class="btn btn-primary" onclick="copyResult()">📋 复制结果</button>
  </div>
  <div id="error"></div>
</div>

<!-- 批量转换 -->
<div id="tab-batch" class="tab">
  <h1>📦 批量转换</h1>
  <div class="card">
    <div class="card-title">状态</div>
    <div class="stat-grid">
      <div class="stat"><div class="stat-value" id="batchTotal">0</div><div class="stat-label">总计</div></div>
      <div class="stat"><div class="stat-value" id="batchDone" style="color:#22c55e">0</div><div class="stat-label">已完成</div></div>
      <div class="stat"><div class="stat-value" id="batchSuccess" style="color:#22c55e">0</div><div class="stat-label">成功</div></div>
      <div class="stat"><div class="stat-value" id="batchFailed" style="color:#ef4444">0</div><div class="stat-label">失败</div></div>
    </div>
    <div class="progress-bar" style="margin-top:12px">
      <div class="progress-fill" id="batchProgress" style="width:0%"></div>
    </div>
    <div style="margin-top:12px" class="btn-group">
      <button class="btn btn-primary" onclick="startBatch()" id="btnBatchStart">▶ 开始批量</button>
      <button class="btn btn-danger" onclick="stopBatch()">⏹ 停止</button>
      <button class="btn btn-outline" onclick="scanFiles()">📋 扫描文件</button>
      <button class="btn btn-outline" onclick="clearBatch()">🗑 清除</button>
    </div>
    <div id="scanResult"></div>
  </div>
  <div class="card">
    <div class="card-title">任务列表</div>
    <div id="batchTasks"><p style="color:#64748b;padding:10px">暂无任务</p></div>
  </div>
</div>

<!-- 文件浏览 -->
<div id="tab-files" class="tab">
  <h1>📁 输出文件</h1>
  <div class="card">
    <div id="outputFiles"><p style="color:#64748b">加载中...</p></div>
  </div>
</div>

<!-- 监听 -->
<div id="tab-watch" class="tab">
  <h1>👁 文件监听</h1>
  <div class="card">
    <div class="stat-grid" id="watchStats">
      <div class="stat"><div class="stat-value" id="watchStatus">停止</div><div class="stat-label">状态</div></div>
      <div class="stat"><div class="stat-value" id="watchEvents">0</div><div class="stat-label">已处理事件</div></div>
    </div>
    <div class="btn-group" style="margin-top:12px">
      <button class="btn btn-success" onclick="startWatcher()">▶ 启动监听</button>
      <button class="btn btn-danger" onclick="stopWatcher()">⏹ 停止监听</button>
    </div>
  </div>
</div>

<!-- 配置 -->
<div id="tab-config" class="tab">
  <h1>⚙ 系统配置</h1>
  <div class="card">
    <div class="btn-group">
      <button class="btn btn-primary" onclick="reloadConfig()">🔄 重新加载</button>
    </div>
    <pre id="configDisplay">加载中...</pre>
  </div>
</div>

<!-- WeKnora -->
<div id="tab-weknora" class="tab">
  <h1>🔗 WeKnora 知识库</h1>
  <div class="card">
    <div class="stat-grid" id="weknoraStatus">
      <div class="stat"><div class="stat-value" id="wkConfigured" style="color:#ef4444">否</div><div class="stat-label">已配置</div></div>
      <div class="stat"><div class="stat-value" id="wkAutoPush" style="color:#64748b">-</div><div class="stat-label">自动推送</div></div>
    </div>
    <div class="btn-group" style="margin-top:12px">
      <button class="btn btn-primary" onclick="testWeKnora()">🔌 测试连接</button>
    </div>
    <div id="wkTestResult"></div>
  </div>
</div>

<div class="footer">DocConverter v2.0.0 | 文档转 Markdown → WeKnora 知识库</div>
</div>

<script>
// ── Tab 切换 ──
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelector('.nav a[onclick*="'+name+'"]').classList.add('active');
}

// ── 单文件转换 ──
const uploadBox = document.getElementById('uploadBox');
uploadBox.addEventListener('dragover', e => { e.preventDefault(); uploadBox.classList.add('dragover'); });
uploadBox.addEventListener('dragleave', () => uploadBox.classList.remove('dragover'));
uploadBox.addEventListener('drop', e => {
  e.preventDefault(); uploadBox.classList.remove('dragover');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

async function uploadFile(input) {
  if (!input.files.length) return;
  handleFile(input.files[0]);
}

async function handleFile(file) {
  const form = new FormData();
  form.append('file', file);
  show('spinner'); hide('result'); hide('error');
  try {
    const r = await fetch('/api/convert', { method:'POST', body:form });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || '转换失败'); }
    const data = await r.json();
    hide('spinner');
    document.getElementById('fileMeta').innerHTML =
      '<span>📄 '+file.name+'</span><span>🔧 '+data.converter+'</span><span>📏 '+data.length+' 字符</span>';
    document.getElementById('markdownOutput').textContent = data.markdown;
    show('result');
  } catch(e) {
    hide('spinner');
    document.getElementById('error').textContent = '❌ ' + e.message;
    show('error');
  }
}

async function copyResult() {
  const text = document.getElementById('markdownOutput').textContent;
  await navigator.clipboard.writeText(text);
  const btn = document.querySelector('#result .btn-primary');
  btn.textContent = '✅ 已复制';
  setTimeout(() => btn.textContent = '📋 复制结果', 2000);
}

// ── 格式标签 ──
fetch('/api/formats').then(r=>r.json()).then(d => {
  const tags = document.getElementById('formatTags');
  for (const f of d.formats) {
    const s = document.createElement('span');
    s.className = 'tag ' + (f.status === 'available' ? 'tag-ready' : 'tag-pending');
    s.textContent = f.name + ' (' + f.extensions.join(',') + ')';
    tags.appendChild(s);
  }
});

// ── 扫描文件 ──
async function scanFiles() {
  const result = document.getElementById('scanResult');
  result.innerHTML = '<p style="color:#94a3b8">扫描中...</p>';
  try {
    const r = await fetch('/api/scan', { method:'POST' });
    const d = await r.json();
    result.innerHTML = '<p style="color:#22c55e;margin-bottom:8px">✅ 找到 ' + d.files_found + ' 个文件' +
      ' (' + d.statistics.total_size_str + ')</p>';
    if (d.files_found > 0) {
      let html = '<table><tr><th>文件</th><th>格式</th><th>大小</th></tr>';
      for (const f of d.files) {
        html += '<tr><td>' + f.rel_path + '</td><td>' + f.ext + '</td><td>' + f.size_str + '</td></tr>';
      }
      html += '</table>';
      result.innerHTML += html;
    }
  } catch(e) {
    result.innerHTML = '<p style="color:#ef4444">❌ ' + e.message + '</p>';
  }
}

// ── 批量 ──
async function startBatch() {
  document.getElementById('btnBatchStart').disabled = true;
  document.getElementById('btnBatchStart').textContent = '⏳ 运行中...';
  try {
    await fetch('/api/batch/start', { method:'POST' });
    pollBatchStatus();
  } catch(e) { console.error(e); }
}

function pollBatchStatus() {
  const interval = setInterval(async () => {
    try {
      const r = await fetch('/api/batch/status');
      const d = await r.json();
      updateBatchUI(d);
      if (d.status === 'done' || d.status === 'idle') {
        clearInterval(interval);
        document.getElementById('btnBatchStart').disabled = false;
        document.getElementById('btnBatchStart').textContent = '▶ 开始批量';
      }
    } catch(e) { clearInterval(interval); }
  }, 1000);
}

function updateBatchUI(d) {
  const s = d.statistics || {};
  document.getElementById('batchTotal').textContent = s.total || 0;
  document.getElementById('batchDone').textContent = s.completed || 0;
  document.getElementById('batchSuccess').textContent = s.success || 0;
  document.getElementById('batchFailed').textContent = s.failed || 0;
  const pct = s.total > 0 ? Math.round((s.completed||0)/s.total*100) : 0;
  document.getElementById('batchProgress').style.width = pct + '%';

  if (d.tasks && d.tasks.length > 0) {
    let html = '<table><tr><th>文件</th><th>格式</th><th>状态</th><th>结果</th></tr>';
    for (const t of d.tasks.slice(0,100)) {
      const cls = 'status-' + t.status;
      html += '<tr><td>' + t.rel_path + '</td><td>' + t.ext + '</td>' +
        '<td class="'+cls+'">' + t.status + '</td>' +
        '<td>' + (t.result_length ? t.result_length+'字' : t.error || '') + '</td></tr>';
    }
    html += '</table>';
    document.getElementById('batchTasks').innerHTML = html;
  }
}

async function stopBatch() { await fetch('/api/batch/stop', { method:'POST' }); }
async function clearBatch() { await fetch('/api/batch/clear', { method:'POST' }); pollBatchStatus(); }

// ── 监听 ──
async function startWatcher() {
  await fetch('/api/watcher/start', { method:'POST' });
  updateWatcherStatus();
}
async function stopWatcher() {
  await fetch('/api/watcher/stop', { method:'POST' });
  updateWatcherStatus();
}
async function updateWatcherStatus() {
  const r = await fetch('/api/watcher/status');
  const d = await r.json();
  document.getElementById('watchStatus').textContent = d.running ? '运行中' : '停止';
  document.getElementById('watchStatus').style.color = d.running ? '#22c55e' : '#ef4444';
  document.getElementById('watchEvents').textContent = d.events_processed || 0;
}

// ── 配置 ──
async function reloadConfig() {
  await fetch('/api/config/reload', { method:'POST' });
  const r = await fetch('/api/config');
  const d = await r.json();
  document.getElementById('configDisplay').textContent = JSON.stringify(d, null, 2);
}

// ── WeKnora ──
async function testWeKnora() {
  const result = document.getElementById('wkTestResult');
  result.innerHTML = '<p style="color:#94a3b8">测试中...</p>';
  try {
    const r = await fetch('/api/weknora/test', { method:'POST' });
    const d = await r.json();
    result.innerHTML = d.ok
      ? '<p style="color:#22c55e">✅ 连接成功!</p>'
      : '<p style="color:#ef4444">❌ ' + (d.error || '连接失败') + '</p>';
  } catch(e) {
    result.innerHTML = '<p style="color:#ef4444">❌ ' + e.message + '</p>';
  }
}

async function updateWeKnoraStatus() {
  const r = await fetch('/api/weknora/status');
  const d = await r.json();
  document.getElementById('wkConfigured').textContent = d.configured ? '是' : '否';
  document.getElementById('wkConfigured').style.color = d.configured ? '#22c55e' : '#ef4444';
  document.getElementById('wkAutoPush').textContent = d.auto_push ? '开启' : '关闭';
}

// ── 输出文件浏览 ──
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function loadOutputFiles(path) {
  path = path || '';
  const r = await fetch('/api/output?path=' + encodeURIComponent(path));
  const d = await r.json();
  if (d.is_dir) {
    const items = d.items.map(function(item) {
      if (item.is_dir) {
        return '<tr><td><a onclick="loadOutputFiles('+JSON.stringify(item.path)+')" style="color:#38bdf8;cursor:pointer">📁 ' + escHtml(item.name) + '</a></td><td>-</td></tr>';
      } else {
        return '<tr><td><a onclick="viewOutputFile('+JSON.stringify(item.path)+')" style="color:#e2e8f0;cursor:pointer">📄 ' + escHtml(item.name) + '</a></td><td>' + item.size + 'B</td></tr>';
      }
    }).join('');
    var html = '<div style="margin-bottom:8px"><button class="btn btn-outline" onclick="loadOutputFiles()">🏠 根目录</button></div>';
    if (path) html += '<p style="color:#94a3b8;margin-bottom:8px">📁 ' + escHtml(path) + '/</p>';
    html += '<table><tr><th>名称</th><th>大小</th></tr>' + items + '</table>';
    document.getElementById('outputFiles').innerHTML = html;
  }
}

async function viewOutputFile(path) {
  const r = await fetch('/api/output?path=' + encodeURIComponent(path));
  const d = await r.json();
  const html = '<div style="margin-bottom:8px"><button class="btn btn-outline" onclick="loadOutputFiles()">← 返回</button></div>' +
    '<p style="color:#94a3b8;margin-bottom:8px">📄 ' + escHtml(path) + ' (' + d.size + 'B)</p>' +
    '<pre>' + escHtml(d.content) + '</pre>';
  document.getElementById('outputFiles').innerHTML = html;
}

// ── 初始化 ──
function show(id) { document.getElementById(id).style.display = 'block'; }
function hide(id) { document.getElementById(id).style.display = 'none'; }

// 初始加载
reloadConfig();
updateWatcherStatus();
updateWeKnoraStatus();
loadOutputFiles();

// 定期刷新状态
setInterval(updateWatcherStatus, 5000);
setInterval(updateWeKnoraStatus, 10000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    config = get_config()
    uvicorn.run(
        "main:app",
        host=config["server"]["host"],
        port=config["server"]["port"],
        reload=False,
        log_level="info",
    )
