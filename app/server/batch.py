"""
批量处理器: 队列管理、并行转换、进度追踪、重试机制、增量同步、异步推送

- 转换前做真实格式嗅探（防止扩展名伪装文件路由错误）
- 通过 SyncRegistry 增量跳过未变化的文件
- WeKnora 推送放入独立后台队列，不阻塞转换主流程
"""
import os, time, logging, threading, json, shutil, queue
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_config
from scanner import FileInfo
from converters import registry
from sniffer import sniff_format
from registry import get_registry

try:
    from config import RENDER_VERSION
except Exception:  # pragma: no cover
    RENDER_VERSION = ""
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

# ── 异步推送队列（有界 + 多消费者 + 失败持久化 + 优雅退出） ──
_push_stop_event = threading.Event()
_push_threads: list = []
_push_failed: list = []          # 最近推送失败的任务（供 /api/batch/retry-push 重推）
_push_failed_lock = threading.Lock()


def _queue_maxsize() -> int:
    try:
        raw = get_config().get("weknora", {}).get("push_queue_size")
        n = 200 if raw in (None, "") else int(raw)
    except Exception:
        n = 200
    return max(1, n)


# 有界队列在导入时创建（满则背压，阻塞生产者）；容量取启动时配置，变更需重启
_push_queue = queue.Queue(maxsize=_queue_maxsize())


def _get_push_queue() -> "queue.Queue":
    """返回有界推送队列（导入时创建；容量变更需重启生效）"""
    return _push_queue


def _ensure_push_workers():
    """确保有足够推送消费者（幂等；停止态不启动）"""
    global _push_threads
    if _push_stop_event.is_set():
        return
    _get_push_queue()
    try:
        raw = get_config().get("weknora", {}).get("push_workers")
        n = 2 if raw in (None, "") else int(raw)
    except Exception:
        n = 2
    n = max(1, min(n, 16))
    alive = [t for t in _push_threads if t.is_alive()]
    _push_threads = alive
    while len(alive) < n:
        t = threading.Thread(target=_push_worker_loop, daemon=True)
        t.start()
        alive.append(t)


def shutdown_push_workers(drain: bool = True, timeout: float = 30.0):
    """优雅停止推送线程。drain=True 时等待队列排空（在途任务完成）后退出。"""
    logger = logging.getLogger("docconverter.batch")
    if not _push_threads:
        return
    if drain and _push_queue is not None:
        try:
            _push_queue.join()
        except Exception:
            pass
    _push_stop_event.set()
    deadline = time.time() + timeout
    for t in _push_threads:
        t.join(timeout=max(0.1, deadline - time.time()))
    _push_threads = [t for t in _push_threads if t.is_alive()]
    logger.info("推送线程已停止（存活 %d）", len(_push_threads))


def _persist_push_failed(task, error):
    """推送失败持久化为 JSONL（审计/排障；写失败仅告警）"""
    logger = logging.getLogger("docconverter.batch")
    try:
        path = get_config().get("weknora", {}).get(
            "push_failed_path", "/data/push_failed.jsonl")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": str(task.file_info.path),
            "rel_path": task.file_info.rel_path,
            "push_original": task.push_original,
            "output_files": list(task.output_files or []),
            "error": (error or "")[:500],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("推送失败记录写入失败: %s", e)


def _record_push_failed(task, error):
    """登记推送失败任务（内存供重推 + JSONL 审计）"""
    with _push_failed_lock:
        _push_failed.append(task)
        if len(_push_failed) > 1000:
            _push_failed.pop(0)
    _persist_push_failed(task, error)


def retry_failed_pushes() -> dict:
    """将最近推送失败的任务重新入队重推（返回重推数量）"""
    with _push_failed_lock:
        tasks = list(_push_failed)
    if not tasks:
        return {"requeued": 0, "message": "无待重推的失败任务"}
    q = _get_push_queue()
    _ensure_push_workers()
    requeued = 0
    for task in tasks:
        task.weknora_push = PUSH_PENDING
        task.weknora_error = ""
        q.put(task)
        requeued += 1
    with _push_failed_lock:
        _push_failed.clear()
    return {"requeued": requeued}


def failed_push_count() -> int:
    with _push_failed_lock:
        return len(_push_failed)


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
    """推送消费者：从有界队列取任务推送到 WeKnora（多消费者之一）。

    阻塞 get 带 1s 超时以便响应停止事件；每个任务读取最新配置，
    避免配置热更新（/api/config/update）后仍连接旧地址/旧凭据。
    """
    logger = logging.getLogger("docconverter.batch")

    while not _push_stop_event.is_set():
        try:
            task = _push_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            if task.weknora_push != PUSH_PENDING:
                continue

            config = get_config()
            client = _get_push_client(config)
            if not client.is_configured():
                task.weknora_error = "WeKnora 未配置，跳过推送"
                task.weknora_push = PUSH_FAILED
                _record_push_failed(task, task.weknora_error)
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
                    _record_push_failed(task, task.weknora_error)
                    logger.error("推送失败: %s: %s", source_path, task.weknora_error)
                continue

            # 转换产物：一个源文件可能产出多个 md（CHM 章节/压缩包内文件），逐个推送
            files_to_push = task.output_files or ([md_rel_path] if task.markdown else [])
            if not files_to_push:
                task.weknora_push = PUSH_FAILED
                task.weknora_error = "无 Markdown 内容可推送"
                _record_push_failed(task, task.weknora_error)
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
                # 判定推送结果：全部成功、或全部为 409 幂等重复，均视为推送完成
                all_duplicate = (ok_count > 0 and not failed_detail
                                 and task.weknora_error)  # 有 duplicate 标记且无真失败
                if ok_count == len(files_to_push) or all_duplicate:
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
                                " (全部重复跳过)" if all_duplicate
                                else (" (含重复跳过)" if task.weknora_error else ""))
                elif ok_count > 0 and not failed_detail:
                    # 部分文件 ok（含 duplicate），无真正失败 → 也视为成功
                    task.weknora_doc_id = ",".join(doc_ids)
                    task.weknora_push = PUSH_PUSHED
                    try:
                        get_registry().mark_pushed(source_path, task.weknora_doc_id)
                    except Exception:
                        pass
                    logger.info("推送完成(部分重复): %s (%d/%d 文件)", source_path,
                                ok_count, len(files_to_push))
                else:
                    task.weknora_push = PUSH_FAILED
                    task.weknora_error = "; ".join(failed_detail[:3]) or "推送失败"
                    _record_push_failed(task, task.weknora_error)
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
                # manual 模式同样：全部成功或全部 409 幂等均视为推送完成
                all_dup = (ok_count > 0 and not failed_detail and task.weknora_error)
                if ok_count == len(files_to_push) or all_dup:
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
                                " (全部重复跳过)" if all_dup
                                else (" (含重复跳过)" if task.weknora_error else ""))
                elif ok_count > 0 and not failed_detail:
                    task.weknora_doc_id = ",".join(doc_ids)
                    task.weknora_push = PUSH_PUSHED
                    try:
                        get_registry().mark_pushed(source_path, task.weknora_doc_id)
                    except Exception:
                        pass
                    logger.info("推送完成(部分重复): %s (%d/%d 文件)", source_path,
                                ok_count, len(files_to_push))
                else:
                    task.weknora_push = PUSH_FAILED
                    task.weknora_error = "; ".join(failed_detail[:3]) or "推送失败"
                    _record_push_failed(task, task.weknora_error)
                    logger.error("推送失败: %s: %s", source_path, task.weknora_error)
        except Exception as e:
            logger.error("推送线程异常: %s", e)
            _record_push_failed(task, "推送线程异常: {}".format(e))
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
    _ensure_push_workers()
    _get_push_queue().put(task)   # 阻塞（背压）：队列满时等待消费


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


# 嗅探器的通用文本兜底（只表示"未识别出结构"，不代表真是文本）→ 不参与路由
_SNIFF_GENERIC_EXTS = {'.txt', ''}


def _pick_converter(ext: str, path: Path):
    """按扩展名取转换器；若嗅探出不同格式，返回 [嗅探转换器, 扩展名转换器] 候选链

    修复: .et/.wps 等文件可能被魔数误判（如 .et 嗅探成 .xlsx），
    嗅探转换器失败时回退到扩展名转换器。

    注意: 嗅探器对"非二进制内容"有 .txt 兜底，它只表示**没识别出结构**，
    并不代表文件真是纯文本（损坏的二进制也会走到这里）。因此该通用兜底不参与
    路由——否则会用文本转换器覆盖正确的扩展名转换器，把损坏文件读成乱码。
    """
    converter = registry.get(ext)
    sniffed_ext, sniffed_name = sniff_format(path)
    sniffed_converter = registry.get(sniffed_ext)
    if (sniffed_ext != ext and sniffed_converter is not None
            and sniffed_converter.available and sniffed_converter is not converter
            and sniffed_ext not in _SNIFF_GENERIC_EXTS):
        return [sniffed_converter, converter], sniffed_ext, sniffed_name
    return [converter], ext, None


def _convert_single(task: BatchTask, output_dir: Path, config: dict) -> BatchTask:
    """执行单个文件转换（含嗅探路由 + 多文件输出支持）"""
    logger = logging.getLogger("docconverter.batch")
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

        registry = get_registry()
        source_hash = registry.sha256_of_file(str(path))
        task.source_hash = source_hash
        rel = task.file_info.rel_path
        md_rel = str(Path(rel).with_suffix(".md"))
        rec = registry.get(str(path))
        # 增量跳过：源文件指纹与渲染版本都未变化且已有输出。
        # 渲染版本必须一起比对：转换逻辑升级后源文件哈希没变，
        # 若只看哈希，老产物会被永久跳过（v2.2.0 修丢图就靠它触发重转）。
        if (rec and rec.get("source_hash") == source_hash
                and rec.get("status") == "converted"
                and rec.get("render_version") == RENDER_VERSION):
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
                # 告知转换器输出根目录：图片/资源需要落到 MD 同级目录
                # （HTML、CHM 等带图文档依赖该属性；不支持的转换器忽略即可）
                if hasattr(cand, "assets_root"):
                    cand.assets_root = str(output_dir)
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
            # 可选：同步一份到镜像目录（备份/二次利用）
            mirror_root = (config.get("converter", {}) or {}).get("output_mirror") or ""
            if mirror_root:
                try:
                    mpath = Path(mirror_root) / out_rel
                    mpath.parent.mkdir(parents=True, exist_ok=True)
                    mpath.write_text(content, encoding="utf-8")
                except OSError as e:
                    logger.warning("输出镜像失败 %s: %s", out_rel, e)
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


def _handle_missing_sources(files: list, config: dict) -> dict:
    """源文件从源目录消失时：从知识库删除对应文档，并可选择归档源文件。

    背景：源文件被移除后，知识库里对应的旧文档若不删除会继续被检索到，
    导致已下线/废止内容仍然生效。

    安全护栏：
    - 仅在全量扫描批次调用（部分批次里"缺席"不等于"已删除"）
    - 扫描结果为 0 条时不执行（疑似挂载失败，避免误删整个知识库）
    - 单条失败不中断整批，逐条计数上报
    """
    logger = logging.getLogger("docconverter.batch")
    reg = get_registry()
    current = {str(f.path) for f in files}
    info = {"checked": True, "missing": 0, "deleted": 0, "archived": 0,
            "failed": 0, "doc_ids": 0}
    if not current:
        info["skipped"] = "empty_scan"
        logger.warning("源文件消失处理已跳过：扫描结果为 0 条（疑似源目录不可用）")
        return info

    missing = sorted(reg.all_files() - current)
    info["missing"] = len(missing)
    if not missing:
        return info

    wk_cfg = config.get("weknora", {}) or {}
    archive_dir = (config.get("archive", {}) or {}).get("dir") or ""
    client = None
    if wk_cfg.get("enabled"):
        try:
            from weknora_client import WeKnoraClient
            client = WeKnoraClient(config)
        except Exception as e:  # noqa: BLE001
            logger.warning("源文件消失处理：初始化知识库客户端失败，仅做本地清理: %s", e)

    for path in missing:
        rec = reg.get(path) or {}
        # 1) 从知识库删除（一个源文件可能产出多篇文档）
        doc_ids = []
        if rec.get("weknora_doc_id"):
            doc_ids.append(rec["weknora_doc_id"])
        for _rel, did in (rec.get("output_docs") or {}).items():
            if did and did not in doc_ids:
                doc_ids.append(did)
        info["doc_ids"] += len(doc_ids)
        if client is not None:
            for did in doc_ids:
                try:
                    client.delete_knowledge(did)
                except Exception as e:  # noqa: BLE001
                    info["failed"] += 1
                    logger.warning("源文件消失处理：删除知识失败 %s: %s", did, e)

        # 2) 归档源文件（若配置了归档目录且文件仍在）
        if archive_dir and os.path.exists(path):
            try:
                os.makedirs(archive_dir, exist_ok=True)
                dst = os.path.join(archive_dir, os.path.basename(path))
                if os.path.exists(dst):
                    dst = os.path.join(
                        archive_dir, "{}_{}".format(int(time.time()), os.path.basename(path)))
                shutil.move(path, dst)
                info["archived"] += 1
            except Exception as e:  # noqa: BLE001
                info["failed"] += 1
                logger.warning("源文件消失处理：归档失败 %s: %s", path, e)

        # 3) 清理注册表记录
        try:
            reg.delete(path)
        except Exception:
            pass
        info["deleted"] += 1

    logger.info("源文件消失处理: 缺失 %d，清理记录 %d，删除知识 %d，归档 %d，失败 %d",
                info["missing"], info["deleted"], info["doc_ids"],
                info["archived"], info["failed"])
    return info


def start_batch(files: list, progress_callback: Optional[Callable] = None,
                push_to_weknora: Optional[bool] = None,
                full_scan: bool = False) -> dict:
    """启动批量转换

    Args:
        files: list[FileInfo] 待转换文件
        progress_callback: (completed, total, task) 进度回调
        push_to_weknora: 是否推送到 WeKnora（None = 按配置 auto_push）
        full_scan: 是否为"源目录全量扫描"批次；仅此类批次才做源文件消失处理
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

    # 重置推送停止事件（允许上一轮 stop 后重新起消费者）
    _push_stop_event.clear()
    # 可选：批次前清理已删源文件的陈旧注册表记录（仅在批次恒为全量源目录扫描时安全开启）
    if bool(config.get("registry", {}).get("prune_on_batch", False)):
        try:
            get_registry().prune([str(f.path) for f in files])
        except Exception:
            pass

    # 源文件消失处理：从知识库删除对应文档（+ 可选归档源文件）。
    # 仅在全量扫描批次执行——部分批次（如上传）里"缺席"不等于"已删除"。
    missing_info = {"checked": False, "missing": 0, "deleted": 0, "archived": 0, "failed": 0}
    if full_scan and bool(config.get("registry", {}).get("handle_missing", False)):
        try:
            missing_info = _handle_missing_sources(files, config)
        except Exception as e:  # noqa: BLE001
            logger.exception("源文件消失处理失败: %s", e)
            missing_info["error"] = str(e)[:200]
    with _lock:
        _batch_state["missing_sources"] = missing_info

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
        q = _push_queue
        return {
            "status": _batch_state["status"],
            "started_at": _batch_state["started_at"],
            "finished_at": _batch_state["finished_at"],
            "statistics": _batch_state["statistics"],
            "tasks": tasks_preview,
            # 源文件消失处理结果（未启用时为 None）
            "missing_sources": _batch_state.get("missing_sources"),
            "total_tasks": len(_batch_state["tasks"]),
            "push_queue_size": q.qsize() if q is not None else 0,
            "push_failed_count": failed_push_count(),
            "push_workers": len([t for t in _push_threads if t.is_alive()]),
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
