"""
目录扫描器: 递归扫描源目录，按扩展名过滤文件
"""
import fnmatch, os
from pathlib import Path
from typing import Callable, Optional

from config import get_config


class FileInfo:
    """扫描到的文件信息"""
    __slots__ = ('path', 'rel_path', 'ext', 'size', 'mtime')

    def __init__(self, path: Path, base_dir: Path):
        self.path = path
        self.rel_path = str(path.relative_to(base_dir))
        self.ext = path.suffix.lower()
        self.size = path.stat().st_size
        self.mtime = path.stat().st_mtime

    def to_dict(self):
        return {
            "path": str(self.path),
            "rel_path": self.rel_path,
            "ext": self.ext,
            "size": self.size,
            "size_str": _format_size(self.size),
            "mtime": self.mtime,
        }


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / 1024 / 1024:.1f}MB"


def _match_exclude(path: Path, patterns: list) -> bool:
    """检查文件是否匹配排除规则"""
    name = path.name
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def scan_directory(
    source_dir: Optional[str] = None,
    recursive: Optional[bool] = None,
    include_extensions: Optional[list] = None,
    exclude_patterns: Optional[list] = None,
    progress_callback: Optional[Callable] = None,
) -> list:
    """扫描目录并返回文件列表

    Args:
        source_dir: 源目录 (默认从配置读取)
        recursive: 是否递归子目录
        include_extensions: 包含的文件扩展名列表
        exclude_patterns: 排除的文件名模式
        progress_callback: 进度回调 (current, total)

    Returns:
        list[FileInfo]
    """
    config = get_config()
    scanner_cfg = config["scanner"]
    converter_cfg = config["converter"]

    source_dir = source_dir or scanner_cfg.get("source_dir", "/data/input")
    recursive = recursive if recursive is not None else scanner_cfg.get("recursive", True)
    include_extensions = include_extensions or scanner_cfg.get("include_extensions", [])
    exclude_patterns = exclude_patterns or scanner_cfg.get("exclude_patterns", [])

    # 规范化扩展名
    include_extensions = [e.lower() if e.startswith('.') else f'.{e.lower()}'
                          for e in include_extensions]

    base = Path(source_dir)
    if not base.exists():
        return []

    files = []

    if recursive:
        iterator = base.rglob("*")
    else:
        iterator = base.glob("*")

    for path in iterator:
        if not path.is_file():
            continue

        ext = path.suffix.lower()
        if ext not in include_extensions:
            continue
        if _match_exclude(path, exclude_patterns):
            continue

        files.append(FileInfo(path, base))

    # 按路径排序
    files.sort(key=lambda f: f.rel_path)
    return files


def get_statistics(files: list) -> dict:
    """获取文件统计信息"""
    from collections import Counter
    ext_counts = Counter(f.ext for f in files)
    total_size = sum(f.size for f in files)

    return {
        "total": len(files),
        "total_size": total_size,
        "total_size_str": _format_size(total_size),
        "by_format": {ext: count for ext, count in ext_counts.most_common()},
    }
