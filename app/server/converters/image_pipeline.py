"""图片管线（CHM / HTML 等转换器共用）

职责:
1. 解析 Markdown 中的图片引用（相对路径 / 反斜杠 / Windows 绝对路径）
2. 按 image_mode 处理:
   - none   : 原样保留
   - link   : 复制到文档同级 `<stem>.files/` 并改写为相对链接
   - base64 : 内联为 data URI
3. 图片缺失时落占位文本（避免知识库留下死引用）
4. 注入 OCR 文字（实际识别由 OCR 预处理阶段完成，此处只读缓存）

设计要点:
- 渲染进程**不加载 OCR 引擎**，只读缓存 → 可安全多进程渲染
- link 模式下若未指定 assets_root（单文件 API 场景），只保留原引用不复制，
  避免产生指向不存在目录的死链接
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_IMG_EXT = re.compile(r'\.(png|jpe?g|gif|bmp|tiff?|emf|wmf)$', re.I)

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".emf": "image/emf", ".wmf": "image/wmf",
}


def _safe_component(name: str, max_bytes: int = 180) -> str:
    """TOC/文件名 → 安全文件或目录名组件

    - 替换文件系统非法字符
    - 按 UTF-8 字节上限截断（规避 ENAMETOOLONG），截断时追加短哈希防碰撞
    """
    import hashlib
    s = _INVALID_NAME.sub("_", (name or "").strip())
    s = s.strip(". ") or "未命名"
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    keep = max_bytes - len(digest) - 1
    out, used = [], 0
    for ch in s:
        b = len(ch.encode("utf-8"))
        if used + b > keep:
            break
        out.append(ch)
        used += b
    return "".join(out) + "~" + digest


def _norm_ref(ref: str) -> str:
    t = ref.split("#")[0].split("?")[0].replace("\\", "/")
    try:
        from urllib.parse import unquote
        return unquote(t)
    except Exception:
        return t


class ImagePipelineMixin:
    """为转换器提供统一的图片处理与 OCR 注入能力

    子类需在 __init__ 中调用 `self._init_image_pipeline(...)`。
    """

    def _init_image_pipeline(self, image_mode: str = "none",
                             image_max_mb: float = 5.0,
                             assets_root: str = "",
                             ocr_cfg: dict = None,
                             chm_root: str = ""):
        self.image_mode = (image_mode or "none").lower()
        self.image_max_mb = float(image_max_mb)
        self.assets_root = assets_root
        self.ocr_cfg = ocr_cfg or {}
        self.ocr_enabled = bool(self.ocr_cfg.get("enabled"))
        # 用于剥离 Windows 绝对路径前缀的根目录名（如 CHM 文件名去扩展名）
        self.chm_root = chm_root

    # ------------------------------------------------------------------
    # 引用解析
    # ------------------------------------------------------------------

    def _resolve_ref(self, ref: str, src_dir: str) -> str:
        """解析图片引用为真实文件路径；失败返回 ""。"""
        t = _norm_ref(ref)
        if not t or "://" in t or t.startswith("data:"):
            return ""
        if not _IMG_EXT.search(t):
            return ""
        if re.match(r"^[A-Za-z]:", t):
            # Windows 绝对路径：按根目录名剥离前缀
            parts = t.split("/")
            root = getattr(self, "chm_root", "") or ""
            for i, seg in enumerate(parts):
                if root and seg == root and i + 1 < len(parts):
                    cand = os.path.normpath(
                        os.path.join(src_dir, "..", "..", *parts[i + 1:]))
                    return cand if os.path.isfile(cand) else ""
            return ""
        if t.startswith("/"):
            return ""
        cand = os.path.normpath(os.path.join(src_dir, t))
        return cand if os.path.isfile(cand) else ""

    # ------------------------------------------------------------------
    # 处理
    # ------------------------------------------------------------------

    def _process_images(self, md: str, source_file: str, out_rel: str,
                        stats: dict = None) -> str:
        """按 self.image_mode 处理 Markdown 中的图片引用，并注入 OCR 文字"""
        if self.image_mode == "none" or not md:
            return md
        src_dir = os.path.dirname(os.path.abspath(source_file))
        stem = Path(source_file).stem
        out_dir = os.path.dirname(out_rel)

        def repl(m):
            alt, ref = m.group(1), m.group(2)
            real = self._resolve_ref(ref, src_dir)
            if not real:
                if stats is not None:
                    stats["missing"] = stats.get("missing", 0) + 1
                # 图片缺失（多为未打包进源文件的绝对路径引用）→ 落占位文本，
                # 避免知识库留下无法解析的死引用
                if self.ocr_cfg.get("placeholder_missing", True):
                    return "（原图缺失：{}）".format(os.path.basename(_norm_ref(ref)))
                return m.group(0)
            if stats is not None:
                stats["found"] = stats.get("found", 0) + 1
            size = os.path.getsize(real)
            suffix = self._ocr_suffix(real, stats)

            if self.image_mode == "base64":
                if size > self.image_max_mb * 1048576:
                    if stats is not None:
                        stats["oversize"] = stats.get("oversize", 0) + 1
                    return m.group(0)
                try:
                    import base64
                    with open(real, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                except OSError:
                    return m.group(0)
                mime = _MIME.get(Path(real).suffix.lower(), "application/octet-stream")
                if stats is not None:
                    stats["inlined"] = stats.get("inlined", 0) + 1
                    stats["bytes"] = stats.get("bytes", 0) + size
                return "![{}](data:{};base64,{}){}".format(alt, mime, b64, suffix)

            # link 模式：复制到文档同级 <stem>.files/ 并改写为相对链接
            name = _safe_component(os.path.basename(real), max_bytes=120)
            # 目录名同样限长：源文件名可超 255 字节，直接拼 .files 会
            # 触发 ENAMETOOLONG 导致整篇图片无法落盘
            target_rel = "{}.files/{}".format(
                _safe_component(stem, max_bytes=180), name)

            if not self.assets_root:
                # 未指定输出根目录（单文件 API 场景）：不复制、不改写，
                # 只保留原引用 + OCR 文字，避免产生死链接
                if stats is not None:
                    stats["no_assets_root"] = stats.get("no_assets_root", 0) + 1
                return "![{}]({}){}".format(alt, ref, suffix)

            dst = os.path.join(self.assets_root, out_dir, target_rel)
            try:
                if not os.path.isfile(dst) or os.path.getsize(dst) != size:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(real, dst)
                    if stats is not None:
                        stats["copied"] = stats.get("copied", 0) + 1
                        stats["bytes"] = stats.get("bytes", 0) + size
            except OSError:
                if stats is not None:
                    stats["copy_failed"] = stats.get("copy_failed", 0) + 1
                return "![{}]({}){}".format(alt, target_rel, suffix)
            if stats is not None:
                stats["linked"] = stats.get("linked", 0) + 1
            return "![{}]({}){}".format(alt, target_rel, suffix)

        return re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', repl, md)

    # ------------------------------------------------------------------
    # OCR 注入
    # ------------------------------------------------------------------

    def _ocr_suffix(self, real_path: str, stats: dict = None) -> str:
        """从 OCR 缓存取该图的文字并渲染为 Markdown 片段（无缓存则返回空）"""
        if not self.ocr_enabled:
            return ""
        try:
            import ocr as _ocr
            key = _ocr.image_key(real_path)
        except Exception:
            return ""
        data = None
        if self.ocr_cfg.get("_preloaded"):
            data = self.ocr_cfg["_preloaded"].get(key)
        if data is None:
            data = _ocr.cache_load(self.ocr_cfg.get("cache_dir", ""), key)
        if data is None and self.ocr_cfg.get("inline_fallback"):
            # 常驻服务场景（容器内批处理）：没有独立 OCR 预处理阶段，
            # 缓存未命中时现场识别并写回缓存，使后续运行可直接命中。
            # 独立导出脚本走多进程预处理，不应开启此项（避免渲染进程加载引擎）。
            try:
                data = _ocr.ocr_image(real_path, self.ocr_cfg, use_cache=True)
                if stats is not None:
                    stats["ocr_inline"] = stats.get("ocr_inline", 0) + 1
            except Exception:
                data = None
        if not data:
            if stats is not None:
                stats["ocr_miss"] = stats.get("ocr_miss", 0) + 1
            return ""
        # 分块接缝残片（「客户经理」被切出「理」）在渲染期剔除：
        # 纯后处理，不动缓存签名，已识别的图无需重跑
        raw_blocks = data.get("blocks") or []
        blocks = _ocr.drop_fragments(raw_blocks)
        if len(blocks) != len(raw_blocks):
            if stats is not None:
                stats["ocr_frag"] = stats.get("ocr_frag", 0) + (len(raw_blocks) - len(blocks))
            data = dict(data)
            data["blocks"] = blocks
            mins = int(self.ocr_cfg.get("min_text_chars", 1))
            data["text"] = "\n".join(b["text"] for b in blocks
                                     if len(b["text"]) >= mins)
        text = (data.get("text") or "").strip()
        if not text:
            if stats is not None:
                stats["ocr_empty"] = stats.get("ocr_empty", 0) + 1
            return ""
        if stats is not None:
            stats["ocr_hit"] = stats.get("ocr_hit", 0) + 1
            stats["ocr_chars"] = stats.get("ocr_chars", 0) + len(text)
        # 泳道图等规则图形：还原为「阶段 × 部门」表格，检索价值远高于平铺文字
        if len(blocks) >= 6:
            try:
                import flowchart as _flow
                table = _flow.to_markdown(
                    real_path, blocks, self.ocr_cfg.get("label", "图片文字"))
            except Exception:
                table = ""
            if table:
                if stats is not None:
                    stats["flow_table"] = stats.get("flow_table", 0) + 1
                return "\n\n" + table
        return "\n\n" + _ocr.to_markdown(data, self.ocr_cfg.get("label", "图片文字"))
