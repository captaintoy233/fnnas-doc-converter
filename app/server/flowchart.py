"""图形结构抽取（确定性几何方法，不用模型）

中文制度文档里的插图大致两类，平铺成文字清单都会丢结构：

1. **泳道图 / 跨职能流程图**——本质是一张「阶段 × 部门」的矩阵。还原成
   Markdown 表格后，检索「某阶段某部门做什么」可以被整体命中，比一堆散句
   有用得多，人读起来也顺。
2. 其它图形（普通流程图、示意图）——退回按阅读顺序的要素清单，至少不丢字。

**为什么不用 VLM 抽拓扑**：实测 3B 视觉模型会编造连线、或陷入重复循环，
而错误的关系会被当成真实制度流程，比"没有关系"更危险。几何方法是确定性的：
同一张图永远得到同一结果，单图几十毫秒，纯 CPU，可离线可复现。

实现中两个关键坑（都实际踩过）：

- **PNG 带 alpha 时不能直接 cv2.imread**：透明区会读成黑色，二值化后整幅图
  都算"前景"，线检测彻底失效（曾误判为"图里只有 1 条连线"）。必须先按白底
  合成再分析。
- **虚线分隔线与长连接箭头无法用"整行暗像素占比"区分**：二者都能占到
  40%~70% 宽度。必须进一步要求分隔线在**每一条泳道内**都有覆盖——分隔线横跨
  全部泳道，箭头只跨其中一两条，这样才分得开。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

Block = Dict
Lane = Tuple[int, int]          # (x0, x1) 像素区间

# 判定为线的最小覆盖比例
_V_LINE_COV = 0.60              # 竖线：占图高比例
_H_CAND_COV = 0.28              # 横线候选：占图宽比例
_H_LANE_COV = 0.30              # 每条泳道内的最小覆盖
_DARK = 150                     # 灰度阈值（深色笔画为前景）


# ----------------------------------------------------------------------
# 图像加载
# ----------------------------------------------------------------------

def load_bgr(image_path: str, max_side: int = 4000):
    """读图并**按白底合成 alpha**；超大图等比缩小以控制耗时。

    返回 BGR ndarray，失败返回 None。
    """
    if cv2 is None:  # pragma: no cover
        return None
    try:
        raw = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
    except Exception:
        return None
    if raw is None:
        return None
    if raw.ndim == 2:
        img = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    elif raw.shape[2] == 4:
        bgr = raw[:, :, :3].astype(np.float32)
        alpha = (raw[:, :, 3].astype(np.float32) / 255.0)[:, :, None]
        img = (bgr * alpha + 255.0 * (1.0 - alpha)).astype(np.uint8)
    else:
        img = raw[:, :, :3]
    h, w = img.shape[:2]
    if max_side > 0 and max(h, w) > max_side:
        s = max_side / float(max(h, w))
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                         interpolation=cv2.INTER_AREA)
    return img


# ----------------------------------------------------------------------
# 线与网格检测
# ----------------------------------------------------------------------

def _runs(mask, gap: int = 4) -> List[Tuple[int, int]]:
    """布尔序列里连续的 True 聚成区间（相隔 <= gap 视为同一段）"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out: List[Tuple[int, int]] = []
    start = prev = int(idx[0])
    for v in idx[1:]:
        v = int(v)
        if v - prev <= gap:
            prev = v
        else:
            out.append((start, prev))
            start = prev = v
    out.append((start, prev))
    return out


def _center(b: Block) -> Tuple[float, float]:
    return (b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0)


def _in_band(b: Block, y0: int, y1: int) -> bool:
    _, cy = _center(b)
    return y0 <= cy < y1


def detect_swimlane(image_path: str, blocks: Optional[List[Block]] = None
                    ) -> Optional[dict]:
    """检测泳道图网格；命中返回结构，否则 None

    返回::

        {
          "title": "……示意图",
          "lanes": ["申请人线下", ...],
          "rows":  [{"label": "产品签约", "cells": ["…", "…", ...]}, ...],
          "grid":  {"vlines": [...], "hlines": [...], "axis": (x0, x1)},
        }
    """
    if cv2 is None or not blocks:
        return None
    img = load_bgr(image_path)
    if img is None:
        return None
    H, W = img.shape[:2]
    if min(H, W) < 120:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dark = gray < _DARK

    # --- 竖线：接近整幅图高的暗线 ---
    col_cov = dark.sum(axis=0) / float(H)
    vlines = [(a + b) // 2 for a, b in _runs(col_cov > _V_LINE_COV, gap=4)]
    if len(vlines) < 4:
        return None

    widths = np.diff(np.array(vlines, dtype=float))
    if widths.size == 0 or float(np.median(widths)) <= 0:
        return None
    med_w = float(np.median(widths))

    # 最左侧明显偏窄的一带 = 竖排阶段名轴
    axis_i = 0 if (widths[0] < 0.55 * med_w and widths[0] < 0.35 * W) else None
    if axis_i is None:
        return None
    axis = (vlines[0], vlines[1])
    lanes: List[Lane] = [(vlines[i], vlines[i + 1])
                         for i in range(1, len(vlines) - 1)]
    if len(lanes) < 2:
        return None

    # --- 横线：必须横跨每一条泳道，才能与长箭头区分 ---
    row_cov = dark.sum(axis=1) / float(W)
    hlines: List[int] = []
    for a, b in _runs(row_cov > _H_CAND_COV, gap=2):
        seg = dark[a:b + 1, :].any(axis=0)
        if all(seg[x0:x1].mean() >= _H_LANE_COV for x0, x1 in lanes):
            hlines.append((a + b) // 2)
    if len(hlines) < 3:
        return None

    bands: List[Tuple[int, int]] = list(zip(hlines, hlines[1:]))

    # --- 竖排阶段名落在哪一带，哪一带就是阶段行 ---
    axis_blocks = [b for b in blocks
                   if axis[0] <= _center(b)[0] < axis[1]
                   and b.get("h", 0) > 1.15 * max(1.0, b.get("w", 1))]
    phase_of_band: Dict[int, List[Block]] = {}
    for i, (y0, y1) in enumerate(bands):
        got = [b for b in axis_blocks if _in_band(b, y0, y1)]
        if got:
            phase_of_band[i] = sorted(got, key=lambda b: b["y"])
    if len(phase_of_band) < 2:
        return None

    first_phase = min(phase_of_band)
    if first_phase == 0:
        return None                      # 缺少表头带
    header_i = first_phase - 1

    # --- 表头泳道名 ---
    lane_names = []
    for x0, x1 in lanes:
        got = [b for b in blocks if _in_band(b, bands[header_i][0], bands[header_i][1])
               and x0 <= _center(b)[0] < x1]
        lane_names.append(_smart_join([b["text"] for b in
                                       sorted(got, key=lambda b: (b["y"], b["x"]))]))

    # --- 表格主体 ---
    rows = []
    for i in sorted(phase_of_band):
        y0, y1 = bands[i]
        label = _smart_join([b["text"] for b in phase_of_band[i]])
        cells = []
        for x0, x1 in lanes:
            got = [b for b in blocks if _in_band(b, y0, y1)
                   and x0 <= _center(b)[0] < x1]
            cells.append(_cell_text(got))
        # 整行都空：多为装饰性分隔，不占表格
        if label and any(c for c in cells):
            rows.append({"label": label, "cells": cells})
    if len(rows) < 2:
        return None

    # --- 标题：表头带之前的文字 ---
    title = ""
    if header_i > 0:
        ty0, ty1 = bands[0][0], bands[header_i][0]
        got = [b for b in blocks if _in_band(b, ty0, ty1)]
        title = _smart_join([b["text"] for b in sorted(got, key=lambda b: (b["y"], b["x"]))])

    return {"title": title, "lanes": lane_names, "rows": rows,
            "grid": {"vlines": vlines, "hlines": hlines, "axis": axis}}


# ----------------------------------------------------------------------
# 文本拼接
# ----------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF)


def _smart_join(parts: List[str]) -> str:
    """中文之间直接相连，其余用空格——避免"申请人 线下"这类多余空格"""
    out = ""
    for p in parts or []:
        p = (p or "").strip()
        if not p:
            continue
        if not out:
            out = p
        elif _is_cjk(out[-1]) and _is_cjk(p[0]):
            out += p
        else:
            out += " " + p
    return out


def _group_boxes(blocks: List[Block]) -> List[List[Block]]:
    """把 OCR 块按纵向间距聚成"同一个方框内的多行" vs "不同方框" """
    bs = sorted(blocks, key=lambda b: (b["y"], b["x"]))
    if not bs:
        return []
    med_h = float(np.median([max(1.0, b["h"]) for b in bs]))
    groups: List[List[Block]] = []
    for b in bs:
        if groups:
            last = groups[-1]
            ly1 = max(x["y"] + x["h"] for x in last)
            if b["y"] - ly1 <= 1.6 * med_h:
                last.append(b)
                continue
        groups.append([b])
    return groups


def _cell_text(blocks: List[Block]) -> str:
    groups = _group_boxes(blocks)
    return "<br>".join(_smart_join([b["text"] for b in g]) for g in groups)


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def to_markdown(image_path: str, blocks: Optional[List[Block]] = None,
                label: str = "图形结构") -> str:
    """泳道图 -> Markdown 表格；不适用时返回空串（调用方回退到文字清单）"""
    sw = detect_swimlane(image_path, blocks)
    if not sw:
        return ""
    lanes = sw["lanes"]
    if not any(lanes):
        return ""
    head = "| 阶段 \\ 部门 | " + " | ".join(ln or "—" for ln in lanes) + " |"
    sep = "| --- |" + " --- |" * len(lanes)
    body = []
    for r in sw["rows"]:
        body.append("| **{}** | {} |".format(
            r["label"], " | ".join(c or "" for c in r["cells"])))
    parts = []
    if sw.get("title"):
        parts.append("**{}**".format(sw["title"]))
    parts.append("[图形结构：泳道图还原为下表，行=阶段，列=部门]")
    parts.append("\n".join([head, sep] + body))
    return "\n\n".join(parts)


def describe(image_path: str, blocks: Optional[List[Block]] = None) -> dict:
    """诊断入口：返回检测结果概要（不渲染），供测试与批量统计使用"""
    sw = detect_swimlane(image_path, blocks)
    if not sw:
        return {"swimlane": False}
    return {"swimlane": True, "lanes": sw["lanes"],
            "phases": [r["label"] for r in sw["rows"]],
            "grid": {"n_v": len(sw["grid"]["vlines"]),
                     "n_h": len(sw["grid"]["hlines"])}}
