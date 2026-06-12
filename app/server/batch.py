"""
批量处理器: 队列管理、并行转换、进度追踪、重试机制
"""
import os, time, threading, json, shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_config
from scanner import FileInfo
from converters import registry


# 任务状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# 全局状态
_batch_state = {
    "status": "idle",         # idle | running | stopping | done
    "tasks": [],              # list[BatchTask]
    "started_at": None,
    "finished_at": None,
    "statistics": {},
}

_lock = threading.Lock()


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

    def to_dict(self):
        return {
            "rel_path": self.file_info.rel_path,
            "ext": self.file_info.ext,
            "size": self.file_info.size,
            "size_str": self.file_info.size_str if hasattr(self.file_info, 'size_str') else "",
            "status": self.status,
            "converter": self.converter or "",
            "result_length": self.result_length,
            "output_path": str(self.output_path) if self.output_path else None,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "retry_count": self.retry_count,
        }


def _convert_single(task: BatchTask, output_dir: Path, config: dict) -> BatchTask:
    """执行单个文件转换"""
    task.started_at = datetime.now().isoformat()
    task.status = STATUS_RUNNING

    try:
        ext = task.file_info.ext
        converter = registry.get(ext)
        if not converter:
            task.status = STATUS_SKIPPED
            task.error = f"不支持的格式: {ext}"
            return task

        task.converter = converter.display_name
        result = converter.convert(str(task.file_info.path))
        task.result_length = len(result)
        task.markdown = result

        # 写入输出文件
        rel = task.file_info.rel_path
        out_path = output_dir / rel
        md_path = out_path.with_suffix('.md')
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(result, encoding='utf-8')
        task.output_path = str(md_path)

        task.status = STATUS_SUCCESS
    except Exception as e:
        task.status = STATUS_FAILED
        task.error = str(e)
    finally:
        task.finished_at = datetime.now().isoformat()

    return task


def start_batch(files: list, progress_callback: Optional[Callable] = None) -> dict:
    """启动批量转换

    Args:
        files: list[FileInfo] 待转换文件
        progress_callback: (completed, total, task) 进度回调

    Returns:
        dict: 批次状态
    """
    global _batch_state

    with _lock:
        if _batch_state["status"] == "running":
            return {"error": "已有批次在运行中"}

        config = get_config()
        output_dir = Path(config["converter"]["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        tasks = [BatchTask(f) for f in files]
        workers = config["batch"]["workers"]
        retry_count = config["batch"]["retry_count"]
        retry_delay = config["batch"]["retry_delay"]

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

    # 使用线程池执行
    pending_tasks = list(tasks)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while pending_tasks and _batch_state["status"] != "stopping":
            # 提交一批任务
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
                    task.error = str(e)

                # 重试逻辑
                if task.status == STATUS_FAILED and task.retry_count < retry_count:
                    task.retry_count += 1
                    time.sleep(retry_delay)
                    try:
                        task = _convert_single(task, output_dir, config)
                    except Exception as e:
                        task.status = STATUS_FAILED
                        task.error = str(e)

                # 更新统计
                with _lock:
                    stats = _batch_state["statistics"]
                    stats["completed"] += 1
                    if task.status == STATUS_SUCCESS:
                        stats["success"] += 1
                    elif task.status == STATUS_FAILED:
                        stats["failed"] += 1
                    elif task.status == STATUS_SKIPPED:
                        stats["skipped"] += 1

                if progress_callback:
                    progress_callback(stats["completed"], stats["total"], task)

    # 完成
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
        tasks_preview = [t.to_dict() for t in _batch_state["tasks"][:50]]
        return {
            "status": _batch_state["status"],
            "started_at": _batch_state["started_at"],
            "finished_at": _batch_state["finished_at"],
            "statistics": _batch_state["statistics"],
            "tasks": tasks_preview,
            "total_tasks": len(_batch_state["tasks"]),
        }


def clear_batch():
    """清除批次结果"""
    global _batch_state
    with _lock:
        _batch_state = {
            "status": "idle",
            "tasks": [],
            "started_at": None,
            "finished_at": None,
            "statistics": {},
        }
