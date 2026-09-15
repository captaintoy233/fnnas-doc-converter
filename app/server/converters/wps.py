"""WPS (WPS文字) → Markdown。

OLE2 复合文档，正文位于 WordDocument 流。优先按 FIB 的 fcMin/fcMac
精确截取正文（UTF-16LE），解析失败时回退到旧版启发式提取。

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- 提取的文本行映射为 Paragraph blocks
- OLE 打开失败时抛出 MalformedDocumentError

向后兼容:
- convert() 方法保持原有签名和返回类型
- 新增 convert_to_document() 返回 Document Model
"""
import olefile
import re
import struct
from . import BaseConverter

# Document Model 导入
from model.document import Document
from model.block import Paragraph
from model.inline import Text
from render.markdown import document_to_markdown
from errors import MalformedDocumentError


class WPSConverter(BaseConverter):
    """WPS文字 → MD (OLE2 WordDocument 流 + FIB 正文定位)"""

    def supported_extensions(self) -> list:
        return ['.wps']

    def _extract_fib_text(self, wd: bytes) -> str:
        """按 FIB 字段 fcMin/fcMac 截取正文（UTF-16LE）

        FIB 布局: 0x18 起 fcMin(4B), 0x1C 起 fcMac(4B);
        0x0A 字节的 bit2 为 fComplex（正文可能分片存储）。
        """
        try:
            if len(wd) < 0x20:
                return ""
            flags = wd[0x0A]
            f_complex = bool(flags & 0x04)
            fc_min, fc_mac = struct.unpack_from('<II', wd, 0x18)
            if f_complex or fc_mac <= fc_min or fc_mac > len(wd):
                return ""
            raw = wd[fc_min:fc_mac]
            text = raw.decode('utf-16-le', errors='replace')
            # 去掉尾部空字符与分隔符
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]+$', '', text)
            return text
        except Exception:
            return ""

    def _extract_heuristic(self, wd: bytes) -> str:
        """旧版启发式：整流解码后定位首个连续 CJK 段落"""
        text = wd.decode('utf-16-le', errors='replace')
        start = re.search(r'[\u4e00-\u9fff]{4,}', text)
        text = text[start.start():] if start else text
        return text

    def _extract_text(self, file_path: str) -> str:
        """从 WPS 文件中提取纯文本"""
        try:
            ole = olefile.OleFileIO(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                f"Failed to open WPS OLE file: {e}",
                file_path=file_path
            ) from e
        try:
            wd = ole.openstream('WordDocument').read()
        except Exception as e:
            raise MalformedDocumentError(
                f"Failed to read WordDocument stream: {e}",
                file_path=file_path
            ) from e
        finally:
            ole.close()

        text = self._extract_fib_text(wd)
        if len(text.strip()) < 4:
            text = self._extract_heuristic(wd)

        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 合并多余空行，压缩行间空白
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        text = self._extract_text(file_path)
        doc = Document()

        # Split text into lines and create Paragraph blocks
        for line in text.split('\n'):
            line = line.strip()
            if line:
                doc.add_block(Paragraph(children=[Text(line)]))

        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
