# -*- coding: utf-8 -*-
"""低质量标注 + 按需预处理（ocr_batch）测试。

对应两条产品决策：
1. GPU 不常驻 → 预处理命令行支持前后置命令，转换期只读缓存；
2. 低清图只标注、不重试 → quality_of / to_markdown 产出警示行。
"""
import json
import os

import pytest

import ocr


# ---------------------------------------------------------------- 质量判定

def test_quality_ok_when_scores_high():
    data = {"blocks": [{"text": "清晰的文字", "score": 0.95},
                       {"text": "第二行", "score": 0.90}],
            "text": "清晰的文字\n第二行", "width": 800, "height": 600}
    q = ocr.quality_of(data, ocr.default_config())
    assert q["level"] == "ok"
    assert q["score"] == pytest.approx(0.925, abs=1e-3)


def test_quality_low_when_scores_low():
    data = {"blocks": [{"text": "乱码一", "score": 0.31},
                       {"text": "乱码二", "score": 0.40}],
            "text": "乱码一\n乱码二", "width": 400, "height": 300}
    q = ocr.quality_of(data, ocr.default_config())
    assert q["level"] == "low"
    assert "0.3" in q["detail"]


def test_quality_low_on_tile_duplicate_fragments():
    """分块切句造成的半句重复：置信度高也要标注，否则 MD 里会留噪声。"""
    text = "1. 审查\n1. 审查审批\n5. 档案\n5. 档案管理\n3. 抵押登记"
    data = {"blocks": [{"text": ln, "score": 0.97} for ln in text.splitlines()],
            "text": text, "width": 1100, "height": 1500, "tiled": True}
    q = ocr.quality_of(data, ocr.default_config())
    assert q["level"] == "low"
    assert "分块重复" in q["detail"]


def test_quality_ok_when_not_tiled():
    """未分块时不做重复判定（整图识别不会切句）。"""
    text = "1. 审查\n1. 审查审批\n5. 档案\n5. 档案管理"
    data = {"blocks": [{"text": ln, "score": 0.97} for ln in text.splitlines()],
            "text": text, "width": 800, "height": 600, "tiled": False}
    assert ocr.quality_of(data, ocr.default_config())["level"] == "ok"


def test_quality_no_text_for_big_image():
    data = {"blocks": [], "text": "", "width": 900, "height": 700}
    q = ocr.quality_of(data, ocr.default_config())
    assert q["level"] == "no_text"


def test_quality_unknown_without_scores():
    """远程引擎只回文本（无置信度）：不得伪造高分，判 unknown。"""
    data = {"blocks": [{"text": "文字", "score": None}], "text": "文字",
            "width": 800, "height": 600}
    q = ocr.quality_of(data, ocr.default_config())
    assert q["level"] == "unknown"
    assert q["score"] is None


def test_quality_skips_tiny_image():
    data = {"skipped": "too_small", "width": 8, "height": 8}
    assert ocr.quality_of(data, ocr.default_config())["level"] == "unknown"


# ---------------------------------------------------------------- MD 标注

def test_markdown_annotates_low_quality():
    cfg = ocr.default_config()
    cfg["annotate_low_quality"] = True
    data = {"blocks": [{"text": "模糊字", "score": 0.3}],
            "text": "模糊字", "width": 500, "height": 400}
    md = ocr.to_markdown(data, "图片文字", cfg)
    assert "识别质量偏低" in md
    assert "模糊字" in md
    assert md.startswith("<!-- ocr:begin -->") and md.endswith("<!-- ocr:end -->")


def test_markdown_no_annotation_when_ok():
    cfg = ocr.default_config()
    cfg["annotate_low_quality"] = True
    data = {"blocks": [{"text": "清楚", "score": 0.98}], "text": "清楚",
            "width": 400, "height": 300}
    md = ocr.to_markdown(data, "图片文字", cfg)
    assert "识别质量偏低" not in md
    assert "清楚" in md


def test_markdown_emits_note_when_big_image_has_no_text():
    cfg = ocr.default_config()
    cfg["annotate_low_quality"] = True
    data = {"blocks": [], "text": "", "width": 1000, "height": 800}
    md = ocr.to_markdown(data, "图片文字", cfg)
    assert "未识别到文字" in md


def test_markdown_legacy_call_without_cfg():
    """不传 cfg 时保持旧行为：只有文字块、无标注。"""
    md = ocr.to_markdown({"text": "只有文字"}, "图片文字")
    quoted = [ln for ln in md.splitlines() if ln.startswith("> ")]
    assert len(quoted) == 2          # 标题行 + 1 行正文
    assert "识别质量" not in md


# ---------------------------------------------------------------- 预处理 CLI

def _tiny_png(path, color=(200, 200, 200)):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    cv2.imwrite(str(path), np.full((160, 240, 3), color, dtype=np.uint8))


def test_batch_dry_run_lists_work(tmp_path, capsys):
    import ocr_batch
    src = tmp_path / "src"
    src.mkdir()
    _tiny_png(src / "a.png")
    _tiny_png(src / "b.png")
    cache = str(tmp_path / "cache")
    rc = ocr_batch.main(["--source", str(src), "--cache-dir", cache, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "需识别 2 张" in out


def test_batch_skips_cached_and_writes_summary(tmp_path):
    import ocr_batch
    src = tmp_path / "src"
    src.mkdir()
    img = src / "a.png"
    _tiny_png(img)
    cache = str(tmp_path / "cache")
    cfg = ocr.default_config()
    cfg.update({"enabled": True, "cache_dir": cache, "backend": "rapidocr"})
    # 预置一条缓存（模拟"已识别过"）
    key = ocr.image_key(str(img))
    ocr.cache_save(cache, key, {"key": key, "sig": ocr._config_signature(cfg),
                                "text": "预置", "blocks": [], "width": 240,
                                "height": 160})
    rc = ocr_batch.main(["--source", str(src), "--cache-dir", cache])
    assert rc == 0


def test_batch_runs_start_and_stop_hooks(tmp_path):
    import ocr_batch
    src = tmp_path / "src"
    src.mkdir()
    _tiny_png(src / "a.png")
    marker = tmp_path / "hooks.txt"

    if os.name == "nt":
        start = 'echo start>>"%s"' % marker
        stop = 'echo stop>>"%s"' % marker
    else:
        start = 'echo start>>"%s"' % marker
        stop = 'echo stop>>"%s"' % marker

    rc = ocr_batch.main(["--source", str(src),
                         "--cache-dir", str(tmp_path / "cache"),
                         "--start-cmd", start, "--stop-cmd", stop])
    assert rc == 0
    content = marker.read_text(encoding="utf-8", errors="replace")
    assert "start" in content and "stop" in content
