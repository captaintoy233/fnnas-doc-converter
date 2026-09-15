"""共享图片管线测试（以 HTMConverter 为载体，覆盖 link/base64/占位/OCR 注入）

背景：图片管线原先只存在于 CHM 转换器，普通 .htm/.html 走注册表时
拿不到"图片落盘 + OCR 文字注入"。此测试锁定 HTML 路径同样具备该能力。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

import ocr  # noqa: E402
from converters.htm_converter import HTMConverter  # noqa: E402


def _png(path: Path, w=120, h=40):
    """写一张真实 PNG（内容哈希需要真实文件）"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    cv2.imwrite(str(path), np.full((h, w, 3), 200, dtype=np.uint8))


def _make_html(tmp_path, img_name="pic.png", missing=False):
    asset_dir = tmp_path / "doc.files"
    asset_dir.mkdir(parents=True, exist_ok=True)
    if not missing:
        _png(asset_dir / img_name)
    html = (
        "<html><head><title>带图文档</title></head><body>"
        "<p>正文一段</p>"
        '<img src="doc.files/{}">'
        "<p>正文二段</p>"
        "</body></html>"
    ).format("nope.png" if missing else img_name)
    p = tmp_path / "doc.htm"
    p.write_text(html, encoding="utf-8")
    return p, asset_dir / img_name


def _ocr_cfg(tmp_path, text="图片里的文字"):
    cache = tmp_path / "cache"
    cfg = ocr.default_config()
    cfg.update({"enabled": True, "cache_dir": str(cache)})
    return cfg, cache


def test_link_mode_copies_image_and_rewrites_link(tmp_path):
    src, img = _make_html(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    conv = HTMConverter(image_mode="link", assets_root=str(out))
    rel, content = conv.convert_to_files(str(src), "文档/doc.htm")[0]

    assert rel == "文档/doc.md"
    assert "![ ]" not in content
    assert "doc.files/pic.png" in content
    copied = out / "文档" / "doc.files" / "pic.png"
    assert copied.is_file()          # 图片落到 MD 同级
    assert copied.read_bytes() == img.read_bytes()


def test_link_mode_without_assets_root_keeps_original_ref(tmp_path):
    """单文件 API 场景未给输出根目录：不得改写成指向不存在目录的死链接"""
    src, _ = _make_html(tmp_path)
    conv = HTMConverter(image_mode="link", assets_root="")
    content = conv.convert(str(src))
    assert "doc.files/pic.png" in content
    # 没有凭空造出 .files 目录
    assert not (tmp_path / "doc.md.files").exists()


def test_missing_image_becomes_placeholder(tmp_path):
    src, _ = _make_html(tmp_path, missing=True)
    out = tmp_path / "out"
    out.mkdir()
    conv = HTMConverter(image_mode="link", assets_root=str(out))
    content = conv.convert(str(src))
    assert "（原图缺失：nope.png）" in content
    assert "![" not in content


def test_missing_image_placeholder_can_be_disabled(tmp_path):
    src, _ = _make_html(tmp_path, missing=True)
    cfg, _ = _ocr_cfg(tmp_path)
    cfg["placeholder_missing"] = False
    conv = HTMConverter(image_mode="link", assets_root=str(tmp_path / "o"),
                        ocr_cfg=cfg)
    (tmp_path / "o").mkdir()
    content = conv.convert(str(src))
    assert "（原图缺失" not in content
    assert "![" in content


def test_ocr_text_injected_from_cache(tmp_path):
    src, img = _make_html(tmp_path)
    cfg, cache = _ocr_cfg(tmp_path, "审批节点：部门初审 → 风控复核")
    # 预置 OCR 缓存（模拟预处理阶段的结果）
    key = ocr.image_key(str(img))
    ocr.cache_save(str(cache), key, {
        "key": key, "width": 120, "height": 40, "tiled": False,
        "text": "审批节点：部门初审 → 风控复核", "blocks": [],
    })
    out = tmp_path / "out"
    out.mkdir()
    conv = HTMConverter(image_mode="link", assets_root=str(out), ocr_cfg=cfg)
    stats = {}
    content = conv._convert_with_images(str(src), "doc.md", stats)

    assert "ocr:begin" in content and "ocr:end" in content
    assert "审批节点：部门初审 → 风控复核" in content
    assert stats.get("ocr_hit") == 1
    assert stats.get("ocr_chars") == len("审批节点：部门初审 → 风控复核")


def test_ocr_disabled_no_injection(tmp_path):
    src, img = _make_html(tmp_path)
    cfg, cache = _ocr_cfg(tmp_path)
    key = ocr.image_key(str(img))
    ocr.cache_save(str(cache), key, {"key": key, "text": "不该出现", "blocks": []})
    cfg["enabled"] = False
    out = tmp_path / "out"
    out.mkdir()
    conv = HTMConverter(image_mode="link", assets_root=str(out), ocr_cfg=cfg)
    content = conv.convert(str(src))
    assert "ocr:begin" not in content
    assert "不该出现" not in content


def test_base64_mode_inlines(tmp_path):
    src, _ = _make_html(tmp_path)
    conv = HTMConverter(image_mode="base64", image_max_mb=5)
    content = conv.convert(str(src))
    assert "data:image/png;base64," in content


def test_base64_skips_oversize(tmp_path):
    src, _ = _make_html(tmp_path)
    conv = HTMConverter(image_mode="base64", image_max_mb=0.000001)
    content = conv.convert(str(src))
    assert "data:image/png;base64," not in content
    assert "doc.files/pic.png" in content


def test_none_mode_untouched(tmp_path):
    src, _ = _make_html(tmp_path)
    conv = HTMConverter(image_mode="none")
    content = conv.convert(str(src))
    assert "doc.files/pic.png" in content
    assert "ocr:begin" not in content


def test_chm_and_htm_share_pipeline():
    """两个转换器应使用同一套图片管线实现"""
    from converters.chm_converter import CHMConverter
    from converters.image_pipeline import ImagePipelineMixin
    assert issubclass(CHMConverter, ImagePipelineMixin)
    assert issubclass(HTMConverter, ImagePipelineMixin)


def test_inline_fallback_ocrs_on_cache_miss(tmp_path, monkeypatch):
    """容器常驻服务：缓存未命中时应现场识别并注入（不依赖独立预处理阶段）"""
    src, img = _make_html(tmp_path)
    cfg, cache = _ocr_cfg(tmp_path)
    cfg["inline_fallback"] = True
    out = tmp_path / "out"
    out.mkdir()

    calls = []

    def fake_ocr_image(path, cfg_, use_cache=True):
        calls.append(path)
        data = {"key": ocr.image_key(path), "sig": ocr._config_signature(cfg_),
                "width": 1, "height": 1, "tiled": False,
                "text": "现场识别出的文字", "blocks": []}
        ocr.cache_save(cfg_["cache_dir"], data["key"], data)
        return data

    monkeypatch.setattr(ocr, "ocr_image", fake_ocr_image)

    conv = HTMConverter(image_mode="link", assets_root=str(out), ocr_cfg=cfg)
    stats = {}
    content = conv._convert_with_images(str(src), "doc.md", stats)

    assert calls, "缓存未命中时应触发内联识别"
    assert "现场识别出的文字" in content
    assert stats.get("ocr_inline") == 1
    # 结果应写回缓存，供下次直接命中
    assert ocr.cache_load(str(cache), ocr.image_key(str(img))) is not None


def test_inline_fallback_disabled_does_not_ocr(tmp_path, monkeypatch):
    """默认关闭：容器外/导出脚本场景不应在渲染进程加载引擎"""
    src, _ = _make_html(tmp_path)
    cfg, _cache = _ocr_cfg(tmp_path)
    cfg["inline_fallback"] = False
    out = tmp_path / "out"
    out.mkdir()

    called = []
    monkeypatch.setattr(ocr, "ocr_image",
                        lambda *a, **k: called.append(1) or {"text": "x", "blocks": []})

    conv = HTMConverter(image_mode="link", assets_root=str(out), ocr_cfg=cfg)
    content = conv.convert(str(src))
    assert not called
    assert "ocr:begin" not in content
