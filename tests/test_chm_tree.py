"""CHM 目录树导出测试

覆盖:
- 按 .hhc 索引层级建文件夹、每文档一个 MD
- 废止状态按"祖先目录名"判定（不误判标题含"废止"的现行文件）
- 文件名净化 / 同目录去重 / 超长名截断
- CHM 内部 .htm 链接 → .md 重写
- 增量同步：新增 / 变更 / 未变 / 删除 的比对结果
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

from converters.chm_converter import CHMConverter, _safe_component  # noqa: E402


# ----------------------------------------------------------------------
# 模拟 CHM 解压结果 + .hhc 索引
# ----------------------------------------------------------------------

def _hhc(nodes, indent=0):
    """由嵌套结构生成 .hhc 文本

    nodes: [{"name": str, "local": str, "children": [...]}]
    """
    pad = "  " * indent
    out = ["{}<UL>".format(pad)]
    for n in nodes:
        out.append('{}<LI> <OBJECT type="text/sitemap">'.format(pad))
        out.append('{}<param name="Name" value="{}">'.format(pad, n["name"]))
        out.append('{}<param name="Local" value="{}">'.format(pad, n.get("local", "")))
        out.append('{}<param name="ImageNumber" value="11">'.format(pad))
        out.append("{}</OBJECT>".format(pad))
        if n.get("children"):
            out.append(_hhc(n["children"], indent + 1))
    out.append("{}</UL>".format(pad))
    return "\n".join(out)


def _make_extract(tmp_path, with_link=False):
    """构造模拟的 CHM 解压目录，返回 (root, files)"""
    root = tmp_path / "extract"
    (root / "01.外部规章" / "人民银行").mkdir(parents=True)
    (root / "25.已废止").mkdir(parents=True)

    def w(rel, body):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "<html><head><title>t</title></head><body>{}</body></html>".format(body),
            encoding="utf-8")
        return p

    w("01.外部规章/01.外部规章.htm", "外部规章索引")
    w("01.外部规章/人民银行/下岗失业人员小额担保贷款管理办法.htm", "第一条 贷款对象。")
    # 标题含"废止"但属现行文件（不应被判为废止）
    w("01.外部规章/人民银行/关于废止部分司法解释的决定.htm", "本决定自发布之日起施行。")
    w("25.已废止/b林权抵押担保管理办法.htm", "【2023年8月已废止】")
    w("0.htm", ('目录页 <a href="01.外部规章/人民银行/'
                '下岗失业人员小额担保贷款管理办法.htm">链接</a>')
      if with_link else "目录")
    # .hhc 未收录的孤儿页
    w("orphan.htm", "孤儿页面")

    toc = [
        {"name": "01.外部规章", "local": "", "children": [
            {"name": "01.外部规章", "local": "01.外部规章/01.外部规章.htm"},
            {"name": "人民银行", "local": "", "children": [
                {"name": "下岗失业人员小额担保贷款管理办法",
                 "local": "01.外部规章/人民银行/下岗失业人员小额担保贷款管理办法.htm"},
                {"name": "关于废止部分司法解释的决定",
                 "local": "01.外部规章/人民银行/关于废止部分司法解释的决定.htm"},
            ]},
        ]},
        {"name": "25.已废止", "local": "", "children": [
            {"name": "b林权抵押担保管理办法",
             "local": "25.已废止/b林权抵押担保管理办法.htm"},
        ]},
    ]
    (root / "book.hhc").write_text(_hhc(toc), encoding="utf-8")

    files = [p for p in root.rglob("*") if p.is_file()]
    return root, files


def _converter(files, **kw):
    conv = CHMConverter(workers=2, layout="tree", **kw)
    conv.extract = lambda fp, dest: files
    return conv


# ----------------------------------------------------------------------
# 目录树布局
# ----------------------------------------------------------------------

def test_tree_layout_mirrors_toc(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    paths = {d["rel_path"] for d in docs}

    # 分组节点建文件夹，叶子落在其索引位置
    assert "01.外部规章/01.外部规章.md" in paths
    assert "01.外部规章/人民银行/下岗失业人员小额担保贷款管理办法.md" in paths
    assert "25.已废止/b林权抵押担保管理办法.md" in paths
    # .hhc 未收录的孤儿进入 _未编目
    assert any(p.startswith("_未编目/") for p in paths)
    # 路径唯一
    assert len(paths) == len(docs)


def test_convert_to_files_tree_returns_nested_paths(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files, namespace_output=False)
    outputs = conv.convert_to_files("book.chm", "河南信贷手册.CHM")
    rels = {r for r, _ in outputs}
    assert "01.外部规章/人民银行/下岗失业人员小额担保贷款管理办法.md" in rels
    # 每个输出都带内容
    assert all(c.strip() for _, c in outputs)


def test_deprecated_status_from_ancestor_dir_only(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    by_path = {d["rel_path"]: d for d in docs}

    assert by_path["25.已废止/b林权抵押担保管理办法.md"]["status"] == "已废止"
    # 标题含"废止"但位于普通目录 → 仍为现行有效（关键：不误判）
    assert by_path["01.外部规章/人民银行/关于废止部分司法解释的决定.md"][
        "status"] == "现行有效"

    md = conv.render_document(by_path["25.已废止/b林权抵押担保管理办法.md"], "book.chm")
    assert "本文档已废止" in md
    assert 'status: "已废止"' in md


def test_front_matter_metadata(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    d = next(x for x in docs if x["rel_path"].endswith("下岗失业人员小额担保贷款管理办法.md"))
    md = conv.render_document(d, "book.chm")
    assert md.startswith("---\n")
    for key in ("title:", "status:", "toc_path:", "source_chm:", "content_hash:"):
        assert key in md


def test_no_front_matter_option(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files, front_matter=False)
    docs, _ = conv.to_toc_documents("book.chm")
    md = conv.render_document(docs[0], "book.chm")
    assert not md.startswith("---\n")


def test_link_rewrite_htm_to_md(tmp_path):
    _, files = _make_extract(tmp_path, with_link=True)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    link_map = conv.build_link_map(docs)
    # 0.htm 是 .hhc 未收录的孤儿页 → 独立成篇
    target = next(x for x in docs
                  if any(os.path.basename(f) == "0.htm" for f in x["source_files"]))
    assert target["rel_path"].startswith("_未编目/")
    md = conv.render_document(target, "book.chm", link_map)
    assert not any(l.endswith(".htm)") for l in md.split("\n"))
    assert ".md)" in md


def test_orphans_emitted_individually(tmp_path):
    """孤儿页应各自成篇，不应合并为一个巨型文档"""
    _, files = _make_extract(tmp_path)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    orphans = [d for d in docs if d["rel_path"].startswith("_未编目/")]
    assert len(orphans) >= 2                      # 0.htm 与 orphan.htm
    for d in orphans:
        assert len(d["source_files"]) == 1        # 每篇只对应一个源文件


def test_safe_component_sanitize_and_truncate():
    assert "/" not in _safe_component("a/b:c*d?e")
    assert _safe_component("   ") == "未命名"
    out = _safe_component("长" * 200)
    assert len(out.encode("utf-8")) <= 180
    assert out != _safe_component("长" * 199 + "短")  # 截断后不碰撞


def test_unique_names_in_same_dir(tmp_path):
    root = tmp_path / "extract"
    root.mkdir()
    (root / "a.htm").write_text("<html><body>A</body></html>", encoding="utf-8")
    (root / "b.htm").write_text("<html><body>B</body></html>", encoding="utf-8")
    files = [p for p in root.rglob("*") if p.is_file()]
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")  # 无 .hhc → 平铺
    # 两个孤儿但文件名不同 → 各自成条；路径必须唯一
    paths = [d["rel_path"] for d in docs]
    assert len(paths) == len(set(paths))


def test_layout_dispatch_chapter_compat(tmp_path, monkeypatch):
    """layout=chapter 仍走旧的章节合并路径"""
    _, files = _make_extract(tmp_path)
    conv = CHMConverter(workers=1, layout="chapter")
    conv.extract = lambda fp, dest: files
    monkeypatch.setattr(conv, "to_chapters", lambda fp: (
        [("第一章", [str(f) for f in files if f.suffix == ".htm"])], str(tmp_path)))
    out = conv.convert_to_files("book.chm", "book.chm")
    assert out and all(p.endswith(".md") for p, _ in out)
    assert all(p.startswith("book/") for p, _ in out)


# ----------------------------------------------------------------------
# 增量同步（导出脚本）
# ----------------------------------------------------------------------

def _load_export_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "export_chm.py"
    spec = importlib.util.spec_from_file_location("export_chm_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeConv(CHMConverter):
    """返回受测试控制的文档清单（work 指向独立临时目录，避免被清理连带删除输出）"""
    docs_source = []

    def __init__(self, *a, **kw):
        kw.setdefault("workers", 1)
        kw.setdefault("layout", "tree")
        super().__init__(*a, **kw)

    def to_toc_documents(self, file_path, work=None):
        return [dict(d) for d in type(self).docs_source], tempfile.mkdtemp(prefix="fakechm_")


def _doc(rel, body, status="现行有效", toc=None):
    f = tempfile.NamedTemporaryFile("w", suffix=".htm", delete=False, encoding="utf-8")
    f.write("<html><body>{}</body></html>".format(body))
    f.close()
    return {
        "rel_path": rel,
        "title": Path(rel).stem,
        "toc_path": toc or rel[:-3],
        "status": status,
        "source_files": [f.name],
        "source_hash": hashlib.sha1(body.encode()).hexdigest(),
    }


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    mod = _load_export_module()
    import converters.chm_converter as chm_mod
    monkeypatch.setattr(chm_mod, "CHMConverter", _FakeConv)
    chm = tmp_path / "book.chm"
    chm.write_bytes(b"fake")
    out = tmp_path / "out"

    def run(*extra):
        # 固定 --workers 1：测试内走进程内顺序路径，避免 fork 干扰 monkeypatch
        monkeypatch.setattr(sys, "argv",
                            ["export_chm.py", str(chm), str(out), "--workers", "1"]
                            + list(extra))
        assert mod.main() == 0
        report = None
        rp = out / "_sync_report.json"
        if rp.is_file():
            report = json.loads(rp.read_text(encoding="utf-8"))
        return out, report

    return mod, out, run


def test_incremental_manifest_diff(fake_env):
    _, out, run = fake_env

    # 第 1 次：2 篇新增
    _FakeConv.docs_source = [_doc("A.md", "alpha"), _doc("D/b.md", "beta")]
    out, r1 = run()
    assert r1["counts"] == {"total": 2, "added": 2, "modified": 0,
                            "unchanged": 0, "deleted": 0, "failed": 0}
    assert (out / "A.md").is_file()
    assert (out / "D" / "b.md").is_file()
    for meta in ("_manifest.json", "_index.md", "_废止清单.md"):
        assert (out / meta).is_file(), meta

    # 第 2 次：内容不变 → 全部 unchanged
    out, r2 = run()
    assert (r2["counts"]["added"], r2["counts"]["modified"]) == (0, 0)
    assert r2["counts"]["unchanged"] == 2

    # 第 3 次：文档集不变、仅 D/b 内容变更 → 只重渲染 1 篇（真·增量）
    _FakeConv.docs_source = [_doc("A.md", "alpha"), _doc("D/b.md", "beta-v2")]
    out, r3 = run()
    assert r3["counts"]["added"] == 0
    assert r3["counts"]["modified"] == 1     # D/b
    assert r3["counts"]["unchanged"] == 1    # A

    # 第 4 次：文档集变化（A 从索引消失、新增 C）→ 链接映射变化，
    # 触发输出指纹变化 → 全量重渲染，且过期项被报告
    _FakeConv.docs_source = [_doc("D/b.md", "beta-v2"), _doc("C.md", "gamma")]
    out, r4 = run()
    assert r4["counts"]["total"] == 2
    assert r4["counts"]["deleted"] == 1      # A
    assert "A.md" in r4["deleted"]
    # 默认不物理删除过期文件（等待人工确认/推送管线删除）
    assert (out / "A.md").is_file()


def test_prune_removes_stale(fake_env):
    _, out, run = fake_env

    _FakeConv.docs_source = [_doc("A.md", "a"), _doc("B.md", "b")]
    out, _ = run()
    assert (out / "B.md").is_file()

    _FakeConv.docs_source = [_doc("A.md", "a")]
    out, rep = run("--prune")
    assert not (out / "B.md").exists()
    assert (out / "A.md").is_file()
    assert rep["pruned"] is True


def test_deprecated_list_generated(fake_env):
    _, out, run = fake_env

    _FakeConv.docs_source = [
        _doc("new.md", "现行"),
        _doc("25.已废止/old.md", "旧制度", status="已废止", toc="25.已废止/old"),
    ]
    out, _ = run()

    dep = (out / "_废止清单.md").read_text(encoding="utf-8")
    assert "old" in dep
    assert "new" not in dep

    m = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
    assert m["document_count"] == 2
    assert m["deprecated_count"] == 1
    # 清单记录状态与哈希，供推送管线判断
    assert m["documents"]["25.已废止/old.md"]["status"] == "已废止"
    assert m["documents"]["25.已废止/old.md"]["source_hash"]


def test_stale_detected_without_manifest_entry(fake_env):
    """回归：未被 prune 的过期文件脱离清单后，仍应能被识别并清理（自愈）"""
    _, out, run = fake_env
    _FakeConv.docs_source = [_doc("A.md", "a")]
    out, _ = run()

    # 模拟上一轮遗留的过期文件（清单中已不存在）
    (out / "ghost.md").write_text("leftover", encoding="utf-8")

    out, rep = run()
    assert "ghost.md" in rep["deleted"]          # 仍被识别
    assert (out / "ghost.md").is_file()          # 默认不删

    out, rep = run("--prune")
    assert not (out / "ghost.md").exists()       # prune 可清理


def test_output_fingerprint_change_forces_full_rerender(fake_env):
    """渲染指纹变化（如切换 front-matter / 布局 / 渲染版本）必须全量重渲染，
    否则源文件未变但链接或格式已过期的文档会被错误跳过"""
    _, out, run = fake_env
    _FakeConv.docs_source = [_doc("A.md", "a"), _doc("D/b.md", "b")]

    out, r1 = run()
    assert r1["counts"]["added"] == 2

    # 指纹未变 → 全部跳过
    out, r2 = run()
    assert r2["counts"]["unchanged"] == 2

    # 切换 --no-front-matter → 指纹变化 → 全量重渲染
    out, r3 = run("--no-front-matter")
    assert r3["counts"]["added"] == 2
    assert r3["counts"]["unchanged"] == 0
    # 重渲染后确实去掉了 front-matter
    assert not (out / "A.md").read_text(encoding="utf-8").startswith("---\n")

    m = json.loads((out / "_manifest.json").read_text(encoding="utf-8"))
    assert m["front_matter"] is False
    assert m["render_fingerprint"]


def test_force_rewrites_all(fake_env):
    _, out, run = fake_env
    _FakeConv.docs_source = [_doc("A.md", "a")]
    out, r1 = run()
    assert r1["counts"]["added"] == 1

    out, r2 = run("--force")
    assert r2["counts"]["added"] == 1     # --force 视为全部重转
    assert r2["counts"]["unchanged"] == 0


# ----------------------------------------------------------------------
# 输出命名空间（多本 CHM 共用输出根目录时避免混树）
# ----------------------------------------------------------------------

def test_tree_output_namespaced_by_default(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files)
    outs = conv.convert_to_files("book.chm", "河南信贷手册.CHM")
    rels = {r for r, _ in outs}
    assert rels, "应产出文档"
    assert all(r.startswith("河南信贷手册/") for r in rels)
    assert "河南信贷手册/01.外部规章/01.外部规章.md" in rels


def test_tree_namespace_can_be_disabled(tmp_path):
    _, files = _make_extract(tmp_path)
    conv = _converter(files, namespace_output=False)
    outs = conv.convert_to_files("book.chm", "河南信贷手册.CHM")
    rels = {r for r, _ in outs}
    assert "01.外部规章/01.外部规章.md" in rels
    assert not any(r.startswith("河南信贷手册/") for r in rels)


def test_namespace_keeps_image_paths_consistent(tmp_path):
    """图片落盘位置必须与 MD 链接一致（都在命名空间内）"""
    root = tmp_path / "extract"
    (root / "cat").mkdir(parents=True)
    img = root / "cat" / "doc.files" / "pic.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (root / "cat" / "doc.htm").write_text(
        '<html><body><p>正文</p><img src="doc.files/pic.png"></body></html>',
        encoding="utf-8")
    toc = [{"name": "cat", "local": "", "children": [
        {"name": "doc", "local": "cat/doc.htm"}]}]
    (root / "book.hhc").write_text(_hhc(toc), encoding="utf-8")
    files = [p for p in root.rglob("*") if p.is_file()]

    out = tmp_path / "out"
    out.mkdir()
    conv = _converter(files, image_mode="link")
    conv.assets_root = str(out)
    outs = conv.convert_to_files("book.chm", "手册A.CHM")
    assert outs

    rel, content = outs[0]
    assert rel.startswith("手册A/"), rel
    # MD 中链接应指向同命名空间内的图片
    assert "doc.files/pic.png" in content
    copied = out / "手册A" / "cat" / "doc.files" / "pic.png"
    assert copied.is_file(), "图片应与 MD 落在同一命名空间下"


# ----------------------------------------------------------------------
# 空壳辅助页过滤（header/tabstrip/tabscript），不得误伤有内容的 sheet#/file#
# ----------------------------------------------------------------------

def _make_scaffold_extract(tmp_path):
    root = tmp_path / "extract2"
    (root / "cat").mkdir(parents=True)
    for name in ("header.htm", "tabstrip.htm", "tabscript.htm",
                 "sheet001.htm", "file0001.htm", "真文档.htm"):
        (root / "cat" / name).write_text(
            "<html><body>{} 内容</body></html>".format(name), encoding="utf-8")
    toc = [{"name": "cat", "local": "", "children": [
        {"name": n, "local": "cat/" + n} for n in
        ("header.htm", "tabstrip.htm", "tabscript.htm",
         "sheet001.htm", "file0001.htm", "真文档.htm")]}]
    (root / "book.hhc").write_text(_hhc(toc), encoding="utf-8")
    return [p for p in root.rglob("*") if p.is_file()]


def test_scaffold_pages_skipped(tmp_path):
    files = _make_scaffold_extract(tmp_path)
    conv = _converter(files)
    docs, _ = conv.to_toc_documents("book.chm")
    names = {Path(d["rel_path"]).name for d in docs}

    assert "header.htm.md" not in names
    assert "tabstrip.htm.md" not in names
    assert "tabscript.htm.md" not in names
    # 有内容的必须保留
    assert "sheet001.htm.md" in names
    assert "file0001.htm.md" in names
    assert "真文档.htm.md" in names
    assert conv.skipped_scaffold == 3


def test_scaffold_filter_can_be_disabled(tmp_path):
    files = _make_scaffold_extract(tmp_path)
    conv = _converter(files, skip_scaffold=False)
    docs, _ = conv.to_toc_documents("book.chm")
    names = {Path(d["rel_path"]).name for d in docs}
    assert "header.htm.md" in names
    assert "tabstrip.htm.md" in names
    assert conv.skipped_scaffold == 0


def test_multiprocess_render_path(tmp_path):
    """大文档集走多进程渲染分支（阈值调为 1 以强制触发）"""
    _, files = _make_extract(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    conv = _converter(files, render_processes=True, mp_threshold=1,
                      image_mode="link")
    conv.assets_root = str(out)
    outputs = conv.convert_to_files("book.chm", "手册.CHM")

    rels = {r for r, _ in outputs}
    assert rels, "多进程分支应产出文档"
    assert all(r.startswith("手册/") for r in rels)
    assert any(r.endswith("下岗失业人员小额担保贷款管理办法.md") for r in rels)
    assert all(c.strip() for _, c in outputs)


def test_mp_threshold_falls_back_to_threads(tmp_path):
    """小批量应走线程分支（不额外起进程）"""
    _, files = _make_extract(tmp_path)
    conv = _converter(files, render_processes=True, mp_threshold=10_000,
                      namespace_output=False)
    outputs = conv.convert_to_files("book.chm", "book.chm")
    assert outputs and all(p.endswith(".md") for p, _ in outputs)
