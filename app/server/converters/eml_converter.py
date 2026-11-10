"""EML / MSG 邮件 → Markdown 合并转换器（v2.4.0）

三方实现的融合结论（GitHub v2.2.0 / V100 容器 / 家 FnOS）：

| 能力 | v2.2.0 | V100 | 本实现 |
|---|---|---|---|
| Document Model 结构化 | ✅ | ❌ | ✅ |
| .msg (Outlook) | ✅ | ❌ | ✅ |
| policy=default 正确解 charset | ❌（compat32 兜底易乱码） | ✅ | ✅ |
| 附件惰性迭代（不一次性进内存） | ❌ list() | ✅ | ✅ |
| 附件体积/总量/字数护栏 | ❌ | ✅（末尾截断） | ✅（**逐附件预算 + 提前止损**） |
| 引用链折叠 | ❌ | ❌ | ✅ |
| cid: 内嵌图占位 | ❌ 死链 | ❌ 死链 | ✅ |
| 内嵌邮件 message/rfc822 | ❌ 混入正文 | ❌ 跳过 | ✅ 递归子节（限深） |
| 附件清单进头部 | ❌ | ❌ | ✅ |
| 空主题兜底 | ❌ | ✅ | ✅ |

真实语料依据（河南/南宁 2496 封、3428 MB）：
- 正文中位数仅 **54 字符**（大量"见附件"式邮件），**价值主要在附件**；
- 797 个附件：`.et` 407 / `.wps` 180 / `.xlsx` 74 / `.pdf` 41 / `.zip` 38 / `.ofd` 21；
- 单封最大 41.4 MB；**23% 的邮件带引用链**（转发同一制度 → 重复文本）；
- 实测旧实现：一封 9.1 MB 邮件峰值内存 **1.9 GB**、产出 437 万字符、耗时 194 秒。

设计原则：**先止损，再转换**。附件按体积预算逐个放行，产出按字符预算逐个截断，
任何单个附件/单封邮件都不能把内存和产出撑爆。
"""
from __future__ import annotations

import email
import email.policy
import logging
import os
import re
import tempfile
from email.header import decode_header, make_header
from pathlib import Path

from . import BaseConverter
from .htm_converter import html_to_markdown

from model.document import Document
from model.block import (
    Heading, Paragraph as MdParagraph, ThematicBreak, BlockQuote,
)
from model.inline import Text
from model.table import Table as MdTable, TableRow, TableCell
from render.markdown import document_to_markdown
from errors import MalformedDocumentError

logger = logging.getLogger("docconverter.eml")

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff'}
_PLAIN_EXTS = {'.txt', '.md', '.markdown', '.csv', '.log', '.json', '.xml', '.yaml', '.yml'}

DEFAULTS = {
    "max_attach_mb": 50,          # 单附件体积上限（表格护栏已能兜住大表，故与 V100 对齐 50 MB）
    "max_total_attach_mb": 150,   # 单封邮件附件总量上限
    "max_attach_chars": 120000,   # 单附件转换产出上限
    "max_total_chars": 400000,    # 单封邮件最终 MD 上限
    "collapse_quoted": True,      # 折叠引用链（Re:/转发 的历史正文）
    "quote_preview_chars": 400,   # 折叠后保留的引用预览长度
    "max_nested_depth": 1,        # 内嵌邮件递归深度
}
_ENV = {
    "max_attach_mb": "EML_MAX_ATTACH_MB",
    "max_total_attach_mb": "EML_MAX_TOTAL_ATTACH_MB",
    "max_attach_chars": "EML_MAX_ATTACH_CHARS",
    "max_total_chars": "EML_MAX_TOTAL_CHARS",
    "collapse_quoted": "EML_COLLAPSE_QUOTED",
    "quote_preview_chars": "EML_QUOTE_PREVIEW_CHARS",
    "max_nested_depth": "EML_MAX_NESTED_DEPTH",
}

# 引用链起点：中文 Outlook / Gmail / Apple Mail 常见标记
_QUOTE_PATTERNS = [
    re.compile(r"^-{2,}\s*原始邮件\s*-{2,}", re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.M | re.I),
    re.compile(r"^_{10,}\s*$", re.M),
    re.compile(r"^On .{5,80} wrote:\s*$", re.M),
    re.compile(r"^发件人[:：].{0,60}\r?\n\s*发送时间[:：]", re.M),
    re.compile(r"^在\s*\d{4}年.+写道[:：]\s*$", re.M),
]


def eml_config() -> dict:
    """取 EML 护栏配置（config.eml → 环境变量 → 默认值）"""
    cfg = dict(DEFAULTS)
    try:
        from config import get_config
        sec = (get_config() or {}).get("eml") or {}
        for k in DEFAULTS:
            if k in sec and sec[k] is not None:
                cfg[k] = sec[k]
    except Exception:
        pass
    for k, env in _ENV.items():
        val = os.environ.get(env)
        if val is None:
            continue
        try:
            cfg[k] = (val.lower() in ("1", "true", "yes", "on")) if k == "collapse_quoted" \
                else int(val)
        except ValueError:
            pass
    return cfg


# ----------------------------------------------------------------------
# 头部与文件名
# ----------------------------------------------------------------------

def _decode_mime_header(value) -> str:
    """解码 MIME 编码头（=?utf-8?B?...?=）与 RFC 2231 参数"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _decode_rfc2231(value) -> str:
    """解码 RFC 2231: UTF-8''%E4%BC%9A%E8%AE%AE.docx"""
    import urllib.parse
    if "'" not in value:
        return value
    parts = value.split("'")
    if len(parts) < 3:
        return value
    enc = parts[0] or "utf-8"
    try:
        return urllib.parse.unquote(parts[2], encoding=enc)
    except Exception:
        return urllib.parse.unquote(parts[2])


def _guess_filename(part) -> str:
    """附件文件名：优先 RFC 2231 的 filename*，其次 MIME 编码头"""
    try:
        fn_star = part.get_param("filename*", header="content-disposition")
    except Exception:
        fn_star = None
    if fn_star:
        name = _decode_rfc2231(str(fn_star))
        if name:
            return name
    fn = part.get_filename()
    if fn:
        return _decode_mime_header(fn)
    ctype = (part.get_content_type() or "").lower()
    ext = {".txt": "text/plain", ".html": "text/html", ".pdf": "application/pdf"}.get(ctype)
    if ctype == "text/plain":
        ext = ".txt"
    elif ctype == "text/html":
        ext = ".html"
    elif ctype == "application/pdf":
        ext = ".pdf"
    else:
        ext = ".bin"
    return "attachment" + ext


def _human_size(n: int) -> str:
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576)
    if n >= 1024:
        return "%d KB" % (n // 1024)
    return "%d B" % n


# ----------------------------------------------------------------------
# 正文解析
# ----------------------------------------------------------------------

def _iter_text_parts(msg):
    """按 MIME 结构 yield (ctype, text)；跳过附件与内嵌邮件"""
    if not msg.is_multipart():
        ctype = (msg.get_content_type() or "").lower()
        if ctype in ("text/plain", "text/html") and not msg.get_filename():
            yield ctype, _part_text(msg)
        return
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        if part.get_filename() or part.get_content_disposition() == "attachment":
            continue
        yield ctype, _part_text(part)


def _part_text(part) -> str:
    """取一段 MIME 部件的文本（policy=default 下 get_content 会按 charset 正确解码）"""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        pass
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    cs = part.get_content_charset() or "utf-8"
    for enc in (cs, "utf-8", "gb18030"):
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _replace_cid_refs(html: str, cid_map: dict) -> str:
    """把 <img src="cid:xxx"> 换成可读占位，避免 HTML→MD 留下死链"""
    if not html or "cid:" not in html.lower():
        return html

    def repl(m):
        cid = m.group(2)
        name = cid_map.get(cid.strip("<>"), "") or cid
        return '{}[内嵌图片: {}]'.format(m.group(1), name)

    return re.sub(r'(<img\b[^>]*src\s*=\s*["\'])cid:([^"\']+)', repl, html, flags=re.I)


def _collapse_quoted(text: str, cfg: dict) -> tuple:
    """折叠引用链：保留最近正文，历史引用压成预览块。

    返回 (正文, 折叠说明 or "")
    注意：整封邮件都是转发（引用标记在开头，cut≈0）时同样要折叠——
    否则同一份制度会随转发链在多封邮件里重复入库。
    """
    if not text or not cfg.get("collapse_quoted", True):
        return text, ""
    cut = None
    for pat in _QUOTE_PATTERNS:
        m = pat.search(text)
        if m and (cut is None or m.start() < cut):
            cut = m.start()
    if cut is None:
        return text, ""
    head = text[:cut].rstrip()
    tail = text[cut:]
    preview_len = int(cfg.get("quote_preview_chars", 400))
    # 引用部分本就短于预览长度时，折叠没意义，原样保留
    if len(tail) <= preview_len * 1.5:
        return text, ""
    preview = re.sub(r"\n{3,}", "\n\n", tail.strip())[:preview_len]
    note = ("引用历史已折叠：原文共 {} 字符（回复链/转发历史），"
            "下方仅保留前 {} 字符预览，避免同一制度重复入库。").format(
                len(tail), min(preview_len, len(tail)))
    return head, note + ("\n\n" + preview if preview else "")


def _resolve_body(msg, cfg: dict) -> str:
    """正文：优先纯文本；纯文本过短（如"见附件"）而 HTML 更丰富时用 HTML"""
    plain, html = [], []
    for ctype, text in _iter_text_parts(msg):
        if not text:
            continue
        (plain if ctype == "text/plain" else html).append(text)

    text = "\n\n".join(p.strip() for p in plain if p and p.strip())
    if len(text.strip()) >= 20 and not html:
        return text

    if html:
        # 先把 cid: 内嵌图引用替换成占位
        cid_map = _cid_filename_map(msg)
        joined = "\n\n".join(_replace_cid_refs(h, cid_map) for h in html if h)
        md = html_to_markdown(joined)
        if len(md.strip()) > len(text.strip()):
            return md
    return text


def _cid_filename_map(msg) -> dict:
    """cid → 文件名，用于把内嵌图引用替换成可读占位"""
    m = {}
    for part in msg.walk():
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if cid:
            m[cid] = _guess_filename(part) or cid
    return m


# ----------------------------------------------------------------------
# 附件
# ----------------------------------------------------------------------

def _iter_attachments(msg):
    """惰性遍历真实附件（跳过内嵌图片与内嵌邮件）。yield (文件名, 部件)"""
    if not msg.is_multipart():
        return
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype in ("message/rfc822", "message/delivery-status"):
            continue
        disp = None
        try:
            disp = part.get_content_disposition()
        except Exception:
            pass
        fn = part.get_filename()
        if disp != "attachment" and not fn:
            continue
        name = _guess_filename(part)
        if disp == "inline" and Path(name).suffix.lower() in _IMAGE_EXTS:
            continue
        yield name, part


def _iter_nested_messages(msg):
    """惰性遍历内嵌邮件（message/rfc822）"""
    if not msg.is_multipart():
        return
    for part in msg.walk():
        if (part.get_content_type() or "").lower() != "message/rfc822":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            for sub in payload:
                yield _guess_filename(part) or "内嵌邮件", sub


def _attach_size(part) -> int:
    """附件体积估计：优先编码后长度（不必解码即可判预算），解码后长度更准"""
    raw = part.get_payload(decode=False)
    if isinstance(raw, str):
        # base64 编码后约 4/3 膨胀；quoted-printable 近似 1:1
        cte = (part.get("Content-Transfer-Encoding") or "").lower()
        return int(len(raw) * 0.75) if cte == "base64" else len(raw)
    return 0


def _convert_attachment(name: str, data: bytes, registry, cfg: dict) -> str:
    """把一个附件转成 Markdown 片段（受单附件字符上限约束）"""
    ext = Path(name).suffix.lower()

    if ext in _IMAGE_EXTS:
        return "> [图片附件: {} ({}), 未做 OCR]".format(name, _human_size(len(data)))

    if ext in _PLAIN_EXTS:
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            return "> [附件 {} 无法解码]".format(name)
        return _truncate(text, cfg["max_attach_chars"], "附件 {}".format(name))

    converter = registry.get(ext)
    if converter is None or not converter.available:
        cname = converter.display_name if converter is not None else \
            (ext.lstrip(".").upper() or "未知")
        return "> [附件: {} ({}, {} 格式暂不支持)]".format(
            name, _human_size(len(data)), cname)

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=ext or ".bin", prefix="emlatt_")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        out = converter.convert(tmp_path)
        if isinstance(out, str) and out.strip():
            return _truncate(out.strip(), cfg["max_attach_chars"], "附件 {}".format(name))
        return "> [附件 {} 转换结果为空]".format(name)
    except Exception as e:
        logger.warning("附件转换失败 %s: %s", name, e)
        return "> [附件 {} 转换失败: {}]".format(name, str(e)[:160])
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


def _truncate(text: str, limit: int, what: str) -> str:
    """按字符上限截断并留说明"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n\n> ⚠️ {} 内容超长已截断：原文 {} 字符，保留前 {} 字符。"\
        .format(what, len(text), limit)


# ----------------------------------------------------------------------
# 文档组装
# ----------------------------------------------------------------------

def _collect_meta(msg, file_path: str) -> dict:
    raw_subject = _decode_mime_header(msg.get("Subject")).strip()
    return {
        # 无主题时用文件名兜底（语料里文件名多为主题，比"(无主题)"更有检索价值），
        # 同时在头部表格里明确标注原邮件无主题
        "subject": raw_subject or Path(file_path).stem or "(无主题)",
        "subject_missing": not raw_subject,
        "from": _decode_mime_header(msg.get("From")),
        "to": _decode_mime_header(msg.get("To")),
        "cc": _decode_mime_header(msg.get("Cc")),
        "date": _decode_mime_header(msg.get("Date")),
        "message_id": str(msg.get("Message-ID") or "").strip(),
    }


def _header_table(meta: dict, attachments: list) -> MdTable:
    rows = [TableRow(cells=[
        TableCell(children=[Text("字段")], is_header=True),
        TableCell(children=[Text("内容")], is_header=True),
    ], is_header=True)]

    subject = meta.get("subject") or "(无主题)"
    if meta.get("subject_missing"):
        subject += "（原邮件无主题，标题取自文件名）"
    rows.append(TableRow(cells=[
        TableCell(children=[Text("主题")]),
        TableCell(children=[Text(subject)]),
    ]))

    for label, key in (("发件人", "from"), ("收件人", "to"),
                       ("抄送", "cc"), ("日期", "date")):
        val = meta.get(key) or ""
        if val:
            rows.append(TableRow(cells=[
                TableCell(children=[Text(label)]),
                TableCell(children=[Text(val)]),
            ]))

    if attachments:
        desc = "、".join("{} ({})".format(n, _human_size(sz)) for n, sz in attachments[:8])
        if len(attachments) > 8:
            desc += " 等 {} 个".format(len(attachments))
        rows.append(TableRow(cells=[
            TableCell(children=[Text("附件")]),
            TableCell(children=[Text(desc)]),
        ]))
    return MdTable(rows=rows)


def _build_document(msg, file_path: str, registry, cfg: dict,
                    depth: int = 0, title_prefix: str = "") -> Document:
    """把一封已解析邮件组装成 Document（可递归处理内嵌邮件）"""
    meta = _collect_meta(msg, file_path)
    doc = Document(title=title_prefix + meta["subject"])

    # 附件清单（先枚举体积，不解码 payload）
    att_list = []
    for name, part in _iter_attachments(msg):
        att_list.append((name, _attach_size(part)))

    doc.add_block(_header_table(meta, att_list))

    # 正文
    body = _resolve_body(msg, cfg)
    body, quote_note = _collapse_quoted(body, cfg)
    if body.strip():
        doc.add_block(MdParagraph(children=[Text(body.strip())]))
    if quote_note:
        doc.add_block(BlockQuote(blocks=[MdParagraph(children=[Text(quote_note)])]))

    # 内嵌邮件（转发原文）：限深递归
    if depth < int(cfg.get("max_nested_depth", 1)):
        for i, (name, sub) in enumerate(_iter_nested_messages(msg), 1):
            doc.add_block(ThematicBreak())
            doc.add_block(Heading(level=2, children=[
                Text("内嵌邮件 {}/{}: {}".format(i, name, 
                     _decode_mime_header(sub.get("Subject")) or "(无主题)"))]))
            sub_doc = _build_document(sub, file_path, registry, cfg, depth + 1)
            for blk in sub_doc.blocks:
                doc.add_block(blk)

    # 附件内容（逐附件预算：先判体积，再转，再按字符截断）
    max_single = int(cfg["max_attach_mb"]) * 1048576
    budget = int(cfg["max_total_attach_mb"]) * 1048576
    max_total_chars = int(cfg["max_total_chars"])
    used_chars = 0
    emitted = False

    for idx, (name, part) in enumerate(_iter_attachments(msg), 1):
        size = _attach_size(part)
        if max_single and size > max_single:
            doc.add_block(MdParagraph(children=[Text(
                "> [附件 {} 跳过：{} 超过单附件上限 {} MB"
                "（EML_MAX_ATTACH_MB 可调）]".format(
                    name, _human_size(size), cfg["max_attach_mb"]))]))
            continue
        if budget - size < 0:
            doc.add_block(MdParagraph(children=[Text(
                "> [附件 {} 跳过：本封邮件附件总量超过 {} MB 预算"
                "（EML_MAX_TOTAL_ATTACH_MB 可调）]".format(
                    name, cfg["max_total_attach_mb"]))]))
            continue
        budget -= size

        # 总字符预算将尽：剩下的附件只记清单，不再转换
        if used_chars >= max_total_chars:
            doc.add_block(MdParagraph(children=[Text(
                "> [附件 {} 未展开：本封邮件已产出 {} 字符，达到上限 {}"
                "（EML_MAX_TOTAL_CHARS 可调）]".format(
                    name, used_chars, max_total_chars))]))
            continue

        data = part.get_payload(decode=True)
        if not data:
            continue

        if not emitted:
            doc.add_block(ThematicBreak())
            doc.add_block(Heading(level=2, children=[Text("附件内容")]))
            emitted = True

        doc.add_block(Heading(level=3, children=[
            Text("附件 {}/{}: {}（{}）".format(
                idx, len(att_list), name, _human_size(len(data))))]))
        md = _convert_attachment(name, data, registry, cfg)
        if md:
            used_chars += len(md)
            doc.add_block(MdParagraph(children=[Text(md)]))

    return doc


class EMLConverter(BaseConverter):
    """EML → MD（邮件头 + 正文 + 附件内容合并，带预算护栏）"""

    def supported_extensions(self) -> list:
        return ['.eml']

    @property
    def display_name(self) -> str:
        return "EML邮件"

    def _parse(self, file_path: str):
        """用 policy.default 解析：get_content() 才会按 charset 正确解码"""
        try:
            with open(file_path, "rb") as f:
                return email.message_from_binary_file(f, policy=email.policy.default)
        except Exception as e:
            raise MalformedDocumentError(
                "Failed to parse EML file: {}".format(e), file_path=file_path)

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        from converters import registry
        msg = self._parse(file_path)
        return _build_document(msg, file_path, registry, eml_config())

    def convert(self, file_path: str, **kwargs) -> str:
        return document_to_markdown(self.convert_to_document(file_path, **kwargs))


class MSGConverter(BaseConverter):
    """MSG (Outlook) → MD（extract-msg），与 EML 共用同一套预算护栏"""

    def supported_extensions(self) -> list:
        return ['.msg']

    @property
    def display_name(self) -> str:
        return "MSG邮件"

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        from converters import registry
        try:
            import extract_msg
        except ImportError:
            raise MalformedDocumentError(
                "需要 extract-msg 库才能解析 .msg（pip install extract-msg）",
                file_path=file_path)
        cfg = eml_config()
        try:
            msg = extract_msg.Message(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                "Failed to parse MSG file: {}".format(e), file_path=file_path)

        try:
            subject = (msg.subject or "").strip() or Path(file_path).stem
            meta = {
                "subject": subject or "(无主题)",
                "from": str(msg.sender or ""),
                "to": str(msg.to or ""),
                "cc": str(msg.cc or ""),
                "date": str(msg.date or ""),
                "message_id": str(getattr(msg, "messageId", "") or ""),
            }
            body = (msg.body or "").strip()
            if not body and getattr(msg, "htmlBody", None):
                html = msg.htmlBody
                if isinstance(html, bytes):
                    html = html.decode("utf-8", errors="replace")
                body = html_to_markdown(html)

            atts = []
            try:
                atts = list(msg.attachments)
            except Exception:
                atts = []

            doc = Document(title=meta["subject"])
            doc.add_block(_header_table(
                meta, [(getattr(a, "longFilename", "") or getattr(a, "shortFilename", "")
                        or "附件", len(getattr(a, "data", b"") or b"")) for a in atts]))

            body, quote_note = _collapse_quoted(body, cfg)
            if body.strip():
                doc.add_block(MdParagraph(children=[Text(body.strip())]))
            if quote_note:
                doc.add_block(BlockQuote(blocks=[MdParagraph(children=[Text(quote_note)])]))

            max_single = int(cfg["max_attach_mb"]) * 1048576
            max_total = int(cfg["max_total_chars"])
            used = 0
            if atts:
                doc.add_block(ThematicBreak())
                doc.add_block(Heading(level=2, children=[Text("附件内容")]))
                for i, att in enumerate(atts, 1):
                    name = (getattr(att, "longFilename", "")
                            or getattr(att, "shortFilename", "") or "附件%d" % i)
                    data = getattr(att, "data", b"") or b""
                    doc.add_block(Heading(level=3, children=[Text(
                        "附件 {}/{}: {}（{}）".format(i, len(atts), name,
                                                     _human_size(len(data))))]))
                    if max_single and len(data) > max_single:
                        doc.add_block(MdParagraph(children=[Text(
                            "> [附件 {} 跳过：{} 超过单附件上限 {} MB]".format(
                                name, _human_size(len(data)), cfg["max_attach_mb"]))]))
                        continue
                    if used >= max_total:
                        doc.add_block(MdParagraph(children=[Text(
                            "> [附件 {} 未展开：已达本封邮件字符上限 {}]".format(
                                name, max_total))]))
                        continue
                    md = _convert_attachment(name, data, registry, cfg)
                    if md:
                        used += len(md)
                        doc.add_block(MdParagraph(children=[Text(md)]))
            return doc
        finally:
            try:
                msg.close()
            except Exception:
                pass

    def convert(self, file_path: str, **kwargs) -> str:
        return document_to_markdown(self.convert_to_document(file_path, **kwargs))
