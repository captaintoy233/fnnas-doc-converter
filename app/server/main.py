"""
DocConverter FastAPI 主服务 - 批量版
"""
import os, json, logging, threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import get_config, reload_config, config_to_json, save_config, APP_VERSION
from scanner import scan_directory, get_statistics
from batch import (start_batch, stop_batch, get_batch_status, clear_batch,
                   retry_failed_pushes, failed_push_count, shutdown_push_workers)
from watcher import start_watcher, stop_watcher, get_watcher_status
from weknora_client import WeKnoraClient
from converters import registry
from converters.loader import register_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("docconverter")

# ── 注册转换器 ──
register_all(registry, get_config())

# ── 应用 ──
app = FastAPI(title="DocConverter", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 优雅退出：停止 watcher + drain 推送队列（在途推送任务完成） ──
@app.on_event("shutdown")
def _on_shutdown():
    try:
        stop_watcher()
    except Exception:
        pass
    try:
        shutdown_push_workers(drain=True, timeout=30)
    except Exception:
        pass


# ── 可选 API 认证（server.auth_token 非空时启用）──
_PUBLIC_PATHS = {"/", "/api/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def auth_middleware(request, call_next):
    # CORS 预检（OPTIONS）直接放行，否则启用认证后跨域预检被 401 拦截
    if request.method == "OPTIONS":
        return await call_next(request)
    token = get_config().get("server", {}).get("auth_token", "")
    if token and request.url.path.startswith("/api/") \
            and request.url.path not in _PUBLIC_PATHS:
        provided = request.headers.get("Authorization", "")
        if provided == "Bearer {}".format(token) or \
                request.headers.get("X-Auth-Token", "") == token:
            return await call_next(request)
        from fastapi.responses import JSONResponse as _JR
        return _JR({"ok": False, "error": "未认证：需要 Authorization: Bearer <token>"},
                   status_code=401)
    return await call_next(request)

# 单例客户端（避免每次请求重新实例化）
_weknora_client = None


def get_weknora_client() -> WeKnoraClient:
    global _weknora_client
    if _weknora_client is None:
        _weknora_client = WeKnoraClient()
    return _weknora_client


# ==================== API 端点 ====================


@app.get("/api/health")
def health():
    config = get_config()
    wk = get_weknora_client()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "formats": len(registry.list_supported_extensions()),
        "weknora_configured": wk.is_configured(),
        "watcher_running": get_watcher_status()["running"],
    }


@app.get("/api/formats")
def list_formats():
    return JSONResponse({"formats": registry.get_all_formats()})


# ── 单文件转换 ──


@app.post("/api/convert")
def convert_file(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if not ext:
        raise HTTPException(400, "无法识别文件扩展名")

    config = get_config()
    from paths import to_local_path
    upload_dir = to_local_path(config["converter"]["upload_dir"], config)
    upload_dir.mkdir(parents=True, exist_ok=True)

    temp_path = upload_dir / f"upload_{os.urandom(4).hex()}{ext}"
    # 流式落盘，避免大文件读入内存（同步端点由 FastAPI 线程池执行）
    max_upload = int(config["converter"].get("max_upload_mb", 1024)) * 1024 * 1024
    with open(temp_path, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            out.write(chunk)
            if out.tell() > max_upload:
                out.close()
                temp_path.unlink(missing_ok=True)
                raise HTTPException(413, "文件超过上传上限 {}MB (converter.max_upload_mb)".format(
                    max_upload // 1024 // 1024))

    try:
        # 嗅探真实格式：扩展名伪装时用真实格式路由
        from sniffer import sniff_format
        real_ext, _ = sniff_format(str(temp_path))
        converter = registry.get(real_ext if real_ext != ext else ext)
        if not converter:
            raise HTTPException(400, f"不支持的格式: {ext}")
        if not converter.available:
            return JSONResponse({
                "filename": file.filename,
                "format": ext,
                "sniffed_format": real_ext,
                "converter": converter.display_name,
                "available": False,
                "markdown": converter.convert(str(temp_path)),
            })

        result = converter.convert(str(temp_path))
        return JSONResponse({
            "filename": file.filename,
            "format": ext,
            "sniffed_format": real_ext if real_ext != ext else None,
            "converter": converter.display_name,
            "length": len(result),
            "markdown": result,
        })
    except HTTPException:
        raise
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
def start_batch_conversion(source_dir: str = Query(None),
                           push: bool = Query(None, description="覆盖自动推送开关")):
    """启动批处理转换（可选指定源目录；push 覆盖 auto_push 配置）"""
    config = get_config()
    files = scan_directory(source_dir=source_dir) if source_dir else scan_directory()
    if not files:
        raise HTTPException(400, "源目录没有可转换的文件")

    # 后台异步执行
    def run_batch():
        try:
            # 仅"默认源目录的全量扫描"才允许源文件消失处理：
            # 指定 source_dir 的局部扫描里，范围外的文件只是"未纳入本次扫描"，
            # 不能据此判定为已删除（否则会把它们误删/误归档）
            start_batch(files, progress_callback=None, push_to_weknora=push,
                        full_scan=(source_dir is None))
        except Exception as e:
            logger.exception("批量转换线程异常: %s", e)
            # 复位批次状态，避免卡在 running
            import batch as _batch_mod
            with _batch_mod._lock:
                _batch_mod._batch_state["status"] = "idle"

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


@app.post("/api/batch/retry-push")
def batch_retry_push():
    """将最近推送失败的任务重新入队重推（返回重推数量）"""
    return retry_failed_pushes()


# ── 文件监听 ──


@app.get("/api/watcher/status")
def watcher_status():
    return get_watcher_status()


# ── 批量上传（生产版移植）──


@app.post("/api/upload/batch")
async def upload_batch(files: list[UploadFile] = File(...), overwrite: bool = Query(False)):
    """批量上传文件到 incoming 目录（流式写入，同名去重/覆盖）"""
    import asyncio
    from upload_batch import upload_files
    config = get_config()
    from paths import to_local_path
    incoming_dir = to_local_path(config["scanner"].get("source_dir") or "/data/input", config)
    file_list = [(f.filename or "unnamed", f) for f in files]
    result = await asyncio.to_thread(upload_files, file_list, str(incoming_dir), overwrite)
    return result


@app.get("/api/upload/status")
def upload_status():
    """上传任务进度"""
    from upload_batch import get_upload_status
    return get_upload_status()


# ── WeKnora 解析/嵌入进度（生产版移植，直连 postgres 聚合，需同网络）──


@app.get("/api/weknora/progress")
def weknora_progress(kb_id: str = Query("")):
    """WeKnora 解析/嵌入/Wiki 进度（直连 postgres 聚合）"""
    from weknora_progress import get_kb_progress, get_all_kbs_progress
    if kb_id:
        return get_kb_progress(kb_id)
    return get_all_kbs_progress()


@app.get("/api/weknora/progress/all")
def weknora_progress_all():
    """全部知识库进度列表"""
    from weknora_progress import get_all_kbs_progress
    return get_all_kbs_progress()


# ── 增量注册表统计 ──


@app.get("/api/registry/stats")
def registry_stats():
    """增量注册表统计（当前后端）"""
    from registry import get_registry
    recs = get_registry().to_dict()
    return {"total": len(recs), "records": list(recs.values())}


@app.post("/api/watcher/start")
def watcher_start():
    def on_new_files(files):
        """新文件出现时自动启动批量转换"""
        logger.info(f"监控触发: {len(files)} 个新文件")

        def run_auto():
            # watcher 只收到新增文件子集，缺席不等于删除 → full_scan=False
            start_batch(files, full_scan=False)

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
    client = get_weknora_client()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置 (设置 WEKNORA_URL 及登录凭据/API Key)"}
    return client.test_connection()


@app.get("/api/weknora/status")
def weknora_status():
    client = get_weknora_client()
    return {
        "configured": client.is_configured(),
        "url": client.api_url if client.api_url else "",
        "auth": "api_key" if client.api_key else ("jwt" if (client.email and client.password) else "none"),
        "auto_push": client.auto_push,
        "dataset_id": client.dataset_id,
        "kb_id": client._kb_id or "",
        "push_original_on_missing": client.push_original_on_missing,
        "push_as_file": client.push_as_file,
    }


@app.post("/api/weknora/list")
def weknora_list_knowledge(page: int = Query(1), page_size: int = Query(50)):
    """列出 WeKnora 知识库中的知识（对账用）"""
    client = get_weknora_client()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置"}
    items = client.list_knowledge(page=page, page_size=page_size)
    return {"ok": True, "count": len(items), "items": items}


@app.get("/api/weknora/verify")
def weknora_verify(doc_id: str = Query(...)):
    """校验单个知识条目的解析状态（HTTP 200 ≠ 解析成功）"""
    client = get_weknora_client()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置"}
    return client.verify_knowledge(doc_id)


@app.post("/api/weknora/verify-pushed")
def weknora_verify_pushed():
    """对账：校验本地 registry 记录的所有已推送文档的解析状态"""
    client = get_weknora_client()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置"}
    from registry import get_registry
    reg = get_registry()
    results = {"total": 0, "completed": 0, "failed": 0,
               "pending": 0, "failed_items": []}
    for src, rec in reg.to_dict().items():
        out_docs = rec.get("output_docs") or {}
        doc_ids = [v for v in out_docs.values() if v]
        if not doc_ids and rec.get("weknora_doc_id"):
            doc_ids = str(rec["weknora_doc_id"]).split(",")
        for doc_id in doc_ids:
            if not doc_id:
                continue
            results["total"] += 1
            info = client.verify_knowledge(doc_id)
            status = info.get("parse_status", "")
            if status == "completed":
                results["completed"] += 1
            elif status == "failed":
                results["failed"] += 1
                results["failed_items"].append({
                    "doc_id": doc_id, "source": src,
                    "error": info.get("error_message", "")})
            else:
                results["pending"] += 1
    return {"ok": True, **results}


@app.post("/api/weknora/reparse")
def weknora_reparse(doc_id: str = Query(...)):
    """显式触发文档解析重试（推送成功≠解析成功）"""
    client = get_weknora_client()
    if not client.is_configured():
        return {"ok": False, "error": "WeKnora 未配置"}
    result = client.trigger_reparse(doc_id)
    return result


@app.get("/api/weknora/mappings")
def weknora_get_mappings():
    """获取文件夹→知识库映射"""
    import folder_mappings
    return {"ok": True, "mappings": folder_mappings.get_mappings()}


@app.post("/api/weknora/mappings")
def weknora_save_mappings(mappings: dict = Body(default={})):
    """保存文件夹→知识库映射（整体覆盖）
    Body: {"mappings": {"工作邮件": "kb-id-or-name", ...}}
    """
    import folder_mappings
    if not isinstance(mappings, dict):
        raise HTTPException(400, "mappings 必须是 {folder: kb_id} 字典")
    # 值允许为 ID 或名称，忽略空值
    clean = {str(k): str(v).strip() for k, v in mappings.items() if str(v).strip()}
    data = folder_mappings.save_mappings(clean)
    if data is None:
        raise HTTPException(500, "映射保存失败")
    return {"ok": True, "mappings": data["mappings"], "updated_at": data["updated_at"]}


# ── 配置 ──

_SECRET_MASK = "***"
_SECRET_FIELDS = ("password", "api_key")

# config update 叶子键类型约束（防止类型注入导致运行时崩溃）
_INT_KEYS = {
    ("batch", "workers"), ("batch", "retry_count"), ("batch", "retry_delay"),
    ("chm", "extract_workers"), ("pdf", "ocr_dpi"),
    ("converter", "max_upload_mb"), ("converter", "port"),
    ("weknora", "max_file_size_mb"), ("weknora", "retry_count"),
    ("weknora", "push_queue_size"), ("weknora", "push_workers"),
    ("scanner", "poll_interval"),
}
_BOOL_KEYS = {
    ("weknora", "enabled"), ("weknora", "auto_push"),
    ("weknora", "push_original_on_missing"), ("weknora", "push_as_file"),
    ("weknora", "push_rel_title"), ("weknora", "delete_replaced"),
    ("converter", "keep_uploaded"), ("pdf", "ocr_enabled"),
    ("scanner", "recursive"), ("scanner", "enabled"), ("watcher", "enabled"),
}


def _validate_config_update(updates: dict):
    """校验 update 的叶子值类型，非法则抛 HTTPException(400)"""
    def walk(node, prefix):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            path = prefix + (k,)
            if isinstance(v, dict):
                walk(v, path)
                continue
            if path in _INT_KEYS:
                if not isinstance(v, bool) and not isinstance(v, int):
                    raise HTTPException(400, "配置项 {} 需要整数，收到: {}".format(
                        ".".join(path), type(v).__name__))
            elif path in _BOOL_KEYS:
                if not isinstance(v, bool):
                    raise HTTPException(400, "配置项 {} 需要布尔值，收到: {}".format(
                        ".".join(path), type(v).__name__))
    walk(updates, ())


def _mask_config(cfg: dict) -> dict:
    """脱敏配置中的凭据字段（用于 API 返回）"""
    import copy
    c = copy.deepcopy(cfg)
    wk = c.get("weknora", {})
    for k in _SECRET_FIELDS:
        if wk.get(k):
            wk[k] = _SECRET_MASK
    return c


@app.get("/api/config")
def get_config_api():
    return _mask_config(get_config())


@app.post("/api/config/reload")
def reload_config_api():
    cfg = reload_config()
    return {"status": "reloaded"}


@app.post("/api/config/update")
def update_config_api(updates: dict):
    """部分更新配置并持久化到配置文件"""
    # 仅允许更新白名单键，避免写入任意嵌套结构
    allowed = {"weknora", "batch", "chm", "pdf", "scanner", "converter"}
    filtered = {k: v for k, v in (updates or {}).items() if k in allowed}
    if not filtered:
        raise HTTPException(400, "无可更新的配置项")
    _validate_config_update(filtered)
    # 脱敏字段为 *** 时保留原值（UI 回填场景）
    wk = filtered.get("weknora", {})
    if wk:
        old_wk = get_config().get("weknora", {})
        for k in _SECRET_FIELDS:
            if wk.get(k) == _SECRET_MASK:
                wk[k] = old_wk.get(k, "")
    ok = save_config(filtered)
    if not ok:
        raise HTTPException(500, "配置写入失败")
    # 重建客户端与转换器以应用新配置
    global _weknora_client
    _weknora_client = None
    from converters.loader import register_all
    from converters import registry
    registry.rebuild(register_all)
    return {"status": "saved", "config": _mask_config(get_config())}


# ── 输出文件 ──


def _resolve_output_path(output_dir: Path, path: str) -> Path:
    """安全解析输出目录内的相对路径（防目录穿越）"""
    target = (output_dir / path).resolve() if path else output_dir.resolve()
    try:
        target.relative_to(output_dir.resolve())
    except ValueError:
        raise HTTPException(404, "路径不存在")
    return target


def _output_dir() -> Path:
    from paths import to_local_path
    config = get_config()
    return to_local_path(config["converter"]["output_dir"], config)


@app.get("/api/output")
def list_output(path: str = Query("")):
    """列出输出目录的文件"""
    output_dir = _output_dir()
    target = _resolve_output_path(output_dir, path)

    if not target.exists():
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
        size = target.stat().st_size
        # 预览限大小，避免大文件全量读入内存（下载走 /api/output/download 流式）
        preview_limit = 5 * 1024 * 1024
        if size > preview_limit:
            return {
                "path": str(target.relative_to(output_dir)),
                "is_dir": False,
                "size": size,
                "truncated": True,
                "content": "（文件 {} 超过 {}MB 预览上限，请使用下载）\n".format(
                    target.name, preview_limit // 1024 // 1024),
            }
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(target.relative_to(output_dir)),
            "is_dir": False,
            "size": size,
            "content": content,
        }


@app.get("/api/output/download")
def download_output(path: str = Query(...)):
    """下载输出目录中的文件（流式附件）"""
    from fastapi.responses import FileResponse
    output_dir = _output_dir()
    target = _resolve_output_path(output_dir, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


@app.post("/api/output/delete")
def delete_output(path: str = Query(...)):
    """删除输出目录中的文件"""
    output_dir = _output_dir()
    target = _resolve_output_path(output_dir, path)
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "deleted", "path": path}


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
.form-row { margin-bottom:12px; }
.form-row label { display:block; font-size:12px; color:#94a3b8; margin-bottom:4px; }
.form-row input[type="text"], .form-row input[type="password"] {
  width:100%; padding:8px 10px; background:#0f172a; border:1px solid #334155;
  border-radius:6px; color:#e2e8f0; font-size:14px; outline:none;
}
.form-row input:focus { border-color:#38bdf8; }
.form-row.checkbox-row { display:flex; gap:20px; flex-wrap:wrap; }
.form-row.checkbox-row label { display:inline-flex; align-items:center; gap:6px;
  color:#cbd5e1; font-size:14px; cursor:pointer; }
.form-row.checkbox-row input { width:16px; height:16px; }
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
      支持 OFD / WPS / DOCX / XLSX / ET / PDF / HTML / PPTX / CHM（自动嗅探真实格式）
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
      <div class="stat"><div class="stat-value" id="batchSkipped" style="color:#64748b">0</div><div class="stat-label">跳过</div></div>
    </div>
    <div class="progress-bar" style="margin-top:12px">
      <div class="progress-fill" id="batchProgress" style="width:0%"></div>
    </div>
    <div class="form-row" style="margin-top:12px">
      <label>源目录 (留空使用配置中的 /data/input)</label>
      <input type="text" id="batchSourceDir" placeholder="/data/input">
    </div>
    <div class="form-row checkbox-row">
      <label><input type="checkbox" id="batchPush" checked> 转换后推送到 WeKnora</label>
    </div>
    <div style="margin-top:12px" class="btn-group">
      <button class="btn btn-primary" onclick="startBatch()" id="btnBatchStart">▶ 开始批量</button>
      <button class="btn btn-danger" onclick="stopBatch()">⏹ 停止</button>
      <button class="btn btn-outline" onclick="scanFiles()">📋 扫描文件</button>
      <button class="btn btn-outline" onclick="clearBatch()">🗑 清除</button>
      <button class="btn btn-outline" onclick="retryPush()" id="btnRetryPush" disabled title="将最近推送失败的任务重新入队">🔄 重推失败 (<span id="retryPushCount">0</span>)</button>
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
      <div class="stat"><div class="stat-value" id="wkAuth">-</div><div class="stat-label">认证方式</div></div>
      <div class="stat"><div class="stat-value" id="wkAutoPush" style="color:#64748b">-</div><div class="stat-label">自动推送</div></div>
    </div>
    <div class="btn-group" style="margin-top:12px">
      <button class="btn btn-primary" onclick="testWeKnora()">🔌 测试连接</button>
      <button class="btn btn-outline" onclick="loadWeknoraConfig()">📥 载入当前配置</button>
    </div>
    <div id="wkTestResult"></div>
  </div>
  <div class="card">
    <div class="card-title">⚙ 连接配置（保存到配置文件，重启后生效）</div>
    <div class="form-row">
      <label>服务地址 (http://host:port)</label>
      <input type="text" id="cfgWkUrl" placeholder="http://192.168.31.131:8080">
    </div>
    <div class="form-row">
      <label>API Key (可选，优先于账号密码)</label>
      <input type="password" id="cfgWkApiKey" placeholder="sk-...">
    </div>
    <div class="form-row">
      <label>登录邮箱</label>
      <input type="text" id="cfgWkEmail" placeholder="admin@rag.local">
    </div>
    <div class="form-row">
      <label>登录密码</label>
      <input type="password" id="cfgWkPassword" placeholder="••••••••">
    </div>
    <div class="form-row">
      <label>知识库 ID / 名称 (留空用第一个)</label>
      <input type="text" id="cfgWkDataset" placeholder="aeb33f57-...">
    </div>
    <div class="form-row checkbox-row">
      <label><input type="checkbox" id="cfgWkEnabled"> 启用 WeKnora</label>
      <label><input type="checkbox" id="cfgWkAutoPush" checked> 转换后自动推送</label>
      <label><input type="checkbox" id="cfgWkPushOriginal" checked> 无法转换时推送原始文件</label>
      <label><input type="checkbox" id="cfgWkPushAsFile" checked> 以文件方式推送（保留目录结构）</label>
    </div>
    <div class="btn-group">
      <button class="btn btn-success" onclick="saveWeknoraConfig()">💾 保存配置</button>
    </div>
    <div id="wkSaveResult"></div>
  </div>
</div>

<div class="footer">DocConverter v2.1.0 | 文档转 Markdown → WeKnora 知识库 | 增量同步 · 格式嗅探 · CHM 支持</div>
</div>

<script>
// ── 认证令牌支持（server.auth_token 启用时自动附加 X-Auth-Token）──
var _dcAuthToken = localStorage.getItem('dc_auth_token') || '';
var _origFetch = window.fetch;
window.fetch = function(input, init) {
  init = init || {};
  if (_dcAuthToken) {
    if (!init.headers) { init.headers = {}; }
    if (init.headers instanceof Headers) { init.headers.set('X-Auth-Token', _dcAuthToken); }
    else { init.headers['X-Auth-Token'] = _dcAuthToken; }
  }
  return _origFetch(input, init).then(function(resp) {
    if (resp.status === 401) {
      var t = prompt('本服务已启用访问令牌，请输入（可留空重试）：');
      if (t) { localStorage.setItem('dc_auth_token', t); _dcAuthToken = t; }
      // 用新令牌重试一次
      if (init.headers instanceof Headers) { init.headers.set('X-Auth-Token', _dcAuthToken); }
      else { init.headers = init.headers || {}; init.headers['X-Auth-Token'] = _dcAuthToken; }
      return _origFetch(input, init);
    }
    return resp;
  });
};

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
      '<span>📄 '+escHtml(file.name)+'</span><span>🔧 '+escHtml(data.converter)+'</span><span>📏 '+data.length+' 字符</span>';
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
  const dir = document.getElementById('batchSourceDir').value.trim();
  const url = '/api/scan' + (dir ? '?source_dir=' + encodeURIComponent(dir) : '');
  try {
    const r = await fetch(url, { method:'POST' });
    const d = await r.json();
    result.innerHTML = '<p style="color:#22c55e;margin-bottom:8px">✅ 找到 ' + d.files_found + ' 个文件' +
      ' (' + d.statistics.total_size_str + ')</p>';
    if (d.files_found > 0) {
      let html = '<table><tr><th>文件</th><th>格式</th><th>大小</th></tr>';
      for (const f of d.files) {
        html += '<tr><td>' + escHtml(f.rel_path) + '</td><td>' + escHtml(f.ext) + '</td><td>' + escHtml(f.size_str) + '</td></tr>';
      }
      html += '</table>';
      result.innerHTML += html;
    }
  } catch(e) {
    result.innerHTML = '<p style="color:#ef4444">❌ ' + escHtml(e.message) + '</p>';
  }
}

// ── 批量 ──
async function startBatch() {
  document.getElementById('btnBatchStart').disabled = true;
  document.getElementById('btnBatchStart').textContent = '⏳ 运行中...';
  const dir = document.getElementById('batchSourceDir').value.trim();
  const push = document.getElementById('batchPush').checked;
  const url = '/api/batch/start?push=' + push + (dir ? '&source_dir=' + encodeURIComponent(dir) : '');
  try {
    await fetch(url, { method:'POST' });
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
        // 完成后再轮询两次，等待推送队列消化
        setTimeout(pollBatchStatus, 3000);
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
  document.getElementById('batchSkipped').textContent = s.skipped || 0;
  const pct = s.total > 0 ? Math.round((s.completed||0)/s.total*100) : 0;
  document.getElementById('batchProgress').style.width = pct + '%';

  // 推送失败计数 → 重推按钮
  const failedPush = d.push_failed_count || 0;
  const rc = document.getElementById('retryPushCount');
  const rb = document.getElementById('btnRetryPush');
  if (rc) rc.textContent = failedPush;
  if (rb) rb.disabled = failedPush === 0;

  if (d.tasks && d.tasks.length > 0) {
    let html = '<table><tr><th>文件</th><th>格式</th><th>状态</th><th>WeKnora</th><th>结果</th></tr>';
    for (const t of d.tasks.slice(0,200)) {
      const cls = 'status-' + t.status;
      const pushTxt = t.weknora_push === 'pushed' ? '<span class="status-success">✅ 已推送</span>'
        : t.weknora_push === 'failed' ? '<span class="status-failed" title="'+escHtml(t.weknora_error||'')+'">❌ 失败</span>'
        : t.weknora_push === 'pending' ? '<span class="status-running">⏳ 排队中</span>'
        : '<span class="status-skipped">-</span>';
      const resultCell = t.output_path
        ? '<a style="color:#38bdf8;cursor:pointer" onclick="downloadFile('+JSON.stringify(t.output_path)+')">⬇ 下载</a>'
        : (t.result_length ? t.result_length+'字' : escHtml(t.error || ''));
      html += '<tr><td>' + escHtml(t.rel_path) + (t.sniffed_ext ? ' <span class="tag">嗅探:'+escHtml(t.sniffed_ext)+'</span>' : '') + '</td><td>' + escHtml(t.ext) + '</td>' +
        '<td class="'+cls+'">' + t.status + (t.skipped ? ' (跳过)' : '') + '</td>' +
        '<td>' + pushTxt + '</td>' +
        '<td>' + resultCell + '</td></tr>';
    }
    html += '</table>';
    document.getElementById('batchTasks').innerHTML = html;
  }
}

function downloadFile(path) {
  window.open('/api/output/download?path=' + encodeURIComponent(path), '_blank');
}

async function stopBatch() { await fetch('/api/batch/stop', { method:'POST' }); }
async function clearBatch() { await fetch('/api/batch/clear', { method:'POST' }); pollBatchStatus(); }
async function retryPush() {
  try {
    const r = await fetch('/api/batch/retry-push', { method:'POST' });
    const d = await r.json();
    alert('已重新入队 ' + (d.requeued || 0) + ' 个失败任务');
    pollBatchStatus();
  } catch(e) { console.error(e); }
}

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
      ? '<p style="color:#22c55e">✅ 连接成功! 知识库: ' + escHtml(d.kb_name || d.kb_id || '?') + ' (认证: ' + escHtml(d.auth || 'jwt') + ')</p>'
      : '<p style="color:#ef4444">❌ ' + escHtml(d.error || '连接失败') + '</p>';
  } catch(e) {
    result.innerHTML = '<p style="color:#ef4444">❌ ' + escHtml(e.message) + '</p>';
  }
}

async function updateWeKnoraStatus() {
  const r = await fetch('/api/weknora/status');
  const d = await r.json();
  document.getElementById('wkConfigured').textContent = d.configured ? '是' : '否';
  document.getElementById('wkConfigured').style.color = d.configured ? '#22c55e' : '#ef4444';
  document.getElementById('wkAuth').textContent = d.auth === 'api_key' ? 'API Key' : (d.auth === 'jwt' ? '账号密码' : '-');
  document.getElementById('wkAutoPush').textContent = d.auto_push ? '开启' : '关闭';
}

async function loadWeknoraConfig() {
  const r = await fetch('/api/config');
  const d = await r.json();
  const wk = d.weknora || {};
  document.getElementById('cfgWkUrl').value = wk.api_url || '';
  document.getElementById('cfgWkApiKey').value = wk.api_key || '';
  document.getElementById('cfgWkEmail').value = wk.email || '';
  document.getElementById('cfgWkPassword').value = wk.password || '';
  document.getElementById('cfgWkDataset').value = wk.dataset_id || '';
  document.getElementById('cfgWkEnabled').checked = !!wk.enabled;
  document.getElementById('cfgWkAutoPush').checked = wk.auto_push !== false;
  document.getElementById('cfgWkPushOriginal').checked = wk.push_original_on_missing !== false;
  document.getElementById('cfgWkPushAsFile').checked = wk.push_as_file !== false;
}

async function saveWeknoraConfig() {
  const result = document.getElementById('wkSaveResult');
  result.innerHTML = '<p style="color:#94a3b8">保存中...</p>';
  const body = {
    weknora: {
      api_url: document.getElementById('cfgWkUrl').value.trim(),
      api_key: document.getElementById('cfgWkApiKey').value.trim(),
      email: document.getElementById('cfgWkEmail').value.trim(),
      password: document.getElementById('cfgWkPassword').value,
      dataset_id: document.getElementById('cfgWkDataset').value.trim(),
      enabled: document.getElementById('cfgWkEnabled').checked,
      auto_push: document.getElementById('cfgWkAutoPush').checked,
      push_original_on_missing: document.getElementById('cfgWkPushOriginal').checked,
      push_as_file: document.getElementById('cfgWkPushAsFile').checked,
    }
  };
  try {
    const r = await fetch('/api/config/update', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '保存失败');
    result.innerHTML = '<p style="color:#22c55e">✅ 配置已保存</p>';
    updateWeKnoraStatus();
  } catch(e) {
    result.innerHTML = '<p style="color:#ef4444">❌ ' + escHtml(e.message) + '</p>';
  }
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
        return '<tr><td><a onclick="loadOutputFiles('+JSON.stringify(item.path)+')" style="color:#38bdf8;cursor:pointer">📁 ' + escHtml(item.name) + '</a></td><td>-</td><td></td></tr>';
      } else {
        return '<tr><td><a onclick="viewOutputFile('+JSON.stringify(item.path)+')" style="color:#e2e8f0;cursor:pointer">📄 ' + escHtml(item.name) + '</a></td><td>' + item.size + 'B</td>' +
          '<td><a style="color:#38bdf8;cursor:pointer" onclick="downloadFile('+JSON.stringify(item.path)+')">⬇ 下载</a></td></tr>';
      }
    }).join('');
    var html = '<div style="margin-bottom:8px"><button class="btn btn-outline" onclick="loadOutputFiles()">🏠 根目录</button></div>';
    if (path) html += '<p style="color:#94a3b8;margin-bottom:8px">📁 ' + escHtml(path) + '/</p>';
    html += '<table><tr><th>名称</th><th>大小</th><th>操作</th></tr>' + items + '</table>';
    document.getElementById('outputFiles').innerHTML = html;
  }
}

async function viewOutputFile(path) {
  const r = await fetch('/api/output?path=' + encodeURIComponent(path));
  const d = await r.json();
  const html = '<div style="margin-bottom:8px"><button class="btn btn-outline" onclick="loadOutputFiles()">← 返回</button> ' +
    '<button class="btn btn-primary" onclick="downloadFile('+JSON.stringify(path)+')">⬇ 下载</button></div>' +
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
