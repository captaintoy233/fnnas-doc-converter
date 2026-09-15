#!/usr/bin/env python3
"""CHM → Markdown 目录树导出（面向 WeKnora 知识库）

按 CHM 的 .hhc 索引层级建立同名文件夹，每个索引条目产出一个 .md，
并生成清单/索引/同步报告，支持手册持续更新后的增量重跑。

输出内容:
  <out_dir>/
    ├── _manifest.json      全量清单（文档→元数据，增量比对基准）
    ├── _sync_report.json   本次新增/变更/删除明细（供推送管线 push/delete）
    ├── _index.md           全量目录索引（带相对链接）
    ├── _废止清单.md         已废止文档清单（提醒勿作现行依据）
    ├── <一级分类>/
    │   ├── <文档>.md
    │   └── <二级分类>/<文档>.md
    └── _未编目/             .hhc 未收录的孤儿页

用法:
  python3 scripts/export_chm.py <chm文件> <输出目录> [选项]

选项:
  --force              忽略清单，全部重新转换
  --prune              删除输出目录中已不在索引内的过期 .md（默认只在报告中列出）
  --workers N          并行进程数（默认 CPU 数，上限 8）
  --reuse-extract DIR  复用已解压目录，跳过 7z 解压（大幅提速迭代）
  --with-assets        复制 *.files 图片资源目录到对应文档旁（体积大，默认关闭）
  --layout             tree(默认) | chapter
  --no-front-matter    不写入 YAML 元数据
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "app", "server"))

MANIFEST_NAME = "_manifest.json"
REPORT_NAME = "_sync_report.json"
INDEX_NAME = "_index.md"
DEPRECATED_NAME = "_废止清单.md"

# 渲染版本：任何影响输出内容/链接的转换逻辑变更时递增，
# 使下次运行自动全量重渲染（避免增量跳过导致链接或格式过期）
# 6: 修复两条丢图路径（表格/标题内联上下文、超大页面快速通道），
#    全库恢复约 2889 张图（占 43.6%）；泳道图还原为 Markdown 表格；
#    渲染期剔除分块 OCR 的接缝残片。
RENDER_VERSION = "chm-tree-6"


def _render_fingerprint(layout: str, front_matter: bool, link_map: dict,
                        image_mode: str = "none",
                        image_max_mb: float = 5.0,
                        ocr_enabled: bool = False) -> str:
    """输出指纹：捕获"源内容之外"影响结果的因素

    仅取链接映射的**输出路径**（排序后）而非源绝对路径，
    以保证解压临时目录变化不会导致指纹漂移。
    """
    h = hashlib.sha1()
    h.update(RENDER_VERSION.encode())
    h.update(layout.encode())
    h.update(b"1" if front_matter else b"0")
    h.update(image_mode.encode())
    h.update(str(image_max_mb).encode())
    h.update(b"1" if ocr_enabled else b"0")
    for dst in sorted(set(link_map.values())):
        h.update(dst.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


# ----------------------------------------------------------------------
# 子进程渲染
# ----------------------------------------------------------------------

_W = {}


def _worker_init(layout: str, front_matter: bool, chm_name: str, link_map: dict,
                 image_mode: str = "none", image_max_mb: float = 5.0,
                 assets_root: str = "", chm_root: str = "", ocr_cfg: dict = None):
    from converters.chm_converter import CHMConverter
    conv = CHMConverter(workers=1, layout=layout, front_matter=front_matter,
                        image_mode=image_mode, image_max_mb=image_max_mb,
                        assets_root=assets_root, ocr_cfg=ocr_cfg)
    conv.chm_root = chm_root
    _W["conv"] = conv
    _W["chm_name"] = chm_name
    _W["link_map"] = link_map


def _worker_render(doc: dict):
    """渲染单个文档；异常不中断整体流程"""
    stats = {}
    try:
        md = _W["conv"].render_document(doc, _W["chm_name"], _W["link_map"], stats)
        return doc["rel_path"], md, None, stats
    except Exception as e:  # noqa: BLE001
        return doc["rel_path"], "", "{}: {}".format(type(e).__name__, e)[:200], stats


# ----------------------------------------------------------------------
# OCR 预处理（独立阶段：各进程持有自己的引擎；渲染阶段只读缓存）
# ----------------------------------------------------------------------

_OCR_CFG = {}


def _ocr_worker_init(cfg: dict, cache_dir: str):
    _OCR_CFG.clear()
    _OCR_CFG.update(cfg)
    _OCR_CFG["cache_dir"] = cache_dir


def _ocr_worker(path: str):
    import ocr as _ocr
    try:
        d = _ocr.ocr_image(path, _OCR_CFG, use_cache=True)
        return path, len(d.get("text") or ""), d.get("skipped"), None
    except Exception as e:  # noqa: BLE001
        return path, 0, None, "{}: {}".format(type(e).__name__, e)[:150]


# ---- 图片引用收集（必须走 _html_to_md：CHM 内 HTML 多为 GBK，
#      直接按 utf-8 读会把中文路径读坏，导致引用解析全部失败）----

_COL = {}


def _collect_init(chm_root: str):
    from converters.chm_converter import CHMConverter
    c = CHMConverter(workers=1)
    c.chm_root = chm_root
    _COL["conv"] = c


def _collect_worker(src: str):
    c = _COL["conv"]
    try:
        md = c._html_to_md(src)
    except Exception:
        return []
    src_dir = os.path.dirname(src)
    out = []
    for m in re.findall(r'!\[[^\]]*\]\(([^)\s]+)\)', md):
        real = c._resolve_ref(m, src_dir)
        if real:
            out.append(real)
    return out


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def _load_ocr_config() -> dict:
    """取 OCR 默认配置（优先 config.ocr，缺失时用 ocr 模块默认值）"""
    try:
        from config import get_config
        cfg = dict(get_config().get("ocr") or {})
    except Exception:
        cfg = {}
    try:
        import ocr as _ocr
        base = _ocr.default_config()
    except Exception:
        base = {}
    base.update({k: v for k, v in cfg.items() if v is not None})
    return base


def _md_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _url_quote(rel_path: str) -> str:
    from urllib.parse import quote
    return quote(rel_path)


def _copy_assets(doc: dict, out_dir: str):
    """把源 HTML 旁的 *.files 资源目录复制到输出 MD 同级，保持相对引用可用"""
    for src in doc["source_files"]:
        src_dir = os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        asset_dir = os.path.join(src_dir, stem + ".files")
        if not os.path.isdir(asset_dir):
            continue
        dst = os.path.join(os.path.dirname(os.path.join(out_dir, doc["rel_path"])),
                           stem + ".files")
        if os.path.isdir(dst):
            continue
        try:
            shutil.copytree(asset_dir, dst)
        except OSError as e:
            print("  ! 资源复制失败 {}: {}".format(asset_dir, e), file=sys.stderr)


def _build_index(docs: list, chm_name: str) -> str:
    """生成嵌套目录索引（Markdown 链接）"""
    from collections import OrderedDict
    tree = OrderedDict()
    for d in docs:
        parts = d["rel_path"].split("/")
        node = tree
        for p in parts[:-1]:
            node = node.setdefault(p, OrderedDict())
        node.setdefault("__docs__", []).append((parts[-1], d))

    def render(node, depth, lines):
        for name, sub in node.items():
            if name == "__docs__":
                for _, d in sorted(sub, key=lambda x: x[0]):
                    flag = " ⚠️已废止" if d["status"] == "已废止" else ""
                    lines.append("{}- [{}]({}){}".format(
                        "  " * depth, d["title"], _url_quote(d["rel_path"]), flag))
                continue
            lines.append("{}- **{}/**".format("  " * depth, name))
            render(sub, depth + 1, lines)

    lines = ["# {} — 目录索引".format(chm_name), "",
             "共 {} 篇文档，其中已废止 {} 篇。".format(
                 len(docs), sum(1 for d in docs if d["status"] == "已废止")), ""]
    render(tree, 0, lines)
    return "\n".join(lines) + "\n"


def _build_deprecated(docs: list) -> str:
    dep = [d for d in docs if d["status"] == "已废止"]
    lines = ["# 已废止文档清单", "",
             "共 {} 篇。以下文档已废止，仅供历史留存，**请勿作为现行依据**。".format(len(dep)),
             ""]
    for d in sorted(dep, key=lambda x: x["rel_path"]):
        lines.append("- [{}]({})　`{}`".format(
            d["title"], _url_quote(d["rel_path"]), d["toc_path"]))
    return "\n".join(lines) + "\n"


def _prune_empty_dirs(root: str):
    for cur, _dirs, _files in os.walk(root, topdown=False):
        if os.path.abspath(cur) == os.path.abspath(root):
            continue
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
        except OSError:
            pass


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="CHM → Markdown 目录树导出")
    ap.add_argument("chm", help="CHM 源文件路径")
    ap.add_argument("out_dir", help="输出目录")
    ap.add_argument("--force", action="store_true", help="全部重新转换")
    ap.add_argument("--prune", action="store_true", help="删除已不在索引内的过期 .md")
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4),
                    help="并行进程数（默认 CPU 数，上限 8）")
    ap.add_argument("--reuse-extract", default="", metavar="DIR",
                    help="复用已解压目录，跳过 7z 解压")
    ap.add_argument("--with-assets", action="store_true",
                    help="[已弃用] 等价于 --image-mode link")
    ap.add_argument("--image-mode", default="none",
                    choices=["none", "link", "base64"],
                    help="图片处理：none=保留原引用(默认) / "
                         "link=复制到文档同级并改写相对链接 / "
                         "base64=内联为 data URI（单文件自包含）")
    ap.add_argument("--image-max-mb", type=float, default=5.0,
                    help="base64 模式下超过该大小的图片不内联（默认 5MB）")
    ap.add_argument("--ocr", action="store_true",
                    help="启用 OCR：把图片中的文字提取出来注入 MD（纯 CPU）")
    ap.add_argument("--ocr-cache", default="", metavar="DIR",
                    help="OCR 结果缓存目录（默认取配置 ocr.cache_dir）")
    ap.add_argument("--ocr-workers", type=int, default=0,
                    help="OCR 并行进程数（默认取 --workers）")
    ap.add_argument("--ocr-threads", type=int, default=0,
                    help="每个 OCR 进程的 ONNX 线程数（默认取配置，2）")
    ap.add_argument("--ocr-tile-threshold", type=int, default=0,
                    help="最长边超过该值则分块 OCR（默认 960，实测可避免小字漏检）")
    ap.add_argument("--no-placeholder-missing", action="store_true",
                    help="图片缺失时不写占位文本（默认写）")
    ap.add_argument("--layout", default="tree", choices=["tree", "chapter"])
    ap.add_argument("--no-front-matter", action="store_true", help="不写入 YAML 元数据")
    ap.add_argument("--progress-every", type=int, default=200, help="进度打印间隔")
    args = ap.parse_args()

    chm_path = os.path.abspath(args.chm)
    out_dir = os.path.abspath(args.out_dir)
    chm_name = os.path.basename(chm_path)

    if not os.path.isfile(chm_path):
        print("找不到 CHM 文件: {}".format(chm_path), file=sys.stderr)
        return 2

    from converters.chm_converter import CHMConverter

    image_mode = "link" if args.with_assets else args.image_mode

    # ---- OCR 配置（默认取 config.ocr，可被命令行覆盖）----
    ocr_cfg = _load_ocr_config()
    if args.ocr:
        ocr_cfg["enabled"] = True
    if args.ocr_cache:
        ocr_cfg["cache_dir"] = os.path.abspath(args.ocr_cache)
    ocr_cfg["cache_dir"] = os.path.expanduser(ocr_cfg.get("cache_dir") or "")
    if args.ocr_threads:
        ocr_cfg["intra_op_num_threads"] = args.ocr_threads
    if args.ocr_tile_threshold:
        ocr_cfg["tile_threshold"] = args.ocr_tile_threshold
        ocr_cfg["tile_size"] = args.ocr_tile_threshold
    if args.no_placeholder_missing:
        ocr_cfg["placeholder_missing"] = False

    conv = CHMConverter(
        workers=1,
        layout=args.layout,
        front_matter=not args.no_front_matter,
        image_mode=image_mode,
        image_max_mb=args.image_max_mb,
        assets_root=os.path.abspath(args.out_dir),
        ocr_cfg=ocr_cfg,
    )
    conv.chm_root = Path(chm_path).stem

    t0 = time.time()
    if args.reuse_extract:
        reuse_root = Path(args.reuse_extract)
        if not reuse_root.is_dir():
            print("复用目录不存在: {}".format(reuse_root), file=sys.stderr)
            return 2
        reused = [p for p in reuse_root.rglob("*") if p.is_file()]
        print("== 1/4 复用已解压目录: {} ({} 个文件)".format(reuse_root, len(reused)))
        conv.extract = lambda fp, dest, _f=reused: _f
    else:
        print("== 1/4 解压并解析索引: {}".format(chm_name))

    docs, work = conv.to_toc_documents(chm_path)
    print("   索引条目: {}".format(len(docs)))

    os.makedirs(out_dir, exist_ok=True)

    try:
        # ---- 增量比对 ----
        manifest_path = os.path.join(out_dir, MANIFEST_NAME)
        old = _load_json(manifest_path) or {}
        old_docs = old.get("documents", {}) or {}

        # 输出指纹变化（布局/元数据/链接映射/渲染版本）→ 必须全量重渲染，
        # 否则源文件未变但链接可能已过期的文档会被错误跳过
        link_map = conv.build_link_map(docs)
        fingerprint = _render_fingerprint(
            args.layout, not args.no_front_matter, link_map,
            image_mode, args.image_max_mb, bool(ocr_cfg.get("enabled")))
        changed_fp = old.get("render_fingerprint") != fingerprint
        force_all = args.force or changed_fp

        added, modified, unchanged, to_render = [], [], [], []
        for d in docs:
            rel = d["rel_path"]
            prev = old_docs.get(rel)
            target = os.path.join(out_dir, rel)
            if force_all or prev is None:
                added.append(rel)
                to_render.append(d)
            elif prev.get("source_hash") != d["source_hash"] or not os.path.isfile(target):
                modified.append(rel)
                to_render.append(d)
            else:
                unchanged.append(rel)

        if changed_fp and old:
            print("   输出指纹变化（{}→{}），全量重渲染".format(
                (old.get("render_fingerprint") or "无")[:8], fingerprint[:8]))

        new_paths = {d["rel_path"] for d in docs}

        # 过期文件：输出目录中已存在、但不在本次索引内的 .md
        # 从文件系统推导（而非仅比对旧清单），使未被 --prune 清理的文件
        # 在后续每次运行中都能被重新识别，不会"失踪"。
        present = set()
        for cur, _dirs, fnames in os.walk(out_dir):
            for fn in fnames:
                if not fn.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(cur, fn), out_dir).replace(os.sep, "/")
                if rel in (INDEX_NAME, DEPRECATED_NAME):
                    continue
                present.add(rel)
        deleted = sorted(present - new_paths)

        # ---- OCR 预处理：对本次要渲染的文档所引用的图片做识别（带缓存）----
        ocr_stats = {}
        if ocr_cfg.get("enabled"):
            import ocr as _ocr
            cache_dir = ocr_cfg["cache_dir"]
            os.makedirs(cache_dir, exist_ok=True)
            conv.ocr_cfg = ocr_cfg
            conv.ocr_enabled = True

            # 收集需要 OCR 的唯一图片（走 _html_to_md，正确处理 GBK 页面）
            srcs = sorted({f for d in to_render for f in d["source_files"]})
            imgs = set()
            nw = max(1, args.workers)
            if nw == 1:
                _collect_init(conv.chm_root)
                for s in srcs:
                    imgs.update(_collect_worker(s))
            else:
                with ProcessPoolExecutor(max_workers=nw,
                                         initializer=_collect_init,
                                         initargs=(conv.chm_root,)) as pool:
                    for lst in pool.map(_collect_worker, srcs, chunksize=4):
                        imgs.update(lst)
            imgs = sorted(imgs)
            todo = []
            cached_hits = 0
            for p in imgs:
                try:
                    k = _ocr.image_key(p)
                except OSError:
                    continue
                if _ocr.cache_load(cache_dir, k) is not None:
                    cached_hits += 1
                else:
                    todo.append(p)
            print("== OCR 预处理: 引用图片 {} 张（缓存命中 {}，待识别 {}）".format(
                len(imgs), cached_hits, len(todo)))
            ocr_stats = {"images": len(imgs), "cached": cached_hits, "todo": len(todo)}

            if todo:
                ow = args.ocr_workers or max(1, args.workers)
                t_ocr = time.time()
                done = chars = failed = skipped = 0
                if ow == 1:
                    _ocr_worker_init(ocr_cfg, cache_dir)
                    for i, p in enumerate(todo, 1):
                        _, c, sk, err = _ocr_worker(p)
                        done += 1
                        chars += c
                        if err:
                            failed += 1
                        if sk:
                            skipped += 1
                        if i % 200 == 0:
                            print("   OCR 进度 {}/{}  已识别文字 {} 字  用时 {:.0f}s".format(
                                i, len(todo), chars, time.time() - t_ocr))
                else:
                    with ProcessPoolExecutor(
                        max_workers=ow,
                        initializer=_ocr_worker_init,
                        initargs=(ocr_cfg, cache_dir),
                    ) as pool:
                        for i, (_p, c, sk, err) in enumerate(
                                pool.map(_ocr_worker, todo, chunksize=2), 1):
                            done += 1
                            chars += c
                            if err:
                                failed += 1
                            if sk:
                                skipped += 1
                            if i % 200 == 0:
                                print("   OCR 进度 {}/{}  已识别文字 {} 字  用时 {:.0f}s".format(
                                    i, len(todo), chars, time.time() - t_ocr))
                ocr_stats.update({"done": done, "chars": chars,
                                  "failed": failed, "skipped": skipped,
                                  "seconds": round(time.time() - t_ocr, 1)})
                print("   OCR 完成: {} 张 / {:.0f}s，共识别 {} 字（跳过小图 {}，失败 {}）".format(
                    done, time.time() - t_ocr, chars, skipped, failed))

        print("== 2/4 转换: 新增 {} / 变更 {} / 未变 {} / 删除 {} / 合计 {}".format(
            len(added), len(modified), len(unchanged), len(deleted), len(docs)))

        # ---- 渲染并流式落盘（内存中只保留哈希，不保留全文）----
        stat = {}
        failures = []

        def emit(rel, content):
            if not content or not content.strip():
                return 0
            _write_text(os.path.join(out_dir, rel), content)
            stat[rel] = {"md_sha1": _md_sha1(content),
                         "bytes": len(content.encode("utf-8"))}
            return 1

        written = 0
        img_stats = {}
        if to_render:
            workers = max(1, args.workers)
            if workers == 1:
                for i, d in enumerate(to_render, 1):
                    _s = {}
                    rel = d["rel_path"]
                    content = conv.render_document(d, chm_name, link_map, _s)
                    err = None
                    for k, v in _s.items():
                        img_stats[k] = img_stats.get(k, 0) + v
                    written += emit(rel, content)
                    if i % args.progress_every == 0:
                        print("   渲染进度 {}/{}".format(i, len(to_render)))
            else:
                with ProcessPoolExecutor(
                    max_workers=workers,
                    initializer=_worker_init,
                    initargs=(args.layout, not args.no_front_matter, chm_name,
                              link_map, image_mode, args.image_max_mb,
                              os.path.abspath(args.out_dir), Path(chm_path).stem,
                              ocr_cfg),
                ) as pool:
                    for i, (rel, content, err, _s) in enumerate(
                            pool.map(_worker_render, to_render, chunksize=4), 1):
                        if err:
                            failures.append((rel, err))
                        for k, v in (_s or {}).items():
                            img_stats[k] = img_stats.get(k, 0) + v
                        written += emit(rel, content)
                        if i % args.progress_every == 0:
                            print("   渲染进度 {}/{}".format(i, len(to_render)))

        # ---- 过期文件清理（默认仅报告）----
        if args.prune and deleted:
            for rel in deleted:
                p = os.path.join(out_dir, rel)
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError as e:
                        print("  ! 删除失败 {}: {}".format(rel, e), file=sys.stderr)
            _prune_empty_dirs(out_dir)

        # ---- 清单 / 报告 / 索引 ----
        print("== 3/4 生成清单与索引")
        now = datetime.now().isoformat(timespec="seconds")
        st = os.stat(chm_path)
        documents = {}
        for d in docs:
            rel = d["rel_path"]
            s = stat.get(rel)
            if s is None:
                prev = old_docs.get(rel) or {}
                if prev.get("md_sha1"):
                    s = {"md_sha1": prev["md_sha1"], "bytes": prev.get("bytes", 0)}
                else:
                    c = ""
                    p = os.path.join(out_dir, rel)
                    if os.path.isfile(p):
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                c = f.read()
                        except OSError:
                            pass
                    s = {"md_sha1": _md_sha1(c) if c else "",
                         "bytes": len(c.encode("utf-8")) if c else 0}
            documents[rel] = {
                "title": d["title"],
                "status": d["status"],
                "toc_path": d["toc_path"],
                "source_files": [os.path.basename(f) for f in d["source_files"]],
                "source_hash": d["source_hash"],
                "md_sha1": s["md_sha1"],
                "bytes": s["bytes"],
            }

        manifest = {
            "schema": 1,
            "generated_at": now,
            "source_chm": {"name": chm_name, "size": st.st_size,
                           "mtime": int(st.st_mtime)},
            "layout": args.layout,
            "front_matter": not args.no_front_matter,
            "render_version": RENDER_VERSION,
            "render_fingerprint": fingerprint,
            "image_mode": image_mode,
            "image_max_mb": args.image_max_mb,
            "image_stats": img_stats,
            "ocr_stats": ocr_stats,
            "document_count": len(docs),
            "deprecated_count": sum(1 for d in docs if d["status"] == "已废止"),
            "documents": documents,
        }
        _write_text(manifest_path,
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        report = {
            "generated_at": now,
            "source_chm": chm_name,
            "layout": args.layout,
            "counts": {"total": len(docs), "added": len(added),
                       "modified": len(modified), "unchanged": len(unchanged),
                       "deleted": len(deleted), "failed": len(failures)},
            "added": sorted(added),
            "modified": sorted(modified),
            "deleted": deleted,
            "failed": [{"path": r, "error": e} for r, e in failures],
            "image_stats": img_stats,
            "ocr_stats": ocr_stats,
            "pruned": bool(args.prune),
        }
        _write_text(os.path.join(out_dir, REPORT_NAME),
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n")

        _write_text(os.path.join(out_dir, INDEX_NAME), _build_index(docs, chm_name))
        _write_text(os.path.join(out_dir, DEPRECATED_NAME), _build_deprecated(docs))

        print("== 4/4 完成，用时 {:.1f}s".format(time.time() - t0))
        print("   文档总数: {}   已废止: {}".format(
            len(docs), manifest["deprecated_count"]))
        print("   本次写入: {} (新增 {} / 变更 {})".format(
            written, len(added), len(modified)))
        if image_mode != "none" and img_stats:
            mb = img_stats.get("bytes", 0) / 1048576
            print("   图片模式: {}  定位 {} / 缺失 {}  落盘 {}  合计 {:.1f} MB".format(
                image_mode, img_stats.get("found", 0), img_stats.get("missing", 0),
                img_stats.get("copied", img_stats.get("inlined", 0)), mb))
        if ocr_cfg.get("enabled") and img_stats:
            print("   OCR 注入: 命中 {} 张 / 缺失 {} / 空文本 {}  注入文字 {} 字".format(
                img_stats.get("ocr_hit", 0), img_stats.get("ocr_miss", 0),
                img_stats.get("ocr_empty", 0), img_stats.get("ocr_chars", 0)))
        if img_stats.get("oversize"):
                print("   超限未内联(>{:g}MB): {} 张".format(
                    args.image_max_mb, img_stats["oversize"]))
        if failures:
            print("   ⚠️ 渲染失败: {} 项（详见 _sync_report.json）".format(len(failures)))
        if deleted:
            print("   已删除条目: {} 项{}".format(
                len(deleted),
                "" if args.prune else "（仅报告，未物理删除；加 --prune 可清理）"))
        print("   输出目录: {}".format(out_dir))
        return 0
    finally:
        # 仅清理本次自建的临时解压目录（--reuse-extract 的目录不受影响，
        # 因为 to_toc_documents 自建 work 目录，复用目录只被读取）
        if work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
