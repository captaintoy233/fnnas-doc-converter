"""纯文本类格式 → Markdown

- `.md`  : **原样透传**（已是 Markdown，经解析器再走一遍只会损伤格式）
- `.txt` : 按行成段，保留空行结构
- `.csv` : 转为 GitHub 风格 Markdown 表格（正确处理引号包裹与内嵌换行）

这些格式在旧版服务里是"直通上传"，本转换器把 .txt/.csv 规范化为 Markdown，
.md 则保持字节级不变，从而与统一管线（输出 .md → 推送知识库）兼容。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import chardet

from . import BaseConverter
from model.document import Document
from model.block import Paragraph
from model.inline import Text
from errors import MalformedDocumentError


class TextConverter(BaseConverter):
    """纯文本类 → MD（.md 透传 / .txt 分段 / .csv 表格）"""

    def supported_extensions(self) -> list:
        return ['.md', '.txt', '.csv']

    @property
    def display_name(self) -> str:
        return "文本文件"

    def _read_text(self, file_path: str) -> str:
        try:
            raw = Path(file_path).read_bytes()
        except OSError as e:
            raise MalformedDocumentError(
                "无法读取文本文件: {}".format(e), file_path=file_path) from e
        if not raw:
            return ""
        enc = chardet.detect(raw).get("encoding", "utf-8") or "utf-8"
        # GBK 内容常被 chardet 误判，做一次回退
        try:
            return raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _csv_to_markdown(text: str) -> str:
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        out = []
        for i, row in enumerate(rows):
            cells = [(c or "").replace("\\", "\\\\").replace("|", "\\|")
                     .replace("\r\n", "<br>").replace("\n", "<br>").strip()
                     for c in row]
            cells += [""] * (width - len(cells))
            out.append("| " + " | ".join(cells) + " |")
            if i == 0:
                out.append("| " + " | ".join(["---"] * width) + " |")
        return "\n".join(out)

    def convert(self, file_path: str, **kwargs) -> str:
        ext = Path(file_path).suffix.lower()
        text = self._read_text(file_path)
        if ext == ".md":
            # 已是 Markdown：原样返回，避免二次渲染损伤
            return text
        if ext == ".csv":
            return self._csv_to_markdown(text)
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型（pragmatic：整体作为一个段落）"""
        doc = Document()
        content = self.convert(file_path, **kwargs)
        if content.strip():
            doc.add_block(Paragraph(children=[Text(content)]))
        return doc

    def convert_to_files(self, file_path: str, rel_path: str) -> list:
        """输出路径需避免"同名的不同格式互相覆盖"

        例如 `报告.md` / `报告.txt` / `报告.csv` 若都输出为 `报告.md`，
        后写的会覆盖先写的。故非 .md 源保留原扩展名：`报告.txt.md`。
        """
        p = Path(rel_path)
        ext = p.suffix.lower()
        if ext == ".md":
            out_rel = str(p)                     # 本来就是 .md，名字不变
        else:
            out_rel = str(p.with_name(p.stem + ext + ".md"))
        return [(out_rel, self.convert(file_path))]
