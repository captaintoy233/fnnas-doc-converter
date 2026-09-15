"""RTF → Markdown 转换器。

使用纯 Python 解析 RTF 文本内容，无需外部依赖。
支持中文 RTF（GBK/UTF-8 编码）。

架构:
- convert_to_document() → Document Model
- convert() → Markdown 字符串
"""
import re
from pathlib import Path
from typing import Optional

from . import BaseConverter
from model.document import Document
from model.block import Paragraph, Heading
from model.inline import Text
from render.markdown import document_to_markdown
from errors import MalformedDocumentError


class RTFConverter(BaseConverter):
    """RTF → Markdown (纯 Python)"""

    def supported_extensions(self) -> list:
        return ['.rtf']

    def _extract_rtf_text(self, file_path: str) -> str:
        """从 RTF 文件中提取纯文本"""
        try:
            with open(file_path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            raise MalformedDocumentError(
                f"Cannot read RTF file: {e}", file_path=file_path
            ) from e

        # 验证 RTF 头
        if not raw.startswith(b'{\\rtf'):
            raise MalformedDocumentError(
                "Not a valid RTF file", file_path=file_path
            )

        content = raw.decode('ascii', errors='replace')

        # 提取字体表中的字符集信息
        charset = 'gbk'  # 默认中文
        if '\\ansi' in content[:200]:
            charset = 'cp1252'
        if '\\ansicpg936' in content[:500] or '\\lang2052' in content[:500]:
            charset = 'gbk'
        if '\\ansicpg65001' in content[:500]:
            charset = 'utf-8'

        # 处理 RTF Unicode 转义 \'xx
        def decode_hex_escape(match):
            hex_val = match.group(1)
            try:
                byte_val = bytes([int(hex_val, 16)])
                return byte_val.decode(charset, errors='replace')
            except (ValueError, UnicodeDecodeError):
                return '?'

        # 先收集所有连续的 \'xx 序列，按字节组解码
        text = content

        # 使用状态机解析 RTF，只提取文本内容
        text = self._parse_rtf_text(content, charset)

        # 处理段落标记
        text = re.sub(r'\\par\b', '\n', text)
        text = re.sub(r'\\line\b', '\n', text)
        text = re.sub(r'\\tab\b', '\t', text)

        # 处理 Unicode 转义 \uN
        def unicode_escape(match):
            code = int(match.group(1))
            if code < 0:
                code += 65536
            return chr(code)
        text = re.sub(r'\\u(-?\d+)\??', unicode_escape, text)

        # 处理十六进制转义 \'xx
        # 收集连续 \'xx 并作为字节序列解码
        def hex_group_decode(match):
            hex_pairs = match.group(0)
            byte_vals = []
            for h in re.findall(r"\\'([0-9a-fA-F]{2})", hex_pairs):
                byte_vals.append(int(h, 16))
            try:
                return bytes(byte_vals).decode(charset, errors='replace')
            except:
                return ''.join(chr(b) if b < 128 else '?' for b in byte_vals)

        text = re.sub(r"(?:\\'[0-9a-fA-F]{2})+", hex_group_decode, text)

        # 移除剩余 RTF 控制字
        text = re.sub(r'\\[a-z]+\d*\s?', '', text)
        text = re.sub(r'\\[{}]', lambda m: m.group(0)[1], text)  # \{ \} → { }
        text = re.sub(r'[{}]', '', text)  # 移除分组括号

        # 清理
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

        return text.strip()

    @staticmethod
    def _parse_rtf_text(rtf: str, charset: str = 'gbk') -> str:
        """状态机 RTF 解析器：只提取可见文本，跳过所有控制组"""
        result = []
        i = 0
        n = len(rtf)
        depth = 0
        skip_depth = -1  # 当 >= 0 时，该深度及以上的内容被跳过

        # 需要跳过的组关键字
        skip_keywords = {
            'fonttbl', 'colortbl', 'stylesheet', 'info', 'pict',
            'object', 'shppict', 'datastore', 'listtable',
            'listoverridetable', 'generator', 'mmathPr',
            'latentstyles', 'rsidtbl', 'themedata', 'colorschememapping',
        }

        while i < n:
            ch = rtf[i]

            if ch == '{':
                depth += 1
                # 检查是否是需要跳过的组
                rest = rtf[i+1:i+40]
                # 去掉前导 \* 或 \\* 前缀
                stripped = rest.lstrip('\\* ')
                for kw in skip_keywords:
                    if stripped.startswith(kw):
                        skip_depth = depth
                        break
                i += 1
                continue

            if ch == '}':
                if depth == skip_depth:
                    skip_depth = -1
                depth -= 1
                i += 1
                continue

            # 如果在跳过区域内，直接跳过
            if skip_depth >= 0 and depth >= skip_depth:
                i += 1
                continue

            if ch == '\\':
                # 读取控制字
                j = i + 1
                if j >= n:
                    break

                # 转义字符 \{ \} \\
                if rtf[j] in '{}\\':
                    result.append(rtf[j])
                    i = j + 1
                    continue

                # \'xx 十六进制字节
                if rtf[j] == "'":
                    hex_str = rtf[j+1:j+3]
                    try:
                        byte_val = bytes([int(hex_str, 16)])
                        # 收集连续的 \'xx 序列一起解码
                        all_bytes = bytearray(byte_val)
                        k = j + 3
                        while k + 3 <= n and rtf[k:k+2] == "\\'":
                            hb = rtf[k+2:k+4]
                            try:
                                all_bytes.append(int(hb, 16))
                            except ValueError:
                                break
                            k += 4
                        decoded = bytes(all_bytes).decode(charset, errors='replace')
                        result.append(decoded)
                        i = k
                    except (ValueError, IndexError):
                        i = j + 3
                    continue

                # \uN Unicode 转义
                if rtf[j] == 'u':
                    k = j + 1
                    num_str = ''
                    if k < n and rtf[k] == '-':
                        num_str = '-'
                        k += 1
                    while k < n and rtf[k].isdigit():
                        num_str += rtf[k]
                        k += 1
                    if num_str and num_str != '-':
                        code = int(num_str)
                        if code < 0:
                            code += 65536
                        result.append(chr(code))
                    # 跳过可选的 ? 替代字符
                    if k < n and rtf[k] == '?':
                        k += 1
                    i = k
                    continue

                # 其他控制字（\par, \line, \tab 等）
                ctrl = ''
                k = j
                while k < n and rtf[k].isalpha():
                    ctrl += rtf[k]
                    k += 1
                # 跳过数字参数
                while k < n and (rtf[k].isdigit() or rtf[k] == '-'):
                    k += 1
                # 跳过一个尾随空格
                if k < n and rtf[k] == ' ':
                    k += 1

                if ctrl == 'par' or ctrl == 'line':
                    result.append('\n')
                elif ctrl == 'tab':
                    result.append('\t')
                # 其他控制字忽略

                i = k
                continue

            # 普通字符
            if ch not in '\r\n':
                result.append(ch)
            i += 1

        text = ''.join(result)
        # 移除 VML/shape 数据残留
        text = re.sub(r'\*?shapeType\d+fFlip[A-Za-z]\d+[^\\]*?(?=\n|$)', '', text)
        # 移除符号字体定义残留（如 seltbaln *!),.:;?...）
        text = re.sub(r'^[^\u4e00-\u9fff\n]*\*.*$', '', text, flags=re.MULTILINE)
        # 清理
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        # 移除只包含特殊符号的行
        lines = []
        for line in text.split('\n'):
            stripped = line.strip()
            if stripped and not re.match(r'^[*\s]+$', stripped):
                # 检查是否主要是非文本字符
                cjk_count = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff')
                alpha_count = sum(1 for c in stripped if c.isalpha())
                if cjk_count > 0 or alpha_count > 3 or len(stripped) < 5:
                    lines.append(line)
        return '\n'.join(lines).strip()

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        text = self._extract_rtf_text(file_path)
        doc = Document()

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            doc.add_block(Paragraph(children=[Text(line)]))

        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
