"""
EML / MSG 邮件 → Markdown 合并转换器

将邮件信息（发件人/收件人/日期/主题）、正文（纯文本或 HTML→MD）、
附件内容（可转换文档就地转换，二进制附件记录元信息）
合并为**单个 MD 文档**，便于推送到知识库后保留完整邮件上下文。

- .eml 用标准库 email 解析
- .msg 用 extract-msg 库解析（Outlook 格式）

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- 新增 convert_to_document() 返回 Document Model
- 使用结构化错误 MalformedDocumentError
"""
import email
import logging
import tempfile
import time
from email.header import decode_header, make_header
from pathlib import Path

from . import BaseConverter
from .htm_converter import html_to_markdown

# Document Model 导入
from model.document import Document
from model.block import Heading, Paragraph as MdParagraph, ThematicBreak
from model.inline import Text
from model.table import Table as MdTable, TableRow, TableCell
from render.markdown import document_to_markdown
from errors import MalformedDocumentError

logger = logging.getLogger("docconverter.eml")

_ATTACH_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff'}


def _decode_mime_header(value) -> str:
    """解码 MIME 编码头（=?utf-8?B?...?=）与 RFC 2231 参数"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _guess_filename(part) -> str:
    # 优先 RFC 2231 filename*
    fn_star = part.get_param("filename*", header="content-disposition")
    if fn_star:
        return _decode_rfc2231(fn_star)
    fn = part.get_filename()
    if fn:
        return _decode_mime_header(fn)
    ctype = part.get_content_type()
    ext = ".bin"
    if ctype == "text/plain":
        ext = ".txt"
    elif ctype == "text/html":
        ext = ".html"
    elif ctype == "application/pdf":
        ext = ".pdf"
    return "attachment{}".format(ext)


def _decode_rfc2231(value) -> str:
    """解码 RFC 2231: UTF-8''%E4%BC%9A%E8%AE%AE.docx"""
    import urllib.parse
    if "'" in value:
        _, _, rest = value.partition("'")
        rest = rest.partition("'")[2]
        try:
            return urllib.parse.unquote(rest, encoding="utf-8")
        except Exception:
            return urllib.parse.unquote(rest)
    return value


def _body_from_msg(msg) -> str:
    """从 email.message 提取正文（优先纯文本，其次 HTML 转 MD）"""
    if msg.is_multipart():
        text_parts = []
        html_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and part.get_filename() is None:
                try:
                    text_parts.append(part.get_content())
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    text_parts.append(payload.decode("utf-8", errors="replace"))
            elif ctype == "text/html" and part.get_filename() is None:
                try:
                    html_parts.append(part.get_content())
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    html_parts.append(payload.decode("utf-8", errors="replace"))
        if text_parts:
            return "\n\n".join(p.strip() for p in text_parts if p.strip())
        if html_parts:
            return html_to_markdown("\n\n".join(html_parts))
        return ""
    ctype = msg.get_content_type()
    if ctype == "text/plain":
        try:
            return msg.get_content() or ""
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            return payload.decode("utf-8", errors="replace")
    if ctype == "text/html":
        try:
            return html_to_markdown(msg.get_content() or "")
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            return html_to_markdown(payload.decode("utf-8", errors="replace"))
    return ""


def _iter_attachments(msg):
    """遍历附件（含内嵌图片）"""
    for part in msg.walk():
        if part.get_content_disposition() == "attachment" or part.get_filename():
            fn = _guess_filename(part)
            if part.get_content_disposition() == "inline" and fn.lower().endswith(tuple(_ATTACH_IMAGE_EXTS)):
                continue  # 内嵌图片跳过
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            yield fn, payload, part.get_content_type()


def _convert_attachment(fn: str, data: bytes, registry) -> str:
    """将附件内容转换为 Markdown 片段"""
    ext = Path(fn).suffix.lower()
    converter = registry.get(ext)
    if converter is None or not converter.available:
        # 纯文本附件直接展示
        if ext in ('.txt', '.md', '.markdown', '.csv', '.log', '.json', '.xml'):
            try:
                return data.decode("utf-8", errors="replace").strip()
            except Exception:
                return ""
        if ext in _ATTACH_IMAGE_EXTS:
            return "> [图片附件: {} ({} KB)]".format(fn, len(data) // 1024)
        ext_name = (converter.display_name if converter else ext.lstrip('.').upper() or "文件")
        return "> [附件: {} ({} KB, {} 格式)]".format(fn, len(data) // 1024, ext_name)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = converter.convert(tmp_path)
        if isinstance(result, str) and result.strip():
            return result.strip()
        return "> [附件 {} 转换结果为空]".format(fn)
    except Exception as e:
        logger.warning("附件转换失败 %s: %s", fn, e)
        return "> [附件 {} 转换失败: {}]".format(fn, e)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


class EMLConverter(BaseConverter):
    """EML → MD (邮件头+正文+附件合并)"""

    def supported_extensions(self) -> list:
        return ['.eml']

    @property
    def display_name(self) -> str:
        return "EML邮件"

    def _parse_eml(self, file_path: str):
        """解析 EML 文件，返回 (msg, subject, frm, to, cc, date, body, attachments)"""
        from converters import registry
        try:
            with open(file_path, "rb") as f:
                msg = email.message_from_bytes(f.read())
        except Exception as e:
            raise MalformedDocumentError(
                "Failed to parse EML file: {}".format(e),
                file_path=file_path
            )

        subject = _decode_mime_header(msg.get("Subject"))
        frm = _decode_mime_header(msg.get("From"))
        to = _decode_mime_header(msg.get("To"))
        cc = _decode_mime_header(msg.get("Cc"))
        date = msg.get("Date", "")
        body = _body_from_msg(msg)
        attachments = list(_iter_attachments(msg))

        return msg, subject, frm, to, cc, date, body, attachments

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        from converters import registry
        msg, subject, frm, to, cc, date, body, attachments = self._parse_eml(file_path)

        doc = Document(title=subject or Path(file_path).stem)

        # 邮件头表格
        header_rows = [
            TableRow(
                cells=[
                    TableCell(children=[Text("字段")], is_header=True),
                    TableCell(children=[Text("内容")], is_header=True),
                ],
                is_header=True,
            )
        ]
        if frm:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("发件人")]),
                TableCell(children=[Text(frm)]),
            ]))
        if to:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("收件人")]),
                TableCell(children=[Text(to)]),
            ]))
        if cc:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("抄送")]),
                TableCell(children=[Text(cc)]),
            ]))
        if date:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("日期")]),
                TableCell(children=[Text(date)]),
            ]))

        if len(header_rows) > 1:  # 有实际数据行
            doc.add_block(MdTable(rows=header_rows))

        # 正文
        if body and body.strip():
            doc.add_block(MdParagraph(children=[Text(body.strip())]))

        # 附件
        if attachments:
            doc.add_block(ThematicBreak())
            doc.add_block(Heading(level=2, children=[Text("附件内容")]))
            for fn, data, ctype in attachments:
                doc.add_block(Heading(level=3, children=[Text("附件: {}".format(fn))]))
                md = _convert_attachment(fn, data, registry)
                if md:
                    doc.add_block(MdParagraph(children=[Text(md)]))

        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)


class MSGConverter(BaseConverter):
    """MSG (Outlook) → MD (extract-msg 库)"""

    def supported_extensions(self) -> list:
        return ['.msg']

    @property
    def display_name(self) -> str:
        return "MSG邮件"

    def _parse_msg(self, file_path: str):
        """解析 MSG 文件，返回 (subject, frm, to, date, body, attachments_data)"""
        import extract_msg
        try:
            msg = extract_msg.Message(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                "Failed to parse MSG file: {}".format(e),
                file_path=file_path
            )
        try:
            subject = msg.subject or Path(file_path).stem
            frm = str(msg.sender or "")
            to = str(msg.to or "")
            date = str(msg.date or "")
            body = (msg.body or "").strip()
            if not body and msg.htmlBody:
                body = html_to_markdown(msg.htmlBody)

            try:
                attachments = list(msg.attachments)
            except Exception:
                attachments = []

            # Extract attachment data before closing
            attach_data = []
            for att in attachments:
                fn = getattr(att, "longFilename", "") or getattr(att, "shortFilename", "") or "附件"
                data = att.data
                attach_data.append((fn, data))

            return subject, frm, to, date, body, attach_data
        finally:
            try:
                msg.close()
            except Exception:
                pass

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        from converters import registry
        subject, frm, to, date, body, attach_data = self._parse_msg(file_path)

        doc = Document(title=subject)

        # 邮件头表格
        header_rows = [
            TableRow(
                cells=[
                    TableCell(children=[Text("字段")], is_header=True),
                    TableCell(children=[Text("内容")], is_header=True),
                ],
                is_header=True,
            )
        ]
        if frm:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("发件人")]),
                TableCell(children=[Text(frm)]),
            ]))
        if to:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("收件人")]),
                TableCell(children=[Text(to)]),
            ]))
        if date:
            header_rows.append(TableRow(cells=[
                TableCell(children=[Text("日期")]),
                TableCell(children=[Text(date)]),
            ]))

        if len(header_rows) > 1:
            doc.add_block(MdTable(rows=header_rows))

        # 正文
        if body and body.strip():
            doc.add_block(MdParagraph(children=[Text(body.strip())]))

        # 附件
        if attach_data:
            doc.add_block(ThematicBreak())
            doc.add_block(Heading(level=2, children=[Text("附件内容")]))
            for fn, data in attach_data:
                doc.add_block(Heading(level=3, children=[Text("附件: {}".format(fn))]))
                md = _convert_attachment(fn, data, registry)
                if md:
                    doc.add_block(MdParagraph(children=[Text(md)]))

        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
