"""泳道图结构抽取测试

全部用合成图 + 合成 OCR 块：几何逻辑可独立于 OCR 引擎与中文字体验证。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "server"))

cv2 = pytest.importorskip("cv2")

import flowchart  # noqa: E402
import ocr  # noqa: E402


# ----------------------------------------------------------------------
# 合成泳道图
# ----------------------------------------------------------------------

V_LINES = [10, 60, 260, 460, 660, 890]
H_SOLID = [20, 100, 680]
H_DASH = [300, 500]
# 只跨前两条泳道的长连接箭头：不得被当成行分隔线
CONNECTOR_Y = 200
CONNECTOR_X = (60, 460)


def _dashed_h(img, y, x0, x1, dash=14, gap=8, color=(0, 0, 0), thick=2):
    x = x0
    while x < x1:
        cv2.line(img, (x, y), (min(x + dash, x1), y), color, thick)
        x += dash + gap


def make_swimlane(path, rgba=False):
    """画一张 2 阶段 × 4 泳道的合成泳道图"""
    h, w = 700, 900
    if rgba:
        img = np.zeros((h, w, 4), dtype=np.uint8)      # 全透明，BGR 为 0
        img[:, :, 3] = 0
        color = (0, 0, 0, 255)   # 合成到白底后应为黑色笔画
    else:
        img = np.full((h, w, 3), 255, dtype=np.uint8)
        color = (0, 0, 0)
    for y in H_SOLID:
        cv2.line(img, (V_LINES[0], y), (V_LINES[-1], y), color, 2)
    for y in H_DASH:
        _dashed_h(img, y, V_LINES[0], V_LINES[-1], color=color)
    for x in V_LINES:
        cv2.line(img, (x, H_SOLID[0]), (x, H_SOLID[-1]), color, 2)
    cv2.line(img, (CONNECTOR_X[0], CONNECTOR_Y), (CONNECTOR_X[1], CONNECTOR_Y),
             color, 2)
    cv2.imwrite(path, img)
    return path


def blk(text, x, y, w=60, h=20):
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "score": 1.0}


def blocks_for_grid():
    """与合成图对应的 OCR 块（阶段名竖排：h > w）"""
    b = []
    # 表头（20..100）四条泳道
    for i, name in enumerate(["部门一", "部门二", "部门三", "部门四"]):
        b.append(blk(name, 110 + i * 200, 50))
    # 竖排阶段名在轴带（x 10..60）内
    b.append(blk("阶段甲", 20, 120, w=20, h=60))
    b.append(blk("阶段乙", 20, 320, w=20, h=60))
    b.append(blk("阶段丙", 20, 520, w=20, h=60))
    # 阶段甲的单元格
    b.append(blk("动作A", 100, 150))
    b.append(blk("动作B", 105, 190))          # 同框换行 -> 直接相连
    b.append(blk("动作C", 300, 150))          # 泳道二
    # 阶段乙：两个不同方框
    b.append(blk("动作D", 100, 350))
    b.append(blk("动作E", 100, 420))          # 间距大 -> 另起方框
    # 阶段丙：泳道三
    b.append(blk("动作F", 500, 550))
    return b


# ----------------------------------------------------------------------
# 网格检测
# ----------------------------------------------------------------------

def test_detect_swimlane_basic(tmp_path):
    p = make_swimlane(str(tmp_path / "flow.png"))
    sw = flowchart.detect_swimlane(p, blocks_for_grid())
    assert sw is not None
    assert sw["lanes"] == ["部门一", "部门二", "部门三", "部门四"]
    assert [r["label"] for r in sw["rows"]] == ["阶段甲", "阶段乙", "阶段丙"]


def test_cell_assignment_and_box_grouping(tmp_path):
    p = make_swimlane(str(tmp_path / "flow.png"))
    sw = flowchart.detect_swimlane(p, blocks_for_grid())
    rows = {r["label"]: r["cells"] for r in sw["rows"]}
    # 同框内的两行直连；不同方框之间用 <br>
    assert rows["阶段甲"][0] == "动作A 动作B"   # 中文前是 ASCII，补一个空格
    assert rows["阶段乙"][0] == "动作D<br>动作E"
    assert rows["阶段甲"][1] == "动作C"
    assert rows["阶段丙"][2] == "动作F"
    assert rows["阶段甲"][3] == ""


def test_long_connector_is_not_row_separator(tmp_path):
    """跨部分泳道的长箭头覆盖率高，但不是行分隔线"""
    p = make_swimlane(str(tmp_path / "flow.png"))
    sw = flowchart.detect_swimlane(p, blocks_for_grid())
    ys = sw["grid"]["hlines"]
    assert not any(abs(y - CONNECTOR_Y) <= 3 for y in ys), ys
    assert len(sw["rows"]) == 3


def test_dashed_separator_detected(tmp_path):
    p = make_swimlane(str(tmp_path / "flow.png"))
    sw = flowchart.detect_swimlane(p, blocks_for_grid())
    for y in H_DASH:
        assert any(abs(v - y) <= 3 for v in sw["grid"]["hlines"]), (y, sw["grid"]["hlines"])


def test_no_swimlane_without_blocks(tmp_path):
    p = make_swimlane(str(tmp_path / "flow.png"))
    assert flowchart.detect_swimlane(p, []) is None
    assert flowchart.to_markdown(p, []) == ""


def test_plain_image_not_swimlane(tmp_path):
    """普通图（没有竖排阶段名）不应被误判"""
    img = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (20, 20), (380, 380), (0, 0, 0), 2)
    p = str(tmp_path / "plain.png")
    cv2.imwrite(p, img)
    b = [blk("随便一些文字", 50, 50 + i * 30) for i in range(8)]
    assert flowchart.detect_swimlane(p, b) is None


# ----------------------------------------------------------------------
# alpha 白底合成（真实踩过的坑）
# ----------------------------------------------------------------------

def test_alpha_composited_before_analysis(tmp_path):
    """透明底 PNG 若直接读会整幅变黑，必须先按白底合成"""
    p = make_swimlane(str(tmp_path / "flow_rgba.png"), rgba=True)
    naive = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    assert (naive < 150).mean() > 0.5, "前提：直接读确实会变黑"

    img = flowchart.load_bgr(p)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    assert (gray < 150).mean() < 0.05, "合成白底后前景应只占少量像素"
    # 合成后仍能正常抽结构
    sw = flowchart.detect_swimlane(p, blocks_for_grid())
    assert sw is not None
    assert len(sw["rows"]) == 3


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def test_to_markdown_table(tmp_path):
    p = make_swimlane(str(tmp_path / "flow.png"))
    md = flowchart.to_markdown(p, blocks_for_grid())
    assert "| 阶段 \\ 部门 | 部门一 | 部门二 | 部门三 | 部门四 |" in md
    assert "| --- |" in md
    assert "| **阶段甲** | 动作A 动作B | 动作C |  |  |" in md


def test_smart_join_cjk_no_space():
    assert flowchart._smart_join(["申请人", "线下"]) == "申请人线下"
    assert flowchart._smart_join(["BoEing", "柜面"]) == "BoEing 柜面"
    assert flowchart._smart_join(["", "  ", "甲"]) == "甲"


# ----------------------------------------------------------------------
# 分块接缝碎片
# ----------------------------------------------------------------------

def test_drop_fragments_suffix_of_full_block():
    """「客户经理」右侧被接缝切出残片「理」，应剔除"""
    full = blk("客户经理", 790, 74, w=80, h=24)
    frag = blk("理", 864, 77, w=20, h=24)
    keep = blk("C3", 820, 102, w=30, h=24)
    out = ocr.drop_fragments([full, frag, keep])
    assert [b["text"] for b in out] == ["客户经理", "C3"]


def test_drop_fragments_prefix_case():
    full = blk("合同、凭证", 789, 703, w=75, h=26)
    frag = blk("证", 864, 704, w=24, h=26)
    assert [b["text"] for b in ocr.drop_fragments([full, frag])] == ["合同、凭证"]


def test_drop_fragments_keeps_unrelated_short_blocks():
    """纵向不重叠的短块不能被误删"""
    a = blk("客户经理", 790, 74, w=80, h=24)
    b = blk("理", 864, 500, w=20, h=24)     # y 相差很远
    assert len(ocr.drop_fragments([a, b])) == 2


def test_drop_fragments_keeps_equal_length():
    a = blk("甲乙", 100, 50, w=40, h=20)
    b = blk("丙丁", 140, 50, w=40, h=20)
    assert len(ocr.drop_fragments([a, b])) == 2


def test_drop_fragments_empty():
    assert ocr.drop_fragments([]) == []
    assert ocr.drop_fragments(None) == []
