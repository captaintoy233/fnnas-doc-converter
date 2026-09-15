"""源文件消失处理测试

覆盖：
- 从知识库删除该源文件产出的所有文档（一个源文件可能产出多篇）
- 源文件归档
- 注册表记录清理
- 安全护栏：空扫描不执行、开关关闭不执行、非全量批次不执行
- 单条失败不影响其余
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

import batch  # noqa: E402


class FakeRegistry:
    def __init__(self, records=None):
        self.data = dict(records or {})
        self.deleted = []

    def all_files(self):
        return set(self.data)

    def get(self, path):
        return self.data.get(path)

    def delete(self, path):
        self.deleted.append(path)
        self.data.pop(path, None)


def _fake_client_factory(deleted, fail_on=()):
    class FakeClient:
        def __init__(self, cfg=None):
            pass

        def delete_knowledge(self, doc_id):
            if doc_id in fail_on:
                raise RuntimeError("boom")
            deleted.append(doc_id)
            return {"ok": True}
    return FakeClient


def _install(monkeypatch, registry, deleted, fail_on=()):
    monkeypatch.setattr(batch, "get_registry", lambda: registry)
    monkeypatch.setattr("weknora_client.WeKnoraClient",
                        _fake_client_factory(deleted, fail_on))


def test_deletes_all_docs_and_archives(tmp_path, monkeypatch):
    src = tmp_path / "已删除的通知.htm"
    src.write_text("x", encoding="utf-8")
    reg = FakeRegistry({
        str(src): {"weknora_doc_id": "doc-root",
                   "output_docs": {"a.md": "doc-a", "b.md": "doc-b"}},
    })
    deleted = []
    _install(monkeypatch, reg, deleted)

    archive = tmp_path / "archive"
    cfg = {"weknora": {"enabled": True}, "archive": {"dir": str(archive)}}
    keep = SimpleNamespace(path=str(tmp_path / "仍在.htm"))
    info = batch._handle_missing_sources([keep], cfg)

    assert info["missing"] == 1
    assert info["deleted"] == 1
    assert info["archived"] == 1
    assert sorted(deleted) == ["doc-a", "doc-b", "doc-root"]
    # 源文件被移入归档目录
    assert not src.exists()
    assert (archive / "已删除的通知.htm").is_file()
    # 注册表记录被清理
    assert reg.deleted == [str(src)]


def test_archive_can_be_disabled(tmp_path, monkeypatch):
    """未配置归档目录时：只从知识库删除，不动源文件"""
    src = tmp_path / "gone.htm"
    src.write_text("x", encoding="utf-8")
    reg = FakeRegistry({str(src): {"weknora_doc_id": "d1", "output_docs": {}}})
    deleted = []
    _install(monkeypatch, reg, deleted)

    info = batch._handle_missing_sources(
        [SimpleNamespace(path=str(tmp_path / "other.htm"))],
        {"weknora": {"enabled": True}, "archive": {"dir": ""}})

    assert info["deleted"] == 1
    assert info["archived"] == 0
    assert src.exists(), "未配置归档时不应移动源文件"
    assert deleted == ["d1"]


def test_empty_scan_is_skipped(tmp_path, monkeypatch):
    """安全护栏：扫描结果为 0 时不执行（疑似挂载失败，避免误删整库）"""
    reg = FakeRegistry({"/old/a.htm": {"weknora_doc_id": "d1"}})
    deleted = []
    _install(monkeypatch, reg, deleted)

    info = batch._handle_missing_sources([], {"weknora": {"enabled": True}})

    assert info["skipped"] == "empty_scan"
    assert info["deleted"] == 0
    assert deleted == []
    assert reg.all_files(), "记录不应被清理"


def test_doc_id_deduplicated(tmp_path, monkeypatch):
    """同一 doc_id 同时出现在 weknora_doc_id 与 output_docs 时只删一次"""
    src = tmp_path / "a.htm"
    src.write_text("x", encoding="utf-8")
    reg = FakeRegistry({str(src): {"weknora_doc_id": "same",
                                   "output_docs": {"a.md": "same"}}})
    deleted = []
    _install(monkeypatch, reg, deleted)

    info = batch._handle_missing_sources(
        [SimpleNamespace(path=str(tmp_path / "keep.htm"))],
        {"weknora": {"enabled": True}, "archive": {"dir": ""}})

    assert deleted == ["same"]
    assert info["doc_ids"] == 1


def test_delete_failure_does_not_abort(tmp_path, monkeypatch):
    """单条删除失败不应中断其余条目的处理"""
    a = tmp_path / "a.htm"; a.write_text("x", encoding="utf-8")
    b = tmp_path / "b.htm"; b.write_text("x", encoding="utf-8")
    reg = FakeRegistry({
        str(a): {"weknora_doc_id": "bad", "output_docs": {}},
        str(b): {"weknora_doc_id": "good", "output_docs": {}},
    })
    deleted = []
    _install(monkeypatch, reg, deleted, fail_on=("bad",))

    info = batch._handle_missing_sources(
        [SimpleNamespace(path=str(tmp_path / "keep.htm"))],
        {"weknora": {"enabled": True}, "archive": {"dir": ""}})

    assert info["failed"] == 1
    assert "good" in deleted
    assert info["deleted"] == 2, "两条记录都应被清理"


def test_no_missing_is_noop(tmp_path, monkeypatch):
    src = tmp_path / "a.htm"; src.write_text("x", encoding="utf-8")
    reg = FakeRegistry({str(src): {"weknora_doc_id": "d1"}})
    deleted = []
    _install(monkeypatch, reg, deleted)

    info = batch._handle_missing_sources(
        [SimpleNamespace(path=str(src))], {"weknora": {"enabled": True}})

    assert info["missing"] == 0
    assert info["deleted"] == 0
    assert deleted == []


# ----------------------------------------------------------------------
# 批次开关：仅"全量扫描 + 配置开启"才执行
# ----------------------------------------------------------------------

def test_start_batch_gate(monkeypatch, tmp_path):
    """full_scan=False 或配置未开启时，不应触碰源文件消失处理"""
    calls = []
    monkeypatch.setattr(batch, "_handle_missing_sources",
                        lambda files, cfg: calls.append(1) or {})
    monkeypatch.setattr(batch, "_convert_single",
                        lambda task, out, cfg: task)

    src = tmp_path / "a.htm"
    src.write_text("x", encoding="utf-8")
    files = [SimpleNamespace(path=src, rel_path="a.htm", ext=".htm",
                             size=1, mtime=0.0)]

    import config as cfgmod
    base = cfgmod.get_config()
    monkeypatch.setitem(base, "registry", {"handle_missing": True,
                                           "prune_on_batch": False})
    # 关掉推送，避免真实网络
    monkeypatch.setitem(base, "weknora", {"enabled": False})
    batch.clear_batch()
    batch.start_batch(files, push_to_weknora=False, full_scan=False)
    assert calls == [], "非全量批次不应执行"
    batch.clear_batch()

    batch.start_batch(files, push_to_weknora=False, full_scan=True)
    assert calls == [1], "全量批次且开关开启时应执行"
    batch.clear_batch()


def test_scoped_scan_does_not_trigger_missing(tmp_path, monkeypatch):
    """安全护栏：指定 source_dir 的局部扫描不得触发源文件消失处理"""
    import inspect
    src = inspect.getsource(__import__("main").start_batch_conversion)
    assert "full_scan=(source_dir is None)" in src, \
        "局部扫描必须传 full_scan=False，避免范围外文件被误归档"


def test_missing_sources_exposed_in_status():
    """批次状态必须上报源文件消失处理结果（否则运维无从知晓）"""
    import inspect
    src = inspect.getsource(batch.get_batch_status)
    assert "missing_sources" in src


# ----------------------------------------------------------------------
# 嗅探路由：通用文本兜底不得覆盖扩展名转换器
# ----------------------------------------------------------------------

def test_generic_text_sniff_does_not_hijack_extension(tmp_path, monkeypatch):
    """内容像文本的未知扩展名文件，不应被 .txt 兜底劫持到文本转换器

    回归背景：注册 .txt/.md/.csv 后，嗅探器对任何非二进制内容都返回 .txt，
    导致（如）.multi 这类自定义扩展名被文本转换器接管，
    既覆盖了正确的扩展名转换器，也可能把损坏文件读成乱码。
    """
    from converters import BaseConverter, registry
    from converters.loader import register_all

    class MultiConv(BaseConverter):
        def supported_extensions(self):
            return ['.multi']

        def convert(self, file_path, **kwargs):
            return "# 正确内容"

    register_all(registry)
    registry.register(MultiConv())

    f = tmp_path / "a.multi"
    f.write_bytes(b"x")                      # 非二进制内容 → 嗅探兜底为 .txt

    cands, real_ext, _ = batch._pick_converter(".multi", f)
    assert MultiConv in [type(c) for c in cands], \
        "自定义扩展名转换器必须仍在候选首位"
    assert type(cands[0]).__name__ == "MultiConv", \
        "不应被文本兜底劫持（当前: %s）" % type(cands[0]).__name__
