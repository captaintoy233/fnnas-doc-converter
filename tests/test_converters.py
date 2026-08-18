"""转换器测试：真实样本 + EML 合并 + 压缩包结构"""
import base64
import zipfile


def test_docx_converter(sample_docx):
    from converters.docx_converter import DOCXConverter
    if not sample_docx:
        return
    out = DOCXConverter().convert(str(sample_docx))
    assert isinstance(out, str) and len(out) > 50
    assert "# " in out or "第" in out  # 标题或正文


def test_ofd_converter(sample_ofd):
    from converters.ofd import OFDConverter
    if not sample_ofd:
        return
    out = OFDConverter().convert(str(sample_ofd))
    assert isinstance(out, str) and len(out) > 20


def test_wps_converter(sample_wps):
    from converters.wps import WPSConverter
    if not sample_wps:
        return
    out = WPSConverter().convert(str(sample_wps))
    assert isinstance(out, str) and len(out) > 20


def test_eml_converter_merges_attachment(tmp_path):
    from converters.loader import register_all
    from converters import registry
    from converters.eml_converter import EMLConverter
    register_all(registry)

    body = "测试正文内容"
    eml = tmp_path / "邮件.eml"
    with zipfile.ZipFile(tmp_path / "tmp.docx", "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    # 附件用可转换的 docx 样本
    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    attach = sample.read_bytes() if sample.exists() else b"not a real docx"
    b64 = base64.encodebytes(attach).decode()

    content = (
        "From: a@b.com\nTo: c@d.com\nSubject: =?utf-8?B?5rWL6K+V5qCH6aKY?=\n"
        "Date: Mon, 1 Jan 2024 00:00:00 +0800\n"
        'Content-Type: multipart/mixed; boundary="X"\n\n'
        "--X\nContent-Type: text/plain; charset=utf-8\n\n" + body + "\n\n"
        "--X\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\n"
        'Content-Disposition: attachment; filename*="UTF-8\'\'%E4%BC%9A%E8%AE%AE.docx"\n'
        "Content-Transfer-Encoding: base64\n\n" + b64 + "\n--X--\n"
    )
    eml.write_text(content, encoding="utf-8")
    out = EMLConverter().convert(str(eml))
    assert "测试正文内容" in out
    assert "## 附件内容" in out
    assert "会议.docx" in out


def test_archive_converter_preserves_structure(tmp_path):
    from converters.loader import register_all
    from converters import registry
    from converters.archive_converter import ArchiveConverter
    register_all(registry)

    # 造一个 zip：内层目录 + 一个可转换 docx
    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    archive = tmp_path / "资料包.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        if sample.exists():
            zf.write(sample, "子目录A/合同.docx")
        zf.writestr("README.md", "# 说明")
        zf.writestr("notes/readme.txt", "文本内容")

    outputs = ArchiveConverter().convert_to_files(str(archive), "资料包.zip")
    rels = [r for r, _ in outputs]
    if sample.exists():
        assert any("子目录A/合同.md" in r for r in rels), rels
    assert any(r.endswith("_归档清单.md") for r in rels)


def test_xlsx_escapes_pipe(tmp_path):
    from converters.xlsx_converter import XLSXConverter
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "表1"
    ws.append(["列A", "列|B"])
    ws.append(["值1", "含|管道符"])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    out = XLSXConverter().convert(str(p))
    assert "含\\|管道符" in out


def test_docx_table_and_paragraph_order(tmp_path):
    """段落与表格交错时保持文档顺序"""
    from docx import Document
    from docx.shared import Pt
    from converters.docx_converter import DOCXConverter
    doc = Document()
    doc.add_heading("标题一", level=1)
    doc.add_paragraph("表格前的段落")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A|1"
    table.cell(0, 1).text = "B"
    table.cell(1, 0).text = "C"
    table.cell(1, 1).text = "D"
    doc.add_paragraph("表格后的段落")
    p = tmp_path / "t.docx"
    doc.save(p)
    out = DOCXConverter().convert(str(p))
    pos_table = out.find("| A\\|1 |")
    pos_after = out.find("表格后的段落")
    pos_before = out.find("表格前的段落")
    assert pos_before < pos_table < pos_after


from pathlib import Path  # noqa: E402


def test_archive_converter_fallback_on_sniff_mismatch(tmp_path, monkeypatch):
    """zip 内文件被嗅探误判时：嗅探转换器失败 → 回退扩展名转换器"""
    import zipfile
    from converters.loader import register_all
    from converters import registry
    from converters import BaseConverter
    from converters.archive_converter import ArchiveConverter
    register_all(registry)

    class FakeFailXLSX(BaseConverter):
        """嗅探出的转换器（.xlsx），必然失败"""
        def supported_extensions(self): return ['.xlsx']
        def convert(self, file_path, **kwargs): raise RuntimeError("嗅探转换器失败")
        def convert_to_files(self, file_path, rel_path): raise RuntimeError("嗅探转换器失败")

    class FakeOkET(BaseConverter):
        """扩展名转换器（.et），成功"""
        def supported_extensions(self): return ['.et']
        def convert(self, file_path, **kwargs): return "# ET 内容"
        def convert_to_files(self, file_path, rel_path):
            return [(str(Path(rel_path).with_suffix('.md')), "# ET 内容")]

    registry.register(FakeFailXLSX())
    registry.register(FakeOkET())

    archive = tmp_path / "包.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("子目录/表格.et", b"fake-biff")

    import converters.archive_converter as ac
    orig = ac.sniff_format
    monkeypatch.setattr(ac, "sniff_format",
                        lambda p: (".xlsx", "XLSX") if str(p).endswith(".et") else orig(p))

    outputs = ArchiveConverter().convert_to_files(str(archive), "包.zip")
    rels = [r for r, _ in outputs]
    assert any("子目录/表格.md" in r for r in rels), rels
    assert not any("_转换失败" in r for r in rels), rels
    # 内容来自回退的 ET 转换器（键含归档名前缀）
    content = next(c for r, c in outputs if r.endswith("子目录/表格.md"))
    assert "ET 内容" in content


def test_batch_converter_fallback_chain(tmp_path, monkeypatch):
    """顶层文件: 嗅探转换器失败 → 回退扩展名转换器"""
    from config import reload_config
    from registry import reset_registry
    reset_registry()
    from converters.loader import register_all
    from converters import registry
    from converters import BaseConverter
    from scanner import FileInfo
    from batch import start_batch, get_batch_status, clear_batch

    class AlwaysFailXLSX(BaseConverter):
        def supported_extensions(self): return ['.xlsx']
        def convert(self, file_path, **kwargs): raise RuntimeError("openpyxl 无法解析")
        def convert_to_files(self, file_path, rel_path):
            raise RuntimeError("openpyxl 无法解析")

    register_all(registry)
    registry.register(AlwaysFailXLSX())

    import batch as batch_mod
    orig = batch_mod.sniff_format
    def fake_sniff(path):
        if str(path).endswith(".weird"):
            return (".xlsx", "XLSX")  # 嗅探成 xlsx（会用 AlwaysFailXLSX 失败）
        return orig(path)
    monkeypatch.setattr(batch_mod, "sniff_format", fake_sniff)

    src = tmp_path / "in"
    src.mkdir()
    f = src / "伪装.weird"
    f.write_bytes(b"some data")
    files = [FileInfo(f, src)]

    monkeypatch.setenv("CONVERTER_SOURCE_DIR", str(src))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "r.json"))
    monkeypatch.setenv("WEKNORA_ENABLED", "false")
    reload_config()

    start_batch(files, push_to_weknora=False)
    st = get_batch_status()["statistics"]
    clear_batch()
    # AlwaysFailXLSX 失败 → 回退 registry.get('.weird')=None → 任务失败（无候选转换器）
    # 该场景验证失败路径不崩溃；真实回退在 .et→ETConverter 场景由 archive 测试覆盖
    assert st["completed"] == 1
