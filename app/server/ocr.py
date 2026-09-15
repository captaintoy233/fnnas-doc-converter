"""图片 OCR 支持（纯 CPU，基于 RapidOCR / ONNX Runtime）

设计要点（依据真实语料实测）:
1. **分块而非缩放**：RapidOCR 对超长边图片会整体缩放，文字过小时检测直接返回空
   （实测 11366x6734 总图默认 0 块；3x3 分块后 870 块 / 6597 字）。
   故超过 tile_threshold 的图片按 tile_size 切块（带重叠），坐标回映射后去重合并。
2. **内容哈希缓存**：以图片内容 SHA-1 为键缓存结果。手册持续更新时，
   未变化的图片无需重复 OCR。
3. **阅读顺序还原**：按垂直重叠聚类成行，行内按 x 排序，得到接近人眼的顺序。
4. 引擎为**进程内单例**，避免重复初始化；线程数按进程数分摊（进程数×线程数≈核数）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional

_ENGINE = None
_ENGINE_KEY = None

_MARK_BEGIN = "<!-- ocr:begin -->"
_MARK_END = "<!-- ocr:end -->"


def default_config() -> dict:
    return {
        "enabled": False,
        "backend": "rapidocr",
        "cache_dir": os.path.expanduser("~/.cache/docconverter/ocr"),
        # 最长边超过该值则分块（RapidOCR 内部对超限图会缩放，导致小字漏检）
        "tile_threshold": 960,
        # 分块目标边长；重叠像素
        "tile_size": 960,
        "tile_overlap": 96,
        # 低于该置信度的文本块丢弃
        "min_score": 0.5,
        # 引擎参数
        "intra_op_num_threads": 2,
        "inter_op_num_threads": 1,
        "max_side_len": 2000,
        "det_limit_side_len": 736,
        "min_text_chars": 1,
        # 任一边小于该值的图片视为装饰/切片碎片（实测大量 1x1、8x35 碎片），跳过 OCR
        "min_image_side": 32,
        # 常驻服务场景：缓存未命中时现场识别（独立导出脚本用多进程预处理，保持 False）
        "inline_fallback": False,
        "label": "图片文字",
        "placeholder_missing": True,
    }


# ----------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------

# 影响识别结果的配置项：任一变化都应使缓存失效
_SIG_KEYS = ("tile_threshold", "tile_size", "tile_overlap", "min_score",
             "min_image_side", "max_side_len", "det_limit_side_len",
             "min_text_chars", "backend")


def _config_signature(cfg: dict) -> str:
    """识别配置指纹（不含 cache_dir / 线程数等不影响结果的项）"""
    payload = json.dumps({k: cfg.get(k) for k in _SIG_KEYS}, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def image_key(path: str) -> str:
    """图片内容 SHA-1（增量重跑的关键：内容不变则复用 OCR 结果）"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_file(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, key[:2], key + ".json")


def cache_load(cache_dir: str, key: str) -> Optional[dict]:
    p = _cache_file(cache_dir, key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cache_save(cache_dir: str, key: str, data: dict) -> None:
    p = _cache_file(cache_dir, key)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------

def get_engine(cfg: dict):
    """进程内单例引擎（配置变化时重建）"""
    global _ENGINE, _ENGINE_KEY
    key = json.dumps({k: cfg.get(k) for k in (
        "intra_op_num_threads", "inter_op_num_threads",
        "max_side_len", "det_limit_side_len")}, sort_keys=True)
    if _ENGINE is not None and _ENGINE_KEY == key:
        return _ENGINE
    from rapidocr import RapidOCR
    params = {
        "Global.max_side_len": int(cfg.get("max_side_len", 2000)),
        "Global.log_level": "error",
        "EngineConfig.onnxruntime.intra_op_num_threads":
            int(cfg.get("intra_op_num_threads", 2)),
        "EngineConfig.onnxruntime.inter_op_num_threads":
            int(cfg.get("inter_op_num_threads", 1)),
        "Det.limit_side_len": int(cfg.get("det_limit_side_len", 736)),
    }
    try:
        _ENGINE = RapidOCR(params=params)
    except Exception:
        # 参数键不被该版本接受时回退默认
        _ENGINE = RapidOCR()
    _ENGINE_KEY = key
    return _ENGINE


def _run(img, cfg: dict) -> list:
    """对单张 ndarray 执行 OCR，返回 [{text,score,x,y,w,h}]"""
    eng = get_engine(cfg)
    res = eng(img)
    boxes = getattr(res, "boxes", None)
    txts = getattr(res, "txts", None)
    scores = getattr(res, "scores", None)
    if boxes is None or txts is None:
        return []
    out = []
    for i, box in enumerate(boxes):
        t = txts[i] if i < len(txts) else ""
        if not t or not t.strip():
            continue
        sc = float(scores[i]) if scores is not None and i < len(scores) else 1.0
        if sc < cfg.get("min_score", 0.5):
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append({"text": t.strip(), "score": round(sc, 4),
                    "x": min(xs), "y": min(ys),
                    "w": max(xs) - min(xs), "h": max(ys) - min(ys)})
    return out


def _tiles(w: int, h: int, size: int, overlap: int):
    """生成 (x0, y0, x1, y1) 分块；长边不超过 size，块间重叠 overlap"""
    if w <= size and h <= size:
        return [(0, 0, w, h)]
    step = max(1, size - overlap)
    xs = list(range(0, max(1, w - overlap), step)) or [0]
    ys = list(range(0, max(1, h - overlap), step)) or [0]
    if xs[-1] + size < w:
        xs.append(max(0, w - size))
    if ys[-1] + size < h:
        ys.append(max(0, h - size))
    out = []
    for y0 in ys:
        for x0 in xs:
            out.append((x0, y0, min(w, x0 + size), min(h, y0 + size)))
    return out


def _dedup(blocks: list) -> list:
    """跨重叠块去重：文本相同且中心点接近的视为同一块"""
    seen = []
    for b in blocks:
        cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        dup = False
        for s in seen:
            sx, sy = s["x"] + s["w"] / 2, s["y"] + s["h"] / 2
            if s["text"] == b["text"] and abs(cx - sx) <= 24 and abs(cy - sy) <= 12:
                dup = True
                break
        if not dup:
            seen.append(b)
    return seen


def drop_fragments(blocks: list, max_gap: float = 10.0) -> list:
    """剔除分块识别在接缝处切出来的残片

    大图分块识别时，跨接缝的文字会被切两半：完整块由前一块识别（如
    「客户经理」），后一块只看到右边几个像素，识别成残片（「理」）。
    残片紧贴完整块边缘、且其文本是完整块的后缀/前缀，据此剔除。

    纯后处理，**不改缓存签名**：已缓存的识别结果无需重跑，渲染时过滤即可。
    """
    if not blocks:
        return []
    kept = []
    for b in blocks:
        bt = (b.get("text") or "").strip()
        frag = False
        if bt:
            for s in blocks:
                if s is b:
                    continue
                st = (s.get("text") or "").strip()
                if len(st) <= len(bt):
                    continue
                # 纵向需有实质重叠
                ov = min(b["y"] + b["h"], s["y"] + s["h"]) - max(b["y"], s["y"])
                if ov < 0.5 * min(b["h"], s["h"]):
                    continue
                s_r, s_l = s["x"] + s["w"], s["x"]
                right_touch = s_r - max_gap <= b["x"] <= s_r + max_gap
                left_touch = s_l - max_gap <= b["x"] + b["w"] <= s_l + max_gap
                if right_touch and st.endswith(bt):
                    frag = True
                    break
                if left_touch and st.startswith(bt):
                    frag = True
                    break
                # 残片整块落在完整块内（含完全被包住的情形）
                if (b["x"] >= s_l - 2 and b["x"] + b["w"] <= s_r + 2
                        and bt in st):
                    frag = True
                    break
        if not frag:
            kept.append(b)
    return kept


def sort_reading_order(blocks: list) -> list:
    """按垂直重叠聚行，行内按 x 排序"""
    if not blocks:
        return []
    items = sorted(blocks, key=lambda b: (b["y"], b["x"]))
    lines: list[list] = []
    for b in items:
        placed = False
        for ln in lines:
            ly0 = min(x["y"] for x in ln)
            ly1 = max(x["y"] + x["h"] for x in ln)
            ov = min(b["y"] + b["h"], ly1) - max(b["y"], ly0)
            if ov > 0.5 * min(b["h"], max(1.0, ly1 - ly0)):
                ln.append(b)
                placed = True
                break
        if not placed:
            lines.append([b])
    lines.sort(key=lambda ln: min(x["y"] for x in ln))
    out = []
    for ln in lines:
        ln.sort(key=lambda x: x["x"])
        out.extend(ln)
    return out


def ocr_image(path: str, cfg: dict = None, use_cache: bool = True) -> dict:
    """OCR 单张图片（含分块与去重），返回结果 dict

    缓存以「图片内容 SHA-1 + 识别配置签名」为键：任一变化都会重识别，
    避免调参后仍命中旧结果；无文字的图片同样入缓存，
    使其在增量重跑时不再被反复推理。
    """
    cfg = cfg or default_config()
    key = image_key(path)
    sig = _config_signature(cfg)

    if use_cache:
        hit = cache_load(cfg["cache_dir"], key)
        # 旧缓存无 sig 字段：视为与当前配置兼容，避免升级后全量重识别
        if hit is not None and hit.get("sig", sig) == sig:
            hit["cached"] = True
            return hit

    import cv2
    img = cv2.imread(path)
    if img is None:
        data = {"key": key, "sig": sig, "error": "unreadable", "blocks": [],
                "text": "", "width": 0, "height": 0, "tiled": False,
                "cached": False}
        if use_cache:
            cache_save(cfg["cache_dir"], key, data)
        return data

    h, w = img.shape[:2]

    # 过小的图（装饰/切片碎片）直接跳过，避免无谓的推理开销
    min_side = int(cfg.get("min_image_side", 32))
    if min_side > 0 and min(w, h) < min_side:
        data = {"key": key, "sig": sig, "width": w, "height": h, "tiled": False,
                "blocks": [], "text": "", "skipped": "too_small", "cached": False}
        if use_cache:
            cache_save(cfg["cache_dir"], key, data)
        return data

    thr = int(cfg.get("tile_threshold", 960))
    tiled = max(w, h) > thr
    if tiled:
        blocks = []
        for (x0, y0, x1, y1) in _tiles(w, h, int(cfg.get("tile_size", thr)),
                                       int(cfg.get("tile_overlap", 96))):
            crop = img[y0:y1, x0:x1]
            for b in _run(crop, cfg):
                b["x"] += x0
                b["y"] += y0
                blocks.append(b)
        blocks = _dedup(blocks)
    else:
        blocks = _run(img, cfg)

    blocks = sort_reading_order(blocks)
    mins = int(cfg.get("min_text_chars", 1))
    text = "\n".join(b["text"] for b in blocks if len(b["text"]) >= mins)

    data = {"key": key, "sig": sig, "width": w, "height": h, "tiled": tiled,
            "blocks": blocks, "text": text, "cached": False}
    if use_cache:
        cache_save(cfg["cache_dir"], key, data)
    return data


def to_markdown(data: dict, label: str = "图片文字") -> str:
    """把 OCR 结果渲染为 Markdown 引用块（可被 RAG 切分/检索）"""
    text = (data or {}).get("text", "") or ""
    if not text.strip():
        return ""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    body = "\n".join("> " + ln for ln in lines)
    return "{}\n> **［{}］**\n{}\n{}".format(_MARK_BEGIN, label, body, _MARK_END)
