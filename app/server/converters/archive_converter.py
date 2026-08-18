"""
压缩包 → Markdown 转换器

流程: 解压（zip/tar/7z/rar）→ 递归扫描内部可转换文件 → 逐文件转换 →
输出到 <归档名>/<内部相对路径>.md，完整保留压缩包内部目录结构。
支持嵌套归档（深度受配置限制）。
"""
import logging
import re
import shutil
import tempfile
from pathlib import Path

from . import BaseConverter
from extract import extract_archive, ARCHIVE_EXTS, CONTAINER_EXTS, SINGLE_EXTS, make_temp
from sniffer import sniff_format
from config import get_config

logger = logging.getLogger("docconverter.archive")

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.svg'}


class ArchiveConverter(BaseConverter):
    """归档 → MD (解压后逐文件转换，保留内部目录结构)"""

    def supported_extensions(self) -> list:
        return ARCHIVE_EXTS

    @property
    def display_name(self) -> str:
        return "归档"

    def _inner_files(self, base: Path, depth: int, max_depth: int) -> list:
        """递归收集可转换文件（含嵌套归档）"""
        from scanner import scan_directory
        from converters import registry
        config = get_config()
        include = [e.lower() if e.startswith('.') else '.' + e.lower()
                   for e in config["scanner"].get("include_extensions", [])]
        include += ARCHIVE_EXTS
        files = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in _IMAGE_EXTS:
                continue
            if ext in include:
                files.append(p)
        return files

    def _convert_inner(self, inner: Path, rel_inner: str, archive_stem: str,
                       depth: int, max_depth: int, out_prefix: str,
                       seen: set) -> list:
        """转换内部单个文件，返回 [(rel_output, content)]"""
        results = []
        ext = inner.suffix.lower()

        # 嵌套归档
        if ext in CONTAINER_EXTS or (ext in SINGLE_EXTS and
                                     inner.name.lower().endswith(tuple(SINGLE_EXTS))):
            if depth >= max_depth:
                logger.warning("归档嵌套过深，跳过: %s", inner)
                return results
            tmp = make_temp("nested_")
            try:
                try:
                    extracted = extract_archive(str(inner), tmp)
                except Exception as e:
                    results.append(("{}/_转换失败-{}.md".format(
                        archive_stem, rel_inner.replace("/", "_")),
                        "> 嵌套归档 {} 解压失败: {}\n".format(inner.name, e)))
                    return results
                for ef in extracted:
                    ef_path = Path(ef)
                    try:
                        ef_rel = ef_path.relative_to(tmp)
                    except ValueError:
                        continue
                    results.extend(self._convert_inner(
                        ef_path, str(ef_rel), archive_stem, depth + 1,
                        max_depth, out_prefix, seen))
                return results
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        # 常规文件：嗅探 + 路由（带回退链）
        from converters import registry
        ext_converter = registry.get(ext)
        sniffed_ext, _ = sniff_format(str(inner))
        # 候选顺序: 嗅探出的转换器优先（防扩展名伪装），失败回退扩展名转换器
        candidates = []
        if sniffed_ext != ext:
            alt = registry.get(sniffed_ext)
            if alt is not None and alt.available and alt is not ext_converter:
                candidates.append(alt)
        if ext_converter is not None and ext_converter.available:
            candidates.append(ext_converter)
        if not candidates:
            return results
        if str(inner) in seen:
            return results
        seen.add(str(inner))

        last_err = None
        for converter in candidates:
            try:
                outputs = converter.convert_to_files(str(inner), rel_inner)
                if not outputs:
                    continue
                for out_rel, content in outputs:
                    full_rel = "{}/{}".format(archive_stem, out_rel)
                    if full_rel in seen:
                        full_rel = "{}/{}".format(full_rel, "_dup")
                    results.append((full_rel, content))
                return results
            except Exception as e:
                last_err = e
                logger.warning("归档内文件转换失败(%s→%s) %s: %s",
                               converter.display_name, sniffed_ext, inner, e)
                continue
        logger.warning("归档内文件全部转换器失败 %s: %s", inner, last_err)
        results.append((
            "{}/_转换失败-{}.md".format(archive_stem, rel_inner.replace("/", "_")),
            "> 文件 {} 转换失败: {}\n".format(inner.name, last_err)))
        return results

    def convert_to_files(self, file_path: str, rel_path: str) -> list:
        config = get_config()
        max_depth = int((config.get("archive", {}) or {}).get("max_depth", 3))
        archive_stem = Path(rel_path).stem
        # 归档名清理
        archive_stem = re.sub(r'[\\/:*?"<>|]+', "_", archive_stem) or "归档"

        tmp = make_temp("archive_")
        try:
            extracted = extract_archive(file_path, tmp)
            base = Path(tmp)
            outputs = []
            seen = set()
            for ef in extracted:
                ef_path = Path(ef)
                try:
                    ef_rel = str(ef_path.relative_to(base))
                except ValueError:
                    continue
                outputs.extend(self._convert_inner(
                    ef_path, ef_rel, archive_stem, 1, max_depth, "", seen))

            # 归档清单（记录结构与统计）
            manifest_lines = [
                "# 归档清单: {}\n".format(Path(file_path).name),
                "",
                "> 本文件由压缩包解压转换生成，保留内部目录结构。",
                "",
                "| 源文件 | 状态 |",
                "|---|---|",
            ]
            for ef in sorted(extracted):
                manifest_lines.append("| {} | 已处理 |".format(
                    str(Path(ef).relative_to(base)).replace("\\", "/")))
            manifest = "\n".join(manifest_lines) + "\n"
            outputs.append(("{}/_归档清单.md".format(archive_stem), manifest))
            return outputs
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def convert(self, file_path: str, **kwargs) -> str:
        """API 单文件转换：返回归档清单摘要"""
        outputs = self.convert_to_files(file_path, Path(file_path).name)
        total_chars = sum(len(c) for _, c in outputs)
        lines = [
            "# 归档: {}\n".format(Path(file_path).name),
            "",
            "共生成 {} 个 Markdown 文件，合计 {} 字符。\n".format(len(outputs), total_chars),
            "| 输出文件 | 大小 |",
            "|---|---|",
        ]
        for out_rel, content in outputs:
            lines.append("| {} | {} 字符 |".format(out_rel, len(content)))
        return "\n".join(lines)
