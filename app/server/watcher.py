"""
文件监听器: 基于 watchdog 监听源目录变化，自动触发转换
"""
import os, time, threading, logging
from pathlib import Path
from typing import Optional, Callable

from config import get_config
from scanner import scan_directory, FileInfo

logger = logging.getLogger("docconverter.watcher")

# 全局状态
_watcher_state = {
    "running": False,
    "thread": None,
    "last_scan": None,
    "known_files": set(),
    "events_processed": 0,
}

_lock = threading.Lock()


def _poll_loop(on_new_files: Callable, stop_event: threading.Event):
    """轮询模式：定期扫描目录检测新文件"""
    config = get_config()
    scanner_cfg = config["watcher"]
    interval = config["scanner"]["poll_interval"]

    known = set()
    source = Path(scanner_cfg["source_dir"])
    source.mkdir(parents=True, exist_ok=True)

    while not stop_event.is_set():
        try:
            files = scan_directory(
                source_dir=str(source),
                recursive=scanner_cfg.get("recursive", True),
            )
            current = {f.path for f in files}
            new = current - known

            if new:
                new_files = [f for f in files if f.path in new]
                logger.info(f"发现 {len(new_files)} 个新文件")
                if on_new_files:
                    on_new_files(new_files)
                _watcher_state["events_processed"] += len(new_files)

            known = current
            _watcher_state["last_scan"] = time.time()

        except Exception as e:
            logger.error(f"扫描异常: {e}")

        stop_event.wait(interval)


def start_watcher(on_new_files: Optional[Callable] = None):
    """启动文件监听（轮询模式，无需 watchdog 库）"""
    global _watcher_state

    with _lock:
        if _watcher_state["running"]:
            return {"status": "already_running"}

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_poll_loop,
            args=(on_new_files, stop_event),
            daemon=True,
        )
        thread.start()

        _watcher_state["running"] = True
        _watcher_state["thread"] = thread
        _watcher_state["stop_event"] = stop_event
        _watcher_state["known_files"] = set()
        _watcher_state["events_processed"] = 0

        return {"status": "started"}


def stop_watcher():
    """停止文件监听"""
    global _watcher_state
    with _lock:
        if not _watcher_state["running"]:
            return {"status": "not_running"}
        _watcher_state["stop_event"].set()
        _watcher_state["running"] = False
        return {"status": "stopped"}


def get_watcher_status() -> dict:
    """获取监听器状态"""
    with _lock:
        return {
            "running": _watcher_state["running"],
            "last_scan": _watcher_state["last_scan"],
            "events_processed": _watcher_state["events_processed"],
        }
