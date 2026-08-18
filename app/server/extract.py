"""
压缩包解压工具: zip / tar(.gz/.bz2/.xz) / rar / 7z / 单文件 gz/bz2/xz

- zip/tar 用标准库；rar/7z 需要外部 7zz/7z（配置 archive.seven_zip_path）
- 内置 zip-slip 防护：解压路径必须落在目标目录内
"""
import gzip
import bz2
import lzma
import logging
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

logger = logging.getLogger("docconverter.extract")

# 归档扩展名（容器类：解压后可能含多个可转换文件）
ARCHIVE_EXTS = ['.zip', '.rar', '.7z', '.tar', '.gz', '.tgz',
                '.bz2', '.tbz2', '.xz', '.txz']

# 容器类归档（内部可能包含目录/多文件）
CONTAINER_EXTS = ['.zip', '.rar', '.7z', '.tar', '.tgz', '.tbz2', '.txz']
# 单文件压缩（解压后是单个文件）
SINGLE_EXTS = ['.gz', '.bz2', '.xz']

# 解压配额（防 zip 炸弹）：总大小 / 条目数
DEFAULT_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024   # 2GB
DEFAULT_MAX_EXTRACT_ENTRIES = 100000


def _extract_limits(config=None):
    if config is None:
        from config import get_config
        config = get_config()
    arc = (config.get("archive", {}) or {})
    mb = int(arc.get("max_extract_mb", 0) or 0)
    max_bytes = mb * 1024 * 1024 if mb > 0 else DEFAULT_MAX_EXTRACT_BYTES
    max_entries = int(arc.get("max_extract_entries", 0) or 0) or DEFAULT_MAX_EXTRACT_ENTRIES
    return max_bytes, max_entries


class ExtractQuotaExceeded(RuntimeError):
    pass


def _safe_target(dest: Path, member_path: str) -> Path:
    """防 zip-slip：确保目标在 dest 内"""
    target = (dest / member_path).resolve()
    dest_resolved = dest.resolve()
    try:
        target.relative_to(dest_resolved)
    except ValueError:
        raise RuntimeError("归档条目路径越界: {}".format(member_path))
    return target


def _fix_zip_name(name: str, flag_bits: int) -> str:
    """修复 Windows zip 的 GBK 文件名乱码

    zipfile 对未设置 UTF-8 标志(bit 11)的条目名按 cp437 解码，
    中文 Windows 创建的文件名实为 GBK 编码，需 cp437→bytes→GBK 还原。
    """
    if flag_bits & 0x800:
        return name
    if all(ord(c) < 128 for c in name):
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except Exception:
        return name


def extract_zip(archive: Path, dest: Path, config=None) -> list:
    max_bytes, max_entries = _extract_limits(config)
    out = []
    count = 0
    size_acc = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            count += 1
            if count > max_entries:
                raise ExtractQuotaExceeded(
                    "归档条目数超过上限 {}，已中止（archive.max_extract_entries）".format(max_entries))
            size_acc += info.file_size
            if size_acc > max_bytes:
                raise ExtractQuotaExceeded("归档解压总大小超过配额，已中止")
            name = _fix_zip_name(info.filename, info.flag_bits)
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = _safe_target(dest, safe_rel_path(name))
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(str(target))
    return out


def extract_tar(archive: Path, dest: Path, config=None) -> list:
    max_bytes, max_entries = _extract_limits(config)
    out = []
    count = 0
    size_acc = 0
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            count += 1
            if count > max_entries:
                raise ExtractQuotaExceeded(
                    "归档条目数超过上限 {}，已中止（archive.max_extract_entries）".format(max_entries))
            size_acc += member.size
            if size_acc > max_bytes:
                raise ExtractQuotaExceeded("归档解压总大小超过配额，已中止")
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                continue
            target = _safe_target(dest, safe_rel_path(member.name))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(str(target))
    return out


def extract_single(archive: Path, dest: Path, compression: str, config=None) -> list:
    """解压单文件压缩（.gz/.bz2/.xz），带解压配额（防单文件炸弹）"""
    max_bytes, _ = _extract_limits(config)
    target = dest / archive.name
    # 去掉压缩后缀，如 report.pdf.gz -> report.pdf
    for ext in SINGLE_EXTS:
        if archive.name.lower().endswith(ext):
            target = dest / archive.name[: -len(ext)]
            break
    target.parent.mkdir(parents=True, exist_ok=True)
    decompress = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[compression]
    written = 0
    with decompress(archive, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                dst.close()
                try:
                    target.unlink()
                except OSError:
                    pass
                raise ExtractQuotaExceeded("单文件解压超过配额，已中止")
            dst.write(chunk)
    return [str(target)]


def safe_component(name: str, max_bytes: int = 180) -> str:
    """文件名安全化：截断超过 max_bytes 的路径组件（保留扩展名）"""
    b = name.encode("utf-8")
    if len(b) <= max_bytes:
        return name
    ext = Path(name).suffix
    keep = max_bytes - len(ext.encode("utf-8")) - 1
    stem = name[: -len(ext)] if ext else name
    while len(stem.encode("utf-8")) > keep and stem:
        stem = stem[:-1]
    return stem + ext


def safe_rel_path(rel: str) -> str:
    """对相对路径的每个组件做安全化（防超长文件名）"""
    parts = [safe_component(p) for p in rel.replace("\\", "/").split("/")]
    return "/".join(parts)


def _safe_exists(p) -> bool:
    """安全判断路径是否存在（超长文件名路径会抛 OSError，视为不存在）"""
    try:
        return Path(p).exists()
    except OSError:
        return False


def make_temp(prefix: str) -> str:
    """在配置的 temp_dir 下创建临时目录（默认系统 /tmp）"""
    import tempfile as _tf
    try:
        from config import get_config
        base = get_config().get("converter", {}).get("temp_dir") or None
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        return _tf.mkdtemp(prefix=prefix, dir=base)
    except Exception:
        return _tf.mkdtemp(prefix=prefix)


def list_7z_entries(archive: Path, seven_zip: str) -> list:
    """列出 7z 归档内条目（-slt 技术模式，仅元数据，快）"""
    try:
        proc = subprocess.run(
            [seven_zip, "l", "-slt", str(archive)],
            capture_output=True, text=True, timeout=600,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    entries = []
    path, is_dir = None, False
    for line in proc.stdout.splitlines():
        line = line.rstrip("\r")
        if line.startswith("Path = "):
            path = line[len("Path = "):]
        elif line.startswith("Folder = "):
            is_dir = line[len("Folder = "):] == "+"
        elif line == "":
            if path is not None and not is_dir:
                entries.append(path)
            path, is_dir = None, False
    if path is not None and not is_dir:
        entries.append(path)
    return entries


def extract_7z(archive: Path, dest: Path, seven_zip: str) -> list:
    """外部 7zz/7z 解压 rar/7z 等

    7z 遇到超长文件名（如 CHM 中 200+ 字节的 CJK 文件名）会报错退出
    （exit 2），但通常已提取完其余全部文件。因此:
    1. 解压后不因 exit!=0 直接失败（除非一个文件都没提取出来）
    2. 与归档条目清单比对，缺失条目用 `7z e -so` 逐条补齐（安全文件名）
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [seven_zip, "x", "-y", "-o{}".format(dest), str(archive)],
        capture_output=True, text=True, timeout=900,
    )

    extracted_files = {str(p) for p in dest.rglob("*") if p.is_file()}
    if not extracted_files:
        raise RuntimeError("7zz 解压失败: {}".format((proc.stderr or proc.stdout)[-300:]))
    if proc.returncode >= 2:
        logger.warning("7z 解压有错误(exit=%s)，将补齐缺失条目: %s",
                       proc.returncode, (proc.stderr or proc.stdout)[-200:])

    # 防御: 校验所有解压产物都落在 dest 内（防 7z 变体写越界）
    dest_resolved = dest.resolve()
    for p in list(extracted_files):
        try:
            Path(p).resolve().relative_to(dest_resolved)
        except ValueError:
            try:
                Path(p).unlink()
            except OSError:
                pass
            logger.warning("7z 解压产物越界，已删除: %s", p)

    entries = list_7z_entries(archive, seven_zip)
    missing = []
    for e in entries:
        rel = e.replace("\\", "/")
        if not _safe_exists(dest / rel) and not _safe_exists(dest / safe_rel_path(rel)):
            missing.append(e)
    if missing:
        from concurrent.futures import ThreadPoolExecutor

        def _one(entry):
            try:
                p2 = subprocess.run(
                    [seven_zip, "e", str(archive), entry, "-so"],
                    capture_output=True, timeout=300,
                )
                if p2.returncode == 0 and p2.stdout:
                    target = dest / safe_rel_path(entry.replace("\\", "/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(p2.stdout)
                    return 1
            except Exception:
                pass
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            recovered = sum(pool.map(_one, missing))
        if recovered < len(missing):
            logger.warning("7z 解压仍有 %d 个条目缺失（超长文件名等）",
                           len(missing) - recovered)
    return [str(p) for p in dest.rglob("*") if p.is_file()]


def extract_archive(archive_path: str, dest_dir: str, config=None) -> list:
    """解压归档到 dest_dir，返回解压出的文件列表（绝对路径）"""
    if config is None:
        from config import get_config
        config = get_config()
    archive = Path(archive_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    name = archive.name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return extract_tar(archive, dest)
    if name.endswith(".tar.bz2") or name.endswith(".tbz2"):
        return extract_tar(archive, dest)
    if name.endswith(".tar.xz") or name.endswith(".txz"):
        return extract_tar(archive, dest)
    if name.endswith(".tar"):
        return extract_tar(archive, dest)

    ext = archive.suffix.lower()
    if ext == ".zip":
        return extract_zip(archive, dest, config)
    if ext in SINGLE_EXTS:
        return extract_single(archive, dest, ext, config)
    if ext in (".rar", ".7z"):
        seven_zip = (config.get("archive", {}) or {}).get("seven_zip_path", "7zz")
        found = shutil.which(seven_zip) or shutil.which("7z")
        if not found:
            raise RuntimeError(
                "解压 {} 需要 7zz/7z，未找到可执行文件。\n"
                "请安装 7-Zip 并设置 archive.seven_zip_path。".format(ext))
        return extract_7z(archive, dest, found)

    raise RuntimeError("不支持的归档格式: {}".format(ext))
