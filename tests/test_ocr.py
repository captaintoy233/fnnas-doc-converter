"""OCR 模块测试（不依赖 RapidOCR 引擎的部分）

覆盖: 内容哈希、缓存读写、分块几何、跨块去重、阅读顺序、Markdown 渲染
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

import ocr  # noqa: E402


def test_image_key_deterministic_and_content_based(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello")
    b.write_bytes(b"hello")
    c = tmp_path / "c.bin"
    c.write_bytes(b"hello!")
    assert ocr.image_key(str(a)) == ocr.image_key(str(b))   # 同内容同键
    assert ocr.image_key(str(a)) != ocr.image_key(str(c))   # 内容变则键变


def test_cache_roundtrip(tmp_path):
    cache = str(tmp_path / "cache")
    key = "a" * 40
    assert ocr.cache_load(cache, key) is None
    data = {"key": key, "text": "测试文字", "blocks": [], "width": 10,
            "height": 10, "tiled": False}
    ocr.cache_save(cache, key, data)
    got = ocr.cache_load(cache, key)
    assert got is not None and got["text"] == "测试文字"
    # 分片目录按前两位散列
    assert os.path.isdir(os.path.join(cache, "aa"))


def test_cache_load_corrupt_returns_none(tmp_path):
    cache = str(tmp_path / "c")
    key = "b" * 40
    p = Path(ocr._cache_file(cache, key))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert ocr.cache_load(cache, key) is None


def test_tiles_small_image_single_tile():
    assert ocr._tiles(100, 50, 960, 96) == [(0, 0, 100, 50)]


def test_tiles_cover_whole_image_and_limit_side():
    w, h = 11366, 6734
    ts = 960
    tiles = ocr._tiles(w, h, ts, 96)
    assert len(tiles) > 50
    # 每块不超过目标边长
    assert all(x1 - x0 <= ts and y1 - y0 <= ts for x0, y0, x1, y1 in tiles)
    # 完全覆盖：四角与中心点都落在某块内
    for px, py in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, h // 2)]:
        assert any(x0 <= px < x1 and y0 <= py < y1 for x0, y0, x1, y1 in tiles)


def test_dedup_removes_overlap_duplicates():
    a = {"text": "审批", "x": 100, "y": 200, "w": 40, "h": 20}
    dup = {"text": "审批", "x": 104, "y": 203, "w": 40, "h": 20}   # 重叠区重复
    other = {"text": "复核", "x": 300, "y": 200, "w": 40, "h": 20}
    far = {"text": "审批", "x": 900, "y": 200, "w": 40, "h": 20}   # 同字但位置远
    out = ocr._dedup([a, dup, other, far])
    assert len(out) == 3
    assert other in out and far in out


def test_sort_reading_order_line_then_x():
    blocks = [
        {"text": "C", "x": 300, "y": 10, "w": 20, "h": 20},
        {"text": "A", "x": 100, "y": 12, "w": 20, "h": 20},
        {"text": "B", "x": 200, "y": 11, "w": 20, "h": 20},
        {"text": "D", "x": 100, "y": 100, "w": 20, "h": 20},
    ]
    got = [b["text"] for b in ocr.sort_reading_order(blocks)]
    assert got == ["A", "B", "C", "D"]


def test_to_markdown_wraps_and_marks():
    data = {"text": "第一行\n第二行"}
    md = ocr.to_markdown(data, "图片文字")
    assert "ocr:begin" in md and "ocr:end" in md
    assert "> 第一行" in md and "> 第二行" in md
    assert "图片文字" in md


def test_to_markdown_empty_returns_blank():
    assert ocr.to_markdown({"text": ""}) == ""
    assert ocr.to_markdown({}) == ""
    assert ocr.to_markdown(None) == ""


def test_min_image_side_skips_tiny(tmp_path):
    """过小的图（碎片/装饰）应跳过，不加载引擎"""
    pytest.importorskip("cv2")
    import numpy as np
    import cv2
    p = tmp_path / "tiny.png"
    cv2.imwrite(str(p), np.zeros((8, 35, 3), dtype=np.uint8))
    cfg = ocr.default_config()
    cfg["cache_dir"] = str(tmp_path / "cache")
    cfg["min_image_side"] = 32
    d = ocr.ocr_image(str(p), cfg)
    assert d["skipped"] == "too_small"
    assert d["text"] == ""
    # 跳过结果同样入缓存，避免下次重复判断
    assert ocr.cache_load(cfg["cache_dir"], d["key"]) is not None


def test_config_signature_changes_with_result_affecting_keys():
    a = ocr.default_config()
    b = dict(a)
    b["tile_threshold"] = 1200
    assert ocr._config_signature(a) != ocr._config_signature(b)
    # 不影响结果的项（线程数、缓存目录）不参与签名
    c = dict(a)
    c["intra_op_num_threads"] = 8
    c["cache_dir"] = "/tmp/other"
    assert ocr._config_signature(a) == ocr._config_signature(c)


def test_legacy_cache_without_sig_still_hits(tmp_path):
    """升级兼容：旧缓存无 sig 字段时不应被判为失效（否则要全量重识别）"""
    cache = str(tmp_path / "c")
    key = "c" * 40
    ocr.cache_save(cache, key, {"key": key, "text": "旧结果", "blocks": []})
    got = ocr.cache_load(cache, key)
    assert got is not None and "sig" not in got


def test_empty_result_is_cached(tmp_path):
    """无文字的图片也要入缓存，避免增量重跑时被反复推理"""
    pytest.importorskip("cv2")
    import numpy as np
    import cv2 as _cv2
    p = tmp_path / "blank.png"
    _cv2.imwrite(str(p), np.full((80, 300, 3), 255, dtype=np.uint8))
    cfg = ocr.default_config()
    cfg["cache_dir"] = str(tmp_path / "cache")
    d = ocr.ocr_image(str(p), cfg)
    assert d["text"] == ""
    saved = ocr.cache_load(cfg["cache_dir"], d["key"])
    assert saved is not None and saved.get("sig")
