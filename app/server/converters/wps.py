"""WPS (WPS文字) → Markdown。

OLE2 复合文档，正文位于 WordDocument 流。优先按 **piece table**（FIB→CLX→
PlcPcd）逐片解码真正正文（兼容 fComplex 分片存储）；piece table 不可用时回退
FIB fcMin/fcMac 直读，再回退旧版启发式。

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

    def _extract_piece_table(self, wd: bytes, tbl: bytes) -> str:
        """按 piece table（FIB→CLX→PlcPcd）逐片解码真正正文。

        Word 二进制格式把正文拆成若干 piece，每片在 PCD 里记录文件偏移与
        压缩标志（compressed→cp1252 单字节，否则 UTF-16LE）。只取这些正文片，
        不会把格式二进制数据当文本——这正是旧 fcMin/fcMac+启发式对 fComplex
        文档产生尾部乱码的根因。任何解析失败返回 "" 交由回退链处理。
        """
        try:
            if not tbl or len(wd) < 0x01A6:
                return ""
            fc_clx, lcb_clx = struct.unpack_from('<II', wd, 0x01A2)
            clx = tbl[fc_clx:fc_clx + lcb_clx]
            pos = 0
            while pos < len(clx) and clx[pos] == 0x01:  # 跳过 Prc
                cb = struct.unpack_from('<H', clx, pos + 1)[0]
                pos += 3 + cb
            if pos >= len(clx) or clx[pos] != 0x02:  # 需要 Pcdt
                return ""
            lcb = struct.unpack_from('<I', clx, pos + 1)[0]
            plc = clx[pos + 5:pos + 5 + lcb]
            n = (lcb - 4) // 12
            if n <= 0:
                return ""
            cps = struct.unpack_from('<%dI' % (n + 1), plc, 0)
            pcd_off = (n + 1) * 4
            out = []
            for i in range(n):
                fc_raw = struct.unpack_from('<I', plc, pcd_off + i * 8 + 2)[0]
                compressed = bool(fc_raw & 0x40000000)
                fc = fc_raw & 0x3FFFFFFF
                cch = cps[i + 1] - cps[i]
                if cch <= 0:
                    continue
                if compressed:
                    out.append(wd[fc // 2:fc // 2 + cch].decode('cp1252', errors='replace'))
                else:
                    out.append(wd[fc:fc + cch * 2].decode('utf-16-le', errors='replace'))
            return ''.join(out)
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
            # piece table 所在的 Table 流：FIB 0x0A bit9 选 1Table/0Table
            tbl = b""
            try:
                flags = struct.unpack_from('<H', wd, 0x0A)[0]
                which = '1Table' if (flags & 0x0200) else '0Table'
                if not ole.exists(which):
                    which = '0Table' if ole.exists('0Table') else '1Table'
                if ole.exists(which):
                    tbl = ole.openstream(which).read()
            except Exception:
                tbl = b""
        except Exception as e:
            raise MalformedDocumentError(
                f"Failed to read WordDocument stream: {e}",
                file_path=file_path
            ) from e
        finally:
            ole.close()

        # piece table 优先（只取真正正文片，消除尾部乱码）→ FIB 直读 → 启发式兜底
        text = self._extract_piece_table(wd, tbl)
        if len(text.strip()) < 4:
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
