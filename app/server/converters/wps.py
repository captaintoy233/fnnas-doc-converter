"""WPS (WPS文字) → Markdown。

OLE2 复合文档，正文位于 WordDocument 流。优先按 FIB 的 fcMin/fcMac
精确截取正文（UTF-16LE），解析失败时回退到旧版启发式提取。
"""
import olefile
import re
import struct
from . import BaseConverter


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

    def convert(self, file_path: str, **kwargs) -> str:
        ole = olefile.OleFileIO(file_path)
        try:
            wd = ole.openstream('WordDocument').read()
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
