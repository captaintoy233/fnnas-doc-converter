"""
批量处理器: 队列管理、并行转换、进度追踪、重试机制、增量同步、异步推送

- 转换前做真实格式嗅探（防止扩展名伪装文件路由错误）
- 通过 SyncRegistry 增量跳过未变化的文件
- WeKnora 推送放入独立后台队列，不阻塞转换主流程
"""
import os, time, threading, json, shutil, queue
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_config
from scanner import FileInfo
from converters import registry
from sniffer import sniff_format
from registry import get_registry
from paths import to_local_path

# 任务状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# 推送状态
PUSH_NONE = "none"
PUSH_PENDING = "pending"
PUSH_PUSHED = "pushed"
PUSH_FAILED = "failed"

# 全局状态
_batch_state = {
    "status": "idle",         # idle | running | stopping | done
    "tasks": [],              # list[BatchTask]
    "started_at": None,
    "finished_at": None,
    "statistics": {},
}

_lock = threading.Lock()

# ── 异步推送队列 ──
_push_queue: "queue.Queue[Optional[BatchTask]]" = queue.Queue()
_push_thread = None


def _start_push_worker():
    """启动全局推送工作线程（幂等）"""
    global _push_thread
    if _push_thread is not None and _push_thread.is_alive():
        return
    _push_thread = threading.Thread(target=_push_worker_loop, daemon=True)
    _push_thread.start()


def _sanitize_md_for_weknora(content: str) -> str:
    """推送前的 Markdown 卫生清洗：控制字符、BOM、CRLF 归一化"""
    if not content:
        return content
    import re as _re
    # 去除 BOM 与非法控制字符（保留 \t \n \r）
    content = content.lstrip("\ufeff")
    content = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content


# 推送客户端按 weknora 配置签名缓存，避免每个任务重新登录/拉知识库列表
_push_client = None
_push_client_sig = None


def _weknora_config_signature(config: dict) -> str:
    wk = config.get("weknora", {})
    return json.dumps({
        "url": wk.get("api_url"), "key": wk.get("api_key"),
        "email": wk.get("email"), "pw": wk.get("password"),
        "ds": wk.get("dataset_id"), "enabled": wk.get("enabled"),
    }, sort_keys=True, ensure_ascii=False)


def _get_push_client(config: dict):
    """按配置签名获取（缓存）的 WeKnoraClient；配置变化时重建"""
    global _push_client, _push_client_sig
    sig = _weknora_config_signature(config)
    if _push_client is None or _push_client_sig != sig:
        from weknora_client import WeKnoraClient
        _push_client = WeKnoraClient(config)
        _push_client_sig = sig
    return _push_client


def _delete_old_docs(client, rec: dict, source_path: str):
    """删除该源文件此前推送过的旧文档（更新语义；删除失败仅记录不中断）"""
    if not rec:
        return
    logger = __import__("logging").getLogger("docconverter.batch")
    ids = set()
    out_docs = rec.get("output_docs") or {}
    ids.update(v for v in out_docs.values() if v)
    old_id = rec.get("weknora_doc_id") or ""
    for part in str(old_id).split(","):
        part = part.strip()
        if part:
            ids.add(part)
    for doc_id in ids:
        try:
            r = client.delete_knowledge(doc_id)
            if r.get("ok"):
                logger.info("已删除旧文档 %s（源: %s）", doc_id, source_path)
            else:
                logger.warning("删除旧文档失败 %s: %s", doc_id, r.get("error"))
        except Exception as e:
            logger.warning("删除旧文档异常 %s: %s", doc_id, e)


def _push_worker_loop():
    """消费推送队列：转换成功后异步推送到 WeKnora

    注意: 每个任务重新创建客户端并读取最新配置，
    避免配置热更新（/api/config/update）后仍连接旧地址/旧凭据。
    """
    from weknora_client import WeKnoraClient  # noqa: F401
    logger = __import__("logging").getLogger("docconverter.batch")

    while True:
        task = _push_queue.get()
        try:
            if task is None:
                break
            if task.weknora_push != PUSH_PENDING:
                continue

            config = get_config()
            client = _get_push_client(config)
            if not client.is_configured():
                task.weknora_error = "WeKnora 未配置，跳过推送"
                task.weknora_push = PUSH_FAILED
                continue

            source_path = str(task.file_info.path)
            wk_cfg = config.get("weknora", {})
            rel_path = task.file_info.rel_path.replace("\\", "/")
            md_rel_path = str(Path(rel_path).with_suffix(".md"))
            output_dir = to_local_path(config["converter"]["output_dir"], config)

            # folder→KB 映射：按文件相对路径解析目标知识库（最长前缀优先）
            target_kb = None
            if wk_cfg.get("folder_kb_map_path"):
                import folder_mappings
                mappings = folder_mappings.get_mappings()
                if mappings:
                    target_kb = folder_mappings.resolve_kb_id(
                        rel_path, mappings,
                        default_kb=wk_cfg.get("dataset_id", "")) or None

            metadata = {
                "source": "docconverter",
                "source_path": source_path,
                "source_rel_path": rel_path,
                "source_format": task.converter or "",
                "source_hash": task.source_hash,
            }

            if task.push_original:
                # 无法转换的格式：推送原始文件，fileName 用相对路径保留结构
                old_rec = get_registry().get(source_path) if \
                    (wk_cfg.get("delete_replaced", True) and not task.skipped) else None
                result = client.push_file(
                    source_path,
                    title=Path(source_path).stem,
                    source_format=task.converter or "",
                    metadata=metadata,
                    rel_path=rel_path,
                    kb_id=target_kb,
                )
                if result.get("ok"):
                    # 推送成功后（且非重复）再删除旧文档，避免推送失败丢文档
                    if old_rec and not result.get("duplicate"):
                        _delete_old_docs(client, old_rec, source_path)
                    task.weknora_doc_id = result.get("doc_id", "")
                    task.weknora_push = PUSH_PUSHED
                    task.weknora_error = ""
                    if result.get("duplicate"):
                        task.weknora_error = result.get("message", "已存在（幂等）")
                    try:
                        get_registry().mark_pushed(source_path, result.get("doc_id", ""))
                    except Exception:
                        pass
                    logger.info("推送成功: %s -> %s%s", source_path, task.weknora_doc_id,
                                " (重复跳过)" if result.get("duplicate") else "")
                else:
                    task.weknora_error = result.get("error", "推送失败")
                    task.weknora_push = PUSH_FAILED
                    logger.error("推送失败: %s: %s", source_path, task.weknora_error)
                continue

            # 转换产物：一个源文件可能产出多个 md（CHM 章节/压缩包内文件），逐个推送
            files_to_push = task.output_files or ([md_rel_path] if task.markdown else [])
            if not files_to_push:
                task.weknora_push = PUSH_FAILED
                task.weknora_error = "无 Markdown 内容可推送"
                continue

            if wk_cfg.get("push_as_file", True):
                ok_count = 0
                doc_ids = []
                failed_detail = []
                # 更新语义：内容变化时先删除旧文档（file 类型不支持内容更新，只能删除重建）
                reg = get_registry()
                rec = reg.get(source_path)
                old_docs = rec.get("output_docs") if rec else {}
                replace = wk_cfg.get("delete_replaced", True) and not task.skipped
                for out_rel in files_to_push:
                    out_rel = out_rel.replace("\\", "/")
                    out_path = output_dir / out_rel
                    try:
                        content = _sanitize_md_for_weknora(
                            out_path.read_text(encoding="utf-8"))
                    except OSError:
                        failed_detail.append("{}: 输出文件不可读".format(out_rel))
                        continue
                    old_id = old_docs.get(out_rel) if replace else None
                    result = client.push_markdown_file(
                        title=Path(out_rel).stem,
                        content=content,
                        rel_path=out_rel,
                        metadata=metadata,
                        kb_id=target_kb,
                    )
                    if result.get("ok"):
                        ok_count += 1
                        if result.get("doc_id"):
                            doc_ids.append(result["doc_id"])
                            try:
                                reg.record_output_doc(source_path, out_rel, result["doc_id"])
                            except Exception:
                                pass
                        # 更新语义：推送成功后（且非重复）再删除旧文档，避免推送失败导致文档丢失
                        if old_id and not result.get("duplicate"):
                            client.delete_knowledge(old_id)
                        if result.get("duplicate"):
                            task.weknora_error = result.get("message", "已存在（幂等）")
                    else:
                        failed_detail.append("{}: {}".format(
                            out_rel, result.get("error", "推送失败")))
                if ok_count == len(files_to_push):
                    task.weknora_doc_id = ",".join(doc_ids)
                    task.weknora_push = PUSH_PUSHED
                    if not task.weknora_error:
                        task.weknora_error = ""
                    try:
                        get_registry().mark_pushed(source_path, task.weknora_doc_id)
                    except Exception:
                        pass
                    logger.info("推送成功: %s (%d/%d 文件)%s", source_path,
                                ok_count, len(files_to_push),
                                " (含重复跳过)" if task.weknora_error else "")
                else:
                    task.weknora_push = PUSH_FAILED
                    task.weknora_error = "; ".join(failed_detail[:3]) or "推送失败"
                    logger.error("推送失败: %s: %s", source_path, task.weknora_error)
            else:
                # manual 模式：逐个输出文件推送（多文件源 CHM 章节/压缩包完整入库）
                if not files_to_push:
                    task.weknora_push = PUSH_FAILED
                    task.weknora_error = "无 Markdown 内容可推送"
                    continue
                ok_count = 0
                doc_ids = []
                failed_detail = []
                reg = get_registry()
                for out_rel in files_to_push:
                    out_rel = out_rel.replace("\\", "/")
                    out_path = output_dir / out_rel
                    try:
                        content = _sanitize_md_for_weknora(
                            out_path.read_text(encoding="utf-8"))
                    except OSError:
                        failed_detail.append("{}: 输出文件不可读".format(out_rel))
                        continue
                    if wk_cfg.get("push_rel_title", True):
                        title = str(Path(out_rel).with_suffix(""))
                    else:
                        title = Path(source_path).stem
                    result = client.push_manual(
                        title=title,
                        content=content,
                        source_path=source_path,
                        source_format=task.converter or "",
                        metadata=metadata,
                        kb_id=target_kb,
                    )
                    if result.get("ok"):
                        ok_count += 1
                        if result.get("doc_id"):
                            doc_ids.append(result["doc_id"])
                            try:
                                reg.record_output_doc(source_path, out_rel, result["doc_id"])
                            except Exception:
                                pass
                        if result.get("duplicate"):
                            task.weknora_error = result.get("message", "已存在（幂等）")
                    else:
                        failed_detail.append("{}: {}".format(
                            out_rel, result.get("error", "推送失败")))
                if ok_count == len(files_to_push):
                    task.weknora_doc_id = ",".join(doc_ids)
                    task.weknora_push = PUSH_PUSHED
                    if not task.weknora_error:
                        task.weknora_error = ""
                    try:
                        get_registry().mark_pushed(source_path, task.weknora_doc_id)
                    except Exception:
                        pass
                    logger.info("推送成功: %s (%d/%d 文件)%s", source_path,
                                ok_count, len(files_to_push),
                                " (含重复跳过)" if task.weknora_error else "")
                else:
                    task.weknora_push = PUSH_FAILED
                    task.weknora_error = "; ".join(failed_detail[:3]) or "推送失败"
                    logger.error("推送失败: %s: %s", source_path, task.weknora_error)
        except Exception as e:
            logger.error("推送线程异常: %s", e)
        finally:
            _push_queue.task_done()


def enqueue_push(task: "BatchTask", use_push: bool = True):
    """将任务加入推送队列（use_push 由调用方按 start_batch 的 push 参数决定）"""
    if not use_push:
        return
    # 增量跳过的任务：已推送过的不重复推送
    if task.skipped:
        rec = get_registry().get(str(task.file_info.path))
        if rec and rec.get("pushed"):
            task.weknora_push = PUSH_PUSHED
            task.weknora_doc_id = rec.get("weknora_doc_id", "")
            return
    task.weknora_push = PUSH_PENDING
    _start_push_worker()
    _push_queue.put(task)


class BatchTask:
    """单个批量转换任务"""

    def __init__(self, file_info: FileInfo):
        self.file_info = file_info
        self.status = STATUS_PENDING
        self.converter = None
        self.result_length = 0
        self.markdown = None
        self.output_path = None
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.retry_count = 0
        self.sniffed_ext = None
        self.source_hash = ""
        self.skipped = False          # 增量跳过
        self.push_original = False    # 原文回退推送（无法转换时）
        self.output_files = []        # 全部输出的相对路径（多文件：CHM 章节/压缩包）
        self.weknora_push = PUSH_NONE
        self.weknora_doc_id = ""
        self.weknora_error = ""

    def to_dict(self):
        return {
            "rel_path": self.file_info.rel_path,
            "ext": self.file_info.ext,
            "sniffed_ext": self.sniffed_ext or "",
            "size": self.file_info.size,
            "size_str": self.file_info.size_str,
            "status": self.status,
            "converter": self.converter or "",
            "result_length": self.result_length,
            "output_path": str(self.output_path) if self.output_path else None,
            "output_count": len(self.output_files),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "retry_count": self.retry_count,
            "skipped": self.skipped,
            "push_original": self.push_original,
            "weknora_push": self.weknora_push,
            "weknora_doc_id": self.weknora_doc_id,
            "weknora_error": self.weknora_error,
        }


def _pick_converter(ext: str, path: Path):
    """按扩展名取转换器；若嗅探出不同格式，返回 [嗅探转换器, 扩展名转换器] 候选链

    修复: .et/.wps 等文件可能被魔数误判（如 .et 嗅探成 .xlsx），
    嗅探转换器失败时回退到扩展名转换器。
    """
    converter = registry.get(ext)
    sniffed_ext, sniffed_name = sniff_format(path)
    sniffed_converter = registry.get(sniffed_ext)
    if sniffed_ext != ext and sniffed_converter is not None and sniffed_converter.available \
            and sniffed_converter is not converter:
        return [sniffed_converter, converter], sniffed_ext, sniffed_name
    return [converter], ext, None


def _convert_single(task: BatchTask, output_dir: Path, config: dict) -> BatchTask:
    """执行单个文件转换（含嗅探路由 + 多文件输出支持）"""
    task.started_at = datetime.now().isoformat()
    task.status = STATUS_RUNNING

    try:
        path = Path(task.file_info.path)
        ext = task.file_info.ext

        candidates, real_ext, real_name = _pick_converter(ext, path)
        if real_ext != ext:
            task.sniffed_ext = real_ext
        if not candidates:
            # 完全无转换器：若配置了原文回退推送且 WeKnora 可用，则推送原始文件
            wk_cfg = config.get("weknora", {})
            if wk_cfg.get("push_original_on_missing", True) and \
                    wk_cfg.get("enabled", False) and wk_cfg.get("api_url"):
                reg = get_registry()
                task.source_hash = reg.sha256_of_file(str(path))
                rec = reg.get(str(path))
                if rec and rec.get("source_hash") == task.source_hash and rec.get("pushed"):
                    task.converter = "原文推送"
                    task.push_original = True
                    task.skipped = True
                    task.weknora_push = PUSH_PUSHED
                    task.weknora_doc_id = rec.get("weknora_doc_id", "")
                    task.status = STATUS_SUCCESS
                    task.finished_at = datetime.now().isoformat()
                    return task
                task.converter = "原文推送"
                task.push_original = True
                reg.record(str(path), task.source_hash, status="pushed_original")
                task.status = STATUS_SUCCESS
                task.finished_at = datetime.now().isoformat()
                return task
            task.status = STATUS_SKIPPED
            task.error = f"不支持的格式: {ext}"
            return task

        # 取第一个可用的真实转换器；全部占位时走原文回退推送
        converter = next((c for c in candidates if c is not None and c.available), None)
        placeholder = next((c for c in candidates if c is not None), None)
        if converter is None:
            # 占位转换器：同样走原文回退推送
            wk_cfg = config.get("weknora", {})
            if wk_cfg.get("push_original_on_missing", True) and \
                    wk_cfg.get("enabled", False) and wk_cfg.get("api_url"):
                reg = get_registry()
                task.source_hash = reg.sha256_of_file(str(path))
                rec = reg.get(str(path))
                if rec and rec.get("source_hash") == task.source_hash and rec.get("pushed"):
                    task.converter = "{}→原文推送".format(placeholder.display_name)
                    task.push_original = True
                    task.skipped = True
                    task.weknora_push = PUSH_PUSHED
                    task.weknora_doc_id = rec.get("weknora_doc_id", "")
                    task.status = STATUS_SUCCESS
                    task.finished_at = datetime.now().isoformat()
                    return task
                task.converter = "{}→原文推送".format(placeholder.display_name)
                task.push_original = True
                reg.record(str(path), task.source_hash, status="pushed_original")
                task.status = STATUS_SUCCESS
                task.finished_at = datetime.now().isoformat()
                return task
            task.status = STATUS_SKIPPED
            task.error = "需要系统工具的格式: {} ({})".format(placeholder.display_name, ext)
            return task

        task.converter = converter.display_name

        # 增量跳过：源文件指纹未变化且已有输出
        registry = get_registry()
        source_hash = registry.sha256_of_file(str(path))
        task.source_hash = source_hash
        rel = task.file_info.rel_path
        md_rel = str(Path(rel).with_suffix(".md"))
        rec = registry.get(str(path))
        if rec and rec.get("source_hash") == source_hash and rec.get("status") == "converted":
            # 多输出源（CHM 章节/压缩包）按 output_files 全部存在才跳过
            out_files = rec.get("output_files") or [md_rel]
            all_exist = True
            for of in out_files:
                if not (output_dir / of).exists():
                    all_exist = False
                    break
            if all_exist:
                task.status = STATUS_SUCCESS
                task.output_path = str(output_dir / out_files[0])
                task.output_files = out_files
                task.skipped = True
                try:
                    task.markdown = (output_dir / out_files[0]).read_text(encoding="utf-8")
                    task.result_length = len(task.markdown)
                except Exception:
                    pass
                task.finished_at = datetime.now().isoformat()
                return task

        # 转换（支持一个源文件产出多个 md；候选链回退: 嗅探转换器失败→扩展名转换器）
        out_files = None
        last_err = None
        for cand in candidates:
            if cand is None or not cand.available:
                continue
            try:
                out_files = cand.convert_to_files(str(path), rel)
                if out_files:
                    task.converter = cand.display_name
                    break
            except Exception as e:
                last_err = e
                logger.warning("转换失败(%s→%s) %s: %s", cand.display_name, ext, path, e)
                continue
        if out_files is None:
            raise RuntimeError("所有候选转换器均失败: {}".format(last_err))
        task.markdown = ""
        task.output_files = []
        primary_rel = md_rel
        for out_rel, content in out_files:
            task.output_files.append(out_rel)
            out_path = (output_dir / out_rel)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            if out_rel == md_rel or task.output_path is None:
                task.output_path = str(out_path)
                task.markdown = content
            task.result_length += len(content)

        registry.record(
            str(path), source_hash,
            output_rel=primary_rel,
            converted_hash=registry.sha256_of_text(task.markdown or ""),
            status="converted",
            output_files=task.output_files,
        )
        task.status = STATUS_SUCCESS
    except Exception as e:
        task.status = STATUS_FAILED
        task.error = str(e)[:500]
    finally:
        task.finished_at = datetime.now().isoformat()

    return task


def start_batch(files: list, progress_callback: Optional[Callable] = None,
                push_to_weknora: Optional[bool] = None) -> dict:
    """启动批量转换

    Args:
        files: list[FileInfo] 待转换文件
        progress_callback: (completed, total, task) 进度回调
        push_to_weknora: 是否推送到 WeKnora（None = 按配置 auto_push）
    """
    global _batch_state

    with _lock:
        if _batch_state["status"] == "running":
            return {"error": "已有批次在运行中"}

        config = get_config()
        output_dir = to_local_path(config["converter"]["output_dir"], config)
        output_dir.mkdir(parents=True, exist_ok=True)

        tasks = [BatchTask(f) for f in files]
        workers = int(config["batch"].get("workers", 2))
        retry_count = int(config["batch"].get("retry_count", 3))
        retry_delay = int(config["batch"].get("retry_delay", 5))
        use_push = push_to_weknora if push_to_weknora is not None \
            else bool(config.get("weknora", {}).get("auto_push", True))

        _batch_state = {
            "status": "running",
            "tasks": tasks,
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "statistics": {
                "total": len(tasks),
                "completed": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
            },
        }

    pending_tasks = list(tasks)

    def _process_result(task: BatchTask):
        # 重试逻辑：失败后最多重试 retry_count 次
        attempt = 0
        while task.status == STATUS_FAILED and attempt < retry_count:
            attempt += 1
            task.retry_count += 1
            task.error = None
            time.sleep(retry_delay)
            task = _convert_single(task, output_dir, config)

        with _lock:
            stats = _batch_state["statistics"]
            stats["completed"] += 1
            if task.skipped:
                stats["skipped"] += 1
            elif task.status == STATUS_SUCCESS:
                stats["success"] += 1
            elif task.status == STATUS_FAILED:
                stats["failed"] += 1
            elif task.status == STATUS_SKIPPED:
                stats["skipped"] += 1

        if use_push and task.status == STATUS_SUCCESS and (task.markdown or task.push_original or task.output_files):
            enqueue_push(task, use_push=use_push)

        if progress_callback:
            progress_callback(stats["completed"], stats["total"], task)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while pending_tasks and _batch_state["status"] != "stopping":
            batch = pending_tasks[:workers]
            pending_tasks = pending_tasks[workers:]

            futures = {executor.submit(_convert_single, t, output_dir, config): t
                       for t in batch}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    task = future.result()
                except Exception as e:
                    task.status = STATUS_FAILED
                    task.error = str(e)[:500]
                _process_result(task)

    with _lock:
        _batch_state["status"] = "done"
        _batch_state["finished_at"] = datetime.now().isoformat()

    return _batch_state


def stop_batch():
    """停止批次处理"""
    global _batch_state
    with _lock:
        if _batch_state["status"] == "running":
            _batch_state["status"] = "stopping"


def get_batch_status() -> dict:
    """获取批次状态"""
    with _lock:
        tasks_preview = [t.to_dict() for t in _batch_state["tasks"][:200]]
        return {
            "status": _batch_state["status"],
            "started_at": _batch_state["started_at"],
            "finished_at": _batch_state["finished_at"],
            "statistics": _batch_state["statistics"],
            "tasks": tasks_preview,
            "total_tasks": len(_batch_state["tasks"]),
            "push_queue_size": _push_queue.qsize(),
        }


def clear_batch():
    """清除批次结果（运行中/停止中拒绝，避免并发写输出与统计错乱）"""
    global _batch_state
    with _lock:
        if _batch_state["status"] in ("running", "stopping"):
            return {"error": "批次正在运行，无法清除"}
        _batch_state = {
            "status": "idle",
            "tasks": [],
            "started_at": None,
            "finished_at": None,
            "statistics": {},
        }
        return {"status": "cleared"}
