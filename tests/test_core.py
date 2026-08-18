"""路径映射 / 嗅探器 / 注册表 测试"""
import json
import os
from pathlib import Path


def test_windows_path_mapping():
    from paths import to_local_path
    cfg = {"converter": {"drive_map": {"C:": "/mnt/c"}}}
    assert str(to_local_path(r"C:\Users\Administrator\Documents\AI", cfg)) == \
        "/mnt/c/Users/Administrator/Documents/AI"
    assert str(to_local_path(r"C:\Users\Administrator\Documents\AI-OUT", cfg)) == \
        "/mnt/c/Users/Administrator/Documents/AI-OUT"
    # 无映射时按 /mnt/<盘符> 推断
    assert str(to_local_path(r"D:\data", {"converter": {"drive_map": {}}})) == "/mnt/d/data"
    # POSIX 路径原样
    assert str(to_local_path("/data/input", cfg)) == "/data/input"


def test_sniffer(sample_docx, sample_ofd, sample_wps):
    from sniffer import sniff_format
    if sample_docx:
        assert sniff_format(str(sample_docx))[0] == ".docx"
    if sample_ofd:
        assert sniff_format(str(sample_ofd))[0] == ".ofd"
    if sample_wps:
        assert sniff_format(str(sample_wps))[0] == ".wps"


def test_sniffer_pdf_header(tmp_path):
    from sniffer import sniff_format
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"%PDF-1.4 fake content")
    assert sniff_format(str(f))[0] == ".pdf"


def test_sniffer_zip_payload(tmp_path):
    import zipfile
    from sniffer import sniff_format
    # 造一个扩展名 .wps 实为 docx 的文件
    f = tmp_path / "伪装.wps"
    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    ext, name = sniff_format(str(f))
    assert ext == ".docx", f"expected .docx got {ext}"


def test_registry_incremental(tmp_path):
    from registry import SyncRegistry
    reg = SyncRegistry(str(tmp_path / "registry.json"))
    src = tmp_path / "a.wps"
    src.write_bytes(b"content-v1")

    assert reg.needs_convert(str(src)) is True
    h1 = reg.sha256_of_file(str(src))
    reg.record(str(src), h1, output_rel="a.md", converted_hash="mdhash")
    assert reg.needs_convert(str(src), h1) is False

    src.write_bytes(b"content-v2")
    h2 = reg.sha256_of_file(str(src))
    assert reg.needs_convert(str(src), h2) is True

    reg.mark_pushed(str(src), "doc-123")
    rec = reg.get(str(src))
    assert rec["pushed"] is True and rec["weknora_doc_id"] == "doc-123"
    # 指纹保持 record 时的值（mark_pushed 只更新推送状态）
    assert rec["source_hash"] == h1

    # 持久化
    reg2 = SyncRegistry(str(tmp_path / "registry.json"))
    assert reg2.get(str(src))["source_hash"] == h1
    assert reg2.get(str(src))["pushed"] is True


def test_zip_extract_slip_protection(tmp_path):
    import zipfile
    from extract import extract_zip
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil.txt", "bad")
        zf.writestr("ok/file.txt", "fine")
    dest = tmp_path / "out"
    dest.mkdir()
    files = extract_zip(archive, dest)
    names = [str(Path(f).relative_to(dest)) for f in files]
    assert "ok/file.txt" in names
    assert not (tmp_path / "evil.txt").exists()


def test_folder_mappings_resolve_longest_prefix(tmp_path, monkeypatch):
    """folder→KB 映射：最长前缀优先"""
    import folder_mappings
    mappings = {
        "工作邮件": "kb-email",
        "工作邮件/李明磊邮件": "kb-lml",
        "报表数据": "kb-report",
    }
    # 一级目录
    assert folder_mappings.resolve_kb_id("工作邮件/a.eml", mappings, "kb-default") == "kb-email"
    # 最长前缀优先
    assert folder_mappings.resolve_kb_id(
        "工作邮件/李明磊邮件/b.eml", mappings, "kb-default") == "kb-lml"
    # 未映射 → 默认
    assert folder_mappings.resolve_kb_id("其他/x.docx", mappings, "kb-default") == "kb-default"
    # 空映射 → 默认
    assert folder_mappings.resolve_kb_id("a/b.docx", {}, "kb-default") == "kb-default"


def test_folder_mappings_save_load(tmp_path, monkeypatch):
    import folder_mappings
    monkeypatch.setenv("WEKNORA_FOLDER_KB_MAP", str(tmp_path / "map.json"))
    from config import reload_config
    reload_config()
    data = folder_mappings.save_mappings({"工作邮件": "kb-1"})
    assert data is not None
    assert folder_mappings.get_mappings() == {"工作邮件": "kb-1"}


def test_sqlite_registry(tmp_path):
    """SQLite registry 后端：同接口 + 相对路径键"""
    from registry import SqliteSyncRegistry
    reg = SqliteSyncRegistry(str(tmp_path / "reg.sqlite"),
                             source_dir=str(tmp_path / "src"))
    src = tmp_path / "src" / "子目录" / "a.docx"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"v1")

    assert reg.needs_convert(str(src)) is True
    h1 = reg.sha256_of_file(str(src))
    reg.record(str(src), h1, output_rel="子目录/a.md", output_files=["子目录/a.md"])
    assert reg.needs_convert(str(src), h1) is False
    # 相对路径键
    rec = reg.get(str(src))
    assert rec["output_files"] == ["子目录/a.md"]

    # 内容变化 → 需要重转
    src.write_bytes(b"v2")
    h2 = reg.sha256_of_file(str(src))
    assert reg.needs_convert(str(src), h2) is True

    # output_docs 追踪（真实流程：record_output_doc + mark_pushed）
    reg.record_output_doc(str(src), "子目录/a.md", "doc-1")
    reg.mark_pushed(str(src), "doc-1")
    assert reg.get(str(src))["output_docs"] == {"子目录/a.md": "doc-1"}
    assert reg.get(str(src))["pushed"] is True

    # 持久化
    reg2 = SqliteSyncRegistry(str(tmp_path / "reg.sqlite"), source_dir=str(tmp_path / "src"))
    assert reg2.get(str(src))["weknora_doc_id"] == "doc-1"
    assert reg2.to_dict()["子目录/a.docx"]["output_files"] == ["子目录/a.md"]
