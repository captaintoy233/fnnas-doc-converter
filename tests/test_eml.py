# -*- coding: utf-8 -*-
"""EML / MSG 转换测试：三方融合后的关键能力逐条锁定。

覆盖：
- 字符集（GBK 正文 / 编码字主题 / quoted-printable）
- 结构（HTML→MD、cid 内嵌图占位、内嵌邮件递归、空主题兜底）
- 附件（清单进头部、纯文本内联、体积/字符/总量三重预算）
- 噪声治理（引用链折叠）
- 表格护栏（行/列/单元格/sheet 上限与截断说明）
"""
import base64
import os
from email.message import EmailMessage
from email.header import Header
from pathlib import Path

import pytest

from converters import registry
from converters.loader import register_all
from converters.eml_converter import EMLConverter, MSGConverter, eml_config


@pytest.fixture(autouse=True)
def _registered():
    register_all()
    yield


def _write(path: Path, raw: bytes) -> str:
    path.write_bytes(raw)
    return str(path)


def _gbk_eml(path: Path) -> str:
    """GBK 正文 + GBK 编码主题（Outlook 常见；compat32 解析会乱码）"""
    subject = "关于信贷业务的通知"
    body = "请各支行按要求报送材料。"
    # RFC 2047 B 编码的 GBK 主题
    enc_subject = "=?gb2312?B?%s?=" % base64.b64encode(
        subject.encode("gbk")).decode()
    raw = (
        "From: 张三 <zhangsan@example.com>\r\n"
        "To: 李四 <lisi@example.com>\r\n"
        "Subject: {}\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=gb2312\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        "{}".format(enc_subject, body)
    ).encode("gbk", errors="replace")
    # 头部是 ASCII，正文 GBK
    head, _, tail = raw.partition(b"\r\n\r\n")
    return _write(path, head + b"\r\n\r\n" + body.encode("gbk"))


def test_gbk_body_and_encoded_subject(tmp_path):
    p = _gbk_eml(tmp_path / "gbk.eml")
    md = EMLConverter().convert(p)
    assert "关于信贷业务的通知" in md
    assert "请各支行按要求报送材料" in md


def test_quoted_printable_utf8_body(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "QP 测试"
    msg["From"] = "a@example.com"
    msg.set_content("第一行内容\n第二行内容", charset="utf-8", cte="quoted-printable")
    p = tmp_path / "qp.eml"
    msg_bytes = msg.as_bytes()
    p.write_bytes(msg_bytes)
    md = EMLConverter().convert(str(p))
    assert "第一行内容" in md and "第二行内容" in md


def test_header_table_has_attachment_manifest(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "带附件"
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    msg.set_content("请查收附件。")
    msg.add_attachment(b"hello", maintype="text", subtype="plain", filename="说明.txt")
    p = tmp_path / "att.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "| 发件人 |" in md and "| 收件人 |" in md
    assert "| 附件 |" in md and "说明.txt" in md
    # 纯文本附件内容被内联
    assert "hello" in md


def test_html_body_with_cid_placeholder(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "含内嵌图"
    msg.set_content("纯文本兜底")
    msg.add_alternative(
        '<html><body><p>正文段落</p>'
        '<img src="cid:img001@example.com"></body></html>',
        subtype="html")
    msg.get_payload()[1].add_related(
        b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png",
        cid="<img001@example.com>", filename="pic.png")
    p = tmp_path / "cid.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "正文段落" in md
    assert "cid:img001" not in md, "cid 死链必须被替换"
    assert "内嵌图片" in md


def test_quoted_chain_is_collapsed(tmp_path):
    history = "这是被转发的历史正文。" * 200  # 足够长，超过预览阈值
    body = ("本封新增说明：请按新口径执行。\n\n"
            "-----原始邮件-----\n"
            "发件人: 王五\n发送时间: 2026-01-01\n\n" + history)
    msg = EmailMessage()
    msg["Subject"] = "Re: 口径说明"
    msg.set_content(body)
    p = tmp_path / "quote.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "本封新增说明" in md
    assert "引用历史已折叠" in md
    assert len(md) < len(history), "折叠后总长度应远小于原始引用链"


def test_nested_rfc822_rendered_as_subsection(tmp_path):
    inner = EmailMessage()
    inner["Subject"] = "被转发的原始邮件"
    inner["From"] = "inner@example.com"
    inner.set_content("原始邮件正文内容")

    outer = EmailMessage()
    outer["Subject"] = "转发: 原始邮件"
    outer["From"] = "outer@example.com"
    outer.set_content("见转发内容")
    outer.add_attachment(inner)
    p = tmp_path / "nested.eml"
    p.write_bytes(outer.as_bytes())

    md = EMLConverter().convert(str(p))
    assert "内嵌邮件" in md
    assert "被转发的原始邮件" in md
    assert "原始邮件正文内容" in md


def test_empty_subject_fallback(tmp_path):
    msg = EmailMessage()
    msg["From"] = "x@example.com"
    msg.set_content("无主题邮件正文")
    p = tmp_path / "nosub.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "原邮件无主题" in md


def test_attachment_size_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("EML_MAX_ATTACH_MB", "1")
    msg = EmailMessage()
    msg["Subject"] = "大附件"
    msg.set_content("正文")
    msg.add_attachment(b"x" * (2 * 1024 * 1024), maintype="application", subtype="pdf",
                       filename="big.pdf")
    p = tmp_path / "big.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "跳过" in md and "超过单附件上限" in md
    assert "big.pdf" in md          # 清单里仍可见
    assert len(md) < 400000


def test_attachment_char_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("EML_MAX_ATTACH_CHARS", "200")
    msg = EmailMessage()
    msg["Subject"] = "长文本附件"
    msg.set_content("正文")
    msg.add_attachment(("甲乙丙丁" * 500).encode("utf-8"), maintype="text",
                       subtype="plain", filename="long.txt")
    p = tmp_path / "longatt.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "已截断" in md
    assert len(md) < 2000


def test_total_chars_budget_stops_further_attachments(tmp_path, monkeypatch):
    monkeypatch.setenv("EML_MAX_TOTAL_CHARS", "300")
    msg = EmailMessage()
    msg["Subject"] = "多附件"
    msg.set_content("正文")
    for i in range(3):
        msg.add_attachment(("内容" * 400).encode("utf-8"), maintype="text", subtype="plain",
                           filename="f%d.txt" % i)
    p = tmp_path / "multi.eml"
    p.write_bytes(msg.as_bytes())
    md = EMLConverter().convert(str(p))
    assert "未展开" in md or "已截断" in md
    assert len(md) <= 1200, "总量预算必须真正拦住后续附件"


def test_eml_config_env_override(monkeypatch):
    monkeypatch.setenv("EML_MAX_ATTACH_MB", "7")
    monkeypatch.setenv("EML_COLLAPSE_QUOTED", "false")
    cfg = eml_config()
    assert cfg["max_attach_mb"] == 7
    assert cfg["collapse_quoted"] is False


# ---------------------------------------------------------------- 表格护栏

def test_xlsx_row_cap_and_note(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    from converters.xlsx_converter import XLSXConverter
    monkeypatch.setenv("SPREADSHEET_MAX_ROWS", "50")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["编号", "名称"])
    for i in range(500):
        ws.append([i, "项目%d" % i])
    p = tmp_path / "big.xlsx"
    wb.save(p)

    md = XLSXConverter().convert(str(p))
    assert "表格过大已截断" in md
    assert "行数" in md
    # 只应保留约 50 行数据
    assert md.count("| 项目") <= 60


def test_xlsx_sheet_cap(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    from converters.xlsx_converter import XLSXConverter
    monkeypatch.setenv("SPREADSHEET_MAX_SHEETS", "3")

    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    for i in range(9):
        wb.create_sheet("S%d" % i).append([i])
    p = tmp_path / "many.xlsx"
    wb.save(p)

    md = XLSXConverter().convert(str(p))
    assert "sheet" in md and "仅处理前 3 个" in md


def test_spreadsheet_limits_defaults(monkeypatch):
    from converters.spreadsheet_limits import get_limits, DEFAULTS
    for env in ("SPREADSHEET_MAX_ROWS", "SPREADSHEET_MAX_COLS",
                "SPREADSHEET_MAX_CELLS", "SPREADSHEET_MAX_CHARS",
                "SPREADSHEET_MAX_SHEETS", "SPREADSHEET_READ_ONLY"):
        monkeypatch.delenv(env, raising=False)
    limits = get_limits()
    assert limits["read_only"] is True
    assert limits["max_rows"] == DEFAULTS["max_rows"]
    assert limits["max_sheets"] == DEFAULTS["max_sheets"]


# ---------------------------------------------------------------- MSG

def test_msg_converter_reports_missing_library(tmp_path, monkeypatch):
    """未装 extract-msg 时应给出可执行提示，而不是抛 ImportError。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "extract_msg":
            raise ImportError("no module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "x.msg"
    p.write_bytes(b"dummy")
    with pytest.raises(Exception) as ei:
        MSGConverter().convert_to_document(str(p))
    assert "extract-msg" in str(ei.value)
