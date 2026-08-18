"""
路径工具: Windows 路径 → 本地路径解析

场景: 源目录/输出目录配置为 Windows 路径
（如 C:\\Users\\Administrator\\Documents\\AI），
而 DocConverter 运行在 Linux 容器（目录通过 SMB 挂载）或 Windows 原生。

解析规则（Linux 上运行）:
1. 配置 converter.drive_map: {"C:": "/mnt/c", ...} 显式映射盘符
2. 未配置时按约定 /mnt/<盘符小写> 推断（如 C: -> /mnt/c）
3. UNC 路径 \\\\server\\share 按 /mnt/server/share 推断

Windows 原生运行（os.name == 'nt'）时直接使用 pathlib 原生解析。
"""
import os
import re
from pathlib import Path
from typing import Optional

_WIN_ABS_RE = re.compile(r'^([A-Za-z]):[\\/](.*)$')
_UNC_RE = re.compile(r'^[\\/]{2}([^\\/]+)[\\/](.*)$')


def _drive_map(config: Optional[dict] = None) -> dict:
    if config is None:
        from config import get_config
        config = get_config()
    conv = (config.get("converter") or {})
    dm = conv.get("drive_map") or {}
    return {str(k).rstrip(":/\\").lower(): str(v).rstrip("/")
            for k, v in dm.items()}


def to_local_path(p: str, config: Optional[dict] = None) -> Path:
    """将配置中的路径（Windows 或 POSIX）解析为本地绝对路径"""
    if not p:
        return Path(".")
    p = str(p).strip()

    # Windows 原生运行：直接使用
    if os.name == "nt":
        return Path(p)

    # 已存在的 POSIX 路径（含相对路径）
    if not _WIN_ABS_RE.match(p) and not _UNC_RE.match(p) and "\\" not in p:
        return Path(p)

    drive_map = _drive_map(config)

    m = _WIN_ABS_RE.match(p)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        base = drive_map.get(drive) or "/mnt/{}".format(drive)
        return Path(base) / rest

    m = _UNC_RE.match(p)
    if m:
        server = m.group(1)
        rest = m.group(2).replace("\\", "/")
        base = drive_map.get(server.lower()) or "/mnt/{}".format(server.lower())
        return Path(base) / rest

    # 含反斜杠的相对路径
    return Path(p.replace("\\", "/"))


def resolve_win(p: str, config: Optional[dict] = None) -> Path:
    """别名：to_local_path"""
    return to_local_path(p, config)


def display_path(p) -> str:
    """展示用：本地路径转回可读形式（不做转换）"""
    return str(p)


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print("{} -> {}".format(arg, to_local_path(arg)))
