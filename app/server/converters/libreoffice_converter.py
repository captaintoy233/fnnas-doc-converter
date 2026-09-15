"""LibreOffice 后端转换器 — 处理 .doc / .xls / .dps / .gd 等旧格式。

策略:
1. 检测 LibreOffice (soffice) 是否可用
2. 将源文件转换为 HTML（保留最多格式信息）
3. 用 html2text / markdownify 将 HTML 转为 Markdown
4. 如果 LibreOffice 不可用，回退到纯 Python 方案：
   - .doc → olefile + 启发式文本提取
   - .xls → xlrd 表格提取
   - .dps → 占位提示
   - .gd → 加密公文提示

架构:
- 遵循 Document Model 模式：convert_to_document() → Document
- convert() 保持向后兼容
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import BaseConverter
from model.document import Document
from model.block import Paragraph, Heading
from model.inline import Text
from render.markdown import document_to_markdown
from errors import ConversionError, MalformedDocumentError, EncryptedDocumentError


def _find_soffice() -> Optional[str]:
    """查找 soffice 可执行文件路径"""
    # 优先检查常见路径
    candidates = [
        "soffice",
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
        "/opt/libreoffice7.6/program/soffice",
        "/snap/bin/libreoffice",
    ]
    for path in candidates:
        if shutil.which(path):
            return shutil.which(path)
    return None


class LibreOfficeConverter(BaseConverter):
    """通过 LibreOffice headless 转换旧格式文档 → Markdown

    支持格式: .doc, .xls, .dps, .gd
    当 LibreOffice 不可用时自动回退到纯 Python 方案。
    """

    def __init__(self, soffice_path: str = ""):
        self._soffice = soffice_path or _find_soffice()
        self._html_converter = None  # lazy init

    def supported_extensions(self) -> list:
        return ['.doc', '.xls', '.dps', '.gd']

    @property
    def available(self) -> bool:
        return True  # 始终可用（有回退方案）

    @property
    def display_name(self) -> str:
        return "Legacy Office"

    @property
    def description(self) -> str:
        if self._soffice:
            return f"LibreOffice 后端 ({self._soffice})，支持 .doc/.xls/.dps"
        return "旧版 Office 格式转换器（纯 Python 回退模式）"

    def _has_libreoffice(self) -> bool:
        return self._soffice is not None and os.path.isfile(self._soffice)

    # ------------------------------------------------------------------
    # LibreOffice 转换路径
    # ------------------------------------------------------------------

    def _convert_via_lo(self, file_path: str) -> str:
        """通过 LibreOffice 将文件转为 HTML，再转 Markdown"""
        with tempfile.TemporaryDirectory(prefix="docconv_lo_") as tmpdir:
            # LibreOffice headless 转 HTML
            cmd = [
                self._soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to", "html",
                "--outdir", tmpdir,
                file_path,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120
                )
            except subprocess.TimeoutExpired:
                raise ConversionError(
                    f"LibreOffice conversion timed out for {file_path}",
                    file_path=file_path,
                )
            except FileNotFoundError:
                raise ConversionError(
                    f"LibreOffice not found at {self._soffice}",
                    file_path=file_path,
                )

            if result.returncode != 0:
                stderr = result.stderr.strip()[:200]
                raise ConversionError(
                    f"LibreOffice conversion failed: {stderr}",
                    file_path=file_path,
                )

            # 查找生成的 HTML 文件
            stem = Path(file_path).stem
            html_path = os.path.join(tmpdir, stem + ".html")
            if not os.path.exists(html_path):
                # LibreOffice 可能改变了文件名
                html_files = list(Path(tmpdir).glob("*.html"))
                if not html_files:
                    raise ConversionError(
                        "LibreOffice produced no HTML output",
                        file_path=file_path,
                    )
                html_path = str(html_files[0])

            html_content = Path(html_path).read_text(encoding="utf-8", errors="replace")
            return self._html_to_markdown(html_content)

    def _html_to_markdown(self, html: str) -> str:
        """将 HTML 转为 Markdown"""
        # 尝试使用 markdownify
        try:
            from markdownify import markdownify as md
            result = md(html, heading_style="ATX", strip=["img", "script", "style"])
            if result and len(result.strip()) > 10:
                return result.strip()
        except ImportError:
            pass

        # 回退：简单的 HTML 标签剥离
        text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</?p[^>]*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<h(\d)[^>]*>(.*?)</h\1>', lambda m: '\n' + '#' * int(m.group(1)) + ' ' + m.group(2) + '\n', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ------------------------------------------------------------------
    # 纯 Python 回退方案
    # ------------------------------------------------------------------

    def _fallback_doc(self, file_path: str) -> str:
        """纯 Python .doc 文本提取（OLE2 WordDocument 流）"""
        import olefile
        import struct

        try:
            ole = olefile.OleFileIO(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                f"Cannot open .doc file: {e}", file_path=file_path
            ) from e

        try:
            wd = ole.openstream('WordDocument').read()
        except Exception:
            ole.close()
            raise MalformedDocumentError(
                "No WordDocument stream in .doc file", file_path=file_path
            )

        ole.close()

        # 尝试 FIB 定位正文
        text = ""
        try:
            if len(wd) >= 0x20:
                flags = wd[0x0A]
                f_complex = bool(flags & 0x04)
                fc_min, fc_mac = struct.unpack_from('<II', wd, 0x18)
                if not f_complex and fc_mac > fc_min and fc_mac <= len(wd):
                    raw = wd[fc_min:fc_mac]
                    text = raw.decode('utf-16-le', errors='replace')
        except Exception:
            pass

        # 回退：全文 UTF-16LE 解码 + CJK 定位
        if len(text.strip()) < 10:
            decoded = wd.decode('utf-16-le', errors='replace')
            match = re.search(r'[\u4e00-\u9fff]{4,}', decoded)
            text = decoded[match.start():] if match else decoded

        # 清理控制字符
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _fallback_xls(self, file_path: str) -> str:
        """纯 Python .xls 表格提取（xlrd）"""
        try:
            import xlrd
        except ImportError:
            raise ConversionError(
                "xlrd not installed; cannot convert .xls files",
                file_path=file_path,
            )

        try:
            wb = xlrd.open_workbook(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                f"Cannot open .xls file: {e}", file_path=file_path
            ) from e

        doc = Document()
        for sheet in wb.sheets():
            if sheet.nrows == 0:
                continue

            # Sheet 标题
            if len(wb.sheets()) > 1:
                doc.add_block(Heading(level=2, children=[Text(sheet.name)]))

            # 构建表格
            from model.table import Table, TableRow, TableCell
            from model.inline import Text as InlineText
            table = Table()
            for row_idx in range(min(sheet.nrows, 500)):  # 限制行数
                cells = []
                for col_idx in range(sheet.ncols):
                    cell = sheet.cell(row_idx, col_idx)
                    val = str(cell.value).strip() if cell.value != '' else ''
                    # 浮点数去掉尾部 .0
                    if cell.ctype == 2 and val.endswith('.0'):
                        val = val[:-2]
                    cells.append(TableCell(children=[InlineText(val)]))
                table.rows.append(TableRow(cells=cells))

            if not table.is_empty():
                doc.add_block(table)

        return document_to_markdown(doc)

    def _fallback_dps(self, file_path: str) -> str:
        """DPS 演示文稿回退提示"""
        return (
            "**DPS 演示文稿暂不支持纯 Python 转换**\n\n"
            "该格式需要 LibreOffice 才能转换为 Markdown。"
            "请安装 LibreOffice 后重试，或先将文件转为 PPTX 格式。"
        )

    def _fallback_gd(self, file_path: str) -> str:
        """GD 加密公文回退提示"""
        return (
            "**⚠️ 该文件为金山加密公文格式（.gd）**\n\n"
            "此格式内容已加密，需要 WPS Office 解密后才能转换。\n"
            "建议：\n"
            "1. 在 WPS Office 中打开并另存为 .docx 或 .pdf\n"
            "2. 使用解密后的文件重新转换"
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        md = self.convert(file_path, **kwargs)
        # 将 Markdown 包装为 Document
        doc = Document()
        for line in md.split('\n'):
            line = line.strip()
            if not line:
                continue
            # 简单检测标题
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if heading_match:
                level = len(heading_match.group(1))
                doc.add_block(Heading(level=level, children=[Text(heading_match.group(2))]))
            else:
                doc.add_block(Paragraph(children=[Text(line)]))
        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串"""
        ext = Path(file_path).suffix.lower()

        # GD 加密公文直接返回提示（无法解密）
        if ext == '.gd':
            return self._fallback_gd(file_path)

        # 优先尝试 LibreOffice
        if self._has_libreoffice():
            try:
                return self._convert_via_lo(file_path)
            except ConversionError:
                # LO 失败时回退到纯 Python
                pass

        # 纯 Python 回退
        if ext == '.doc':
            return self._fallback_doc(file_path)
        elif ext == '.xls':
            return self._fallback_xls(file_path)
        elif ext == '.dps':
            return self._fallback_dps(file_path)
        else:
            raise ConversionError(
                f"No fallback converter for {ext}",
                file_path=file_path,
            )
