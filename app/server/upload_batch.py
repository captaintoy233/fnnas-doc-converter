"""
upload_batch.py — 本地文件批量上传（流式写入 incoming 目录）
支持多文件并行上传、同名去重（存在则跳过或覆盖）、返回每个文件的结果。
"""
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 上传状态（内存中追踪，供前端轮询）
_upload_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "bytes_total": 0,
    "bytes_done": 0,
    "current": "",
    "started_at": 0,
    "finished_at": 0,
    "errors": [],
}


def get_upload_status() -> dict:
    """返回当前上传任务状态"""
    s = _upload_state
    elapsed = (s["finished_at"] - s["started_at"]) if s["finished_at"] and s["started_at"] else 0
    return {
        "running": s["running"],
        "total": s["total"],
        "done": s["done"],
        "failed": s["failed"],
        "skipped": s["skipped"],
        "bytes_total": s["bytes_total"],
        "bytes_done": s["bytes_done"],
        "current": s["current"],
        "pct": round(s["bytes_done"] * 100.0 / s["bytes_total"], 1) if s["bytes_total"] else 0.0,
        "elapsed_sec": round(elapsed, 1),
        "errors": s["errors"][-20:],
    }


def _reset_state(total: int, bytes_total: int):
    s = _upload_state
    s.update({
        "running": True,
        "total": total,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "bytes_total": bytes_total,
        "bytes_done": 0,
        "current": "",
        "started_at": time.time(),
        "finished_at": 0,
        "errors": [],
    })


def upload_files(files: list, target_dir: str, overwrite: bool = False, chunk_size: int = 1024 * 1024):
    """
    流式写入多个上传文件到 target_dir。
    files: [(filename, file_obj)] —— file_obj 是 async 迭代器或类文件对象
    返回: {ok, uploaded, skipped, failed, errors, target_dir}
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    file_sizes = {}
    for name, f in files:
        try:
            # 尝试读取大小（可能失败）
            sz = getattr(f, "size", 0) or 0
            if hasattr(f, "seek") and sz == 0:
                try:
                    f.seek(0, os.SEEK_END)
                    sz = f.tell()
                    f.seek(0)
                except Exception:
                    sz = 0
            file_sizes[name] = sz
            total_bytes += sz
        except Exception:
            file_sizes[name] = 0

    _reset_state(len(files), total_bytes)
    uploaded, skipped, failed = [], [], []
    errors = []

    for name, f in files:
        _upload_state["current"] = name
        safe_name = os.path.basename(name.replace("\\", "/"))
        if not safe_name:
            safe_name = "upload_{}".format(int(time.time()))
        dest = target / safe_name

        # 同名处理
        if dest.exists():
            if overwrite:
                try:
                    dest.unlink()
                except Exception:
                    pass
            else:
                _upload_state["skipped"] += 1
                skipped.append(safe_name)
                logger.info("跳过已存在: %s", safe_name)
                continue

        try:
            written = 0
            with open(dest, "wb") as out:
                # UploadFile 有同步 .file (SpooledTemporaryFile) 可直接读
                src = getattr(f, "file", None)
                while True:
                    chunk = src.read(chunk_size) if src else b""
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    _upload_state["bytes_done"] += len(chunk)
            uploaded.append({"name": safe_name, "size": written})
            _upload_state["done"] += 1
        except Exception as e:
            _upload_state["failed"] += 1
            errors.append("{}: {}".format(safe_name, str(e)))
            logger.error("上传失败 %s: %s", safe_name, e)
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass

    _upload_state["running"] = False
    _upload_state["finished_at"] = time.time()
    return {
        "ok": True,
        "target_dir": str(target),
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "total": len(files),
        "done_count": _upload_state["done"],
        "skipped_count": len(skipped),
        "failed_count": len(failed),
    }
