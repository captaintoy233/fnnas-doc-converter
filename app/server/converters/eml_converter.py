"""
EML / MSG 邮件 → Markdown 合并转换器

将邮件信息（发件人/收件人/日期/主题）、正文（纯文本或 HTML→MD）、
附件内容（可转换文档就地转换，二进制附件记录元信息）
合并为**单个 MD 文档**，便于推送到知识库后保留完整邮件上下文。

- .eml 用标准库 email 解析
- .msg 用 extract-msg 库解析（Outlook 格式）
"""
import email
import logging
import tempfile
import time
from email.header import decode_header, make_header
from pathlib import Path

from . import BaseConverter
from .htm_converter import html_to_markdown

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

    def _convert_eml(self, file_path: str) -> str:
        from converters import registry
        with open(file_path, "rb") as f:
            msg = email.message_from_bytes(f.read())

        subject = _decode_mime_header(msg.get("Subject"))
        frm = _decode_mime_header(msg.get("From"))
        to = _decode_mime_header(msg.get("To"))
        cc = _decode_mime_header(msg.get("Cc"))
        date = msg.get("Date", "")

        parts = ["# {}".format(subject or Path(file_path).stem), ""]
        parts.append("| 字段 | 内容 |")
        parts.append("|---|---|")
        if frm:
            parts.append("| 发件人 | {} |".format(frm.replace('|', '\\|')))
        if to:
            parts.append("| 收件人 | {} |".format(to.replace('|', '\\|')))
        if cc:
            parts.append("| 抄送 | {} |".format(cc.replace('|', '\\|')))
        if date:
            parts.append("| 日期 | {} |".format(date.replace('|', '\\|')))
        parts.append("")

        body = _body_from_msg(msg)
        if body:
            parts.append(body.strip())
        parts.append("")

        # 附件
        attachments = list(_iter_attachments(msg))
        if attachments:
            parts.append("---")
            parts.append("")
            parts.append("## 附件内容")
            for fn, data, ctype in attachments:
                parts.append("")
                parts.append("### 附件: {}".format(fn))
                parts.append("")
                md = _convert_attachment(fn, data, registry)
                if md:
                    parts.append(md)
        return "\n".join(parts)

    def convert(self, file_path: str, **kwargs) -> str:
        return self._convert_eml(file_path)


class MSGConverter(BaseConverter):
    """MSG (Outlook) → MD (extract-msg 库)"""

    def supported_extensions(self) -> list:
        return ['.msg']

    @property
    def display_name(self) -> str:
        return "MSG邮件"

    def _convert_msg(self, file_path: str) -> str:
        import extract_msg
        from converters import registry
        msg = extract_msg.Message(file_path)
        try:
            subject = msg.subject or Path(file_path).stem
            frm = str(msg.sender or "")
            to = str(msg.to or "")
            date = str(msg.date or "")
            body = (msg.body or "").strip()
            if not body and msg.htmlBody:
                body = html_to_markdown(msg.htmlBody)

            parts = ["# {}".format(subject), ""]
            parts.append("| 字段 | 内容 |")
            parts.append("|---|---|")
            if frm:
                parts.append("| 发件人 | {} |".format(frm.replace('|', '\\|')))
            if to:
                parts.append("| 收件人 | {} |".format(to.replace('|', '\\|')))
            if date:
                parts.append("| 日期 | {} |".format(date.replace('|', '\\|')))
            parts.append("")
            if body:
                parts.append(body.strip())
            parts.append("")

            try:
                attachments = list(msg.attachments)
            except Exception:
                attachments = []
            if attachments:
                parts.append("---")
                parts.append("")
                parts.append("## 附件内容")
                for att in attachments:
                    fn = getattr(att, "longFilename", "") or getattr(att, "shortFilename", "") or "附件"
                    parts.append("")
                    parts.append("### 附件: {}".format(fn))
                    parts.append("")
                    data = att.data
                    md = _convert_attachment(fn, data, registry)
                    if md:
                        parts.append(md)
            return "\n".join(parts)
        finally:
            try:
                msg.close()
            except Exception:
                pass

    def convert(self, file_path: str, **kwargs) -> str:
        return self._convert_msg(file_path)
