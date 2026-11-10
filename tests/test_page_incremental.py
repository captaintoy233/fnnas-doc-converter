# -*- coding: utf-8 -*-
"""页级增量测试：registry 记录/更新页指纹；CHM 重转时未变化的页复用磁盘产物。"""
import hashlib
import os

from registry import SyncRegistry, SqliteSyncRegistry


def test_registry_page_hashes(tmp_path):
    reg = SyncRegistry(str(tmp_path / "r.json"))
    src = tmp_path / "a.chm"
    src.write_bytes(b"x")
    h = reg.sha256_of_file(str(src))
    pages = {"a/x.md": "h1", "a/y.md": "h2"}
    reg.record(str(src), h, output_files=list(pages), page_hashes=pages)
    rec = reg.get(str(src))
    assert rec["page_hashes"] == pages
    # 单页更新不丢其他页指纹
    reg.set_page_hash(str(src), "a/z.md", "h3")
    rec = reg.get(str(src))
    assert rec["page_hashes"]["a/z.md"] == "h3"
    assert rec["page_hashes"]["a/x.md"] == "h1"


def test_sqlite_page_hashes(tmp_path):
    reg = SqliteSyncRegistry(str(tmp_path / "r.sqlite"))
    src = tmp_path / "a.chm"
    src.write_bytes(b"x")
    h = reg.sha256_of_file(str(src))
    reg.record(str(src), h, output_files=["a/x.md"], page_hashes={"a/x.md": "h1"})
    assert reg.get(str(src))["page_hashes"] == {"a/x.md": "h1"}
    reg.set_page_hash(str(src), "a/y.md", "h2")
    assert reg.get(str(src))["page_hashes"]["a/y.md"] == "h2"


def _make_converter(tmp_path):
    from converters.chm_converter import CHMConverter
    conv = CHMConverter.__new__(CHMConverter)
    conv.layout = "tree"
    conv.front_matter = False
    conv.image_mode = "none"
    conv.image_max_mb = 5.0
    conv.assets_root = str(tmp_path / "out")
    conv.ocr_cfg = {}
    conv.namespace_output = False
    conv.skip_scaffold = True
    conv.render_processes = False
    conv.workers = 2
    conv.mp_threshold = 50
    conv.prior_page_hashes = {}
    conv.chm_root = ""
    os.makedirs(conv.assets_root, exist_ok=True)
    return conv


def test_chm_reuses_unchanged_pages(tmp_path, monkeypatch):
    conv = _make_converter(tmp_path)
    docs = [
        {"rel_path": "d1/p1.md", "title": "制度一", "toc_path": "d1",
         "status": "现行有效", "source_files": [], "source_hash": "s1"},
        {"rel_path": "d1/p2.md", "title": "制度二", "toc_path": "d1",
         "status": "现行有效", "source_files": [], "source_hash": "s2"},
    ]
    (tmp_path / "out" / "d1").mkdir(parents=True, exist_ok=True)
    content1 = "# 制度一\n\n正文不变"
    (tmp_path / "out" / "d1" / "p1.md").write_text(content1, encoding="utf-8")
    conv.prior_page_hashes = {
        "d1/p1.md": hashlib.sha256(content1.encode("utf-8")).hexdigest(),
        "d1/p2.md": "stale",
    }
    rendered = []

    def fake_render(doc, chm_name="", link_map=None, img_stats=None):
        rendered.append(doc["rel_path"])
        return "# new " + doc["title"]

    monkeypatch.setattr(conv, "render_document", fake_render)
    outs = conv.convert_tree_to_files("fake.chm", "fake.chm",
                                      docs=docs, work=str(tmp_path))
    rels = {r for r, _ in outs}
    assert rels == {"d1/p1.md", "d1/p2.md"}
    assert rendered == ["d1/p2.md"], "只应重渲染变化页，实际 {}".format(rendered)
    dmap = dict(outs)
    assert dmap["d1/p1.md"] == content1            # 未变页原样复用
    assert dmap["d1/p2.md"].startswith("# new")    # 变化页重渲染
