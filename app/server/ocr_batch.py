#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OCR 预处理（按需调用）——把图片识别结果写进共享缓存，供转换期零成本注入。

为什么要单独一步：GPU 主机（如家里 2080Ti）平时被大模型占着，**不该常驻 OCR 服务**。
于是把 OCR 从"转换时的内联调用"改成"批量预处理"：

    1. 扫描待转换文档产生的图片（或任意目录）
    2. 仅对缓存未命中的图调一次引擎（本地 CPU 或远程 GPU）
    3. 结果按「图片内容 SHA-1 + 配置签名」写入 cache_dir
    4. 转换时 ocr.inline_fallback=false，只读缓存 —— 转换过程完全不碰 GPU

远程 GPU 的生命周期由 --start-cmd / --stop-cmd 控制，跑完即关：

    python ocr_batch.py --source /data/output \\
        --backend remote --remote-url http://gpu-host:8890/ocr --token xxx \\
        --start-cmd "ssh gpu-host 'serve_ocr.py --idle-timeout 300 &'" \\
        --stop-cmd  "ssh gpu-host 'curl -XPOST .../shutdown'"

关键：预处理与转换必须用**同一份 ocr 配置**（尤其 backend 与 cache_dir），
否则配置签名不同会导致缓存全部失效、转换期又去现场识别。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ocr

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}


def iter_images(root: Path, recursive: bool = True):
    if root.is_file():
        if root.suffix.lower() in IMG_EXT:
            yield root
        return
    it = root.rglob("*") if recursive else root.glob("*")
    for p in it:
        if p.is_file() and p.suffix.lower() in IMG_EXT:
            yield p


def build_cfg(a) -> dict:
    cfg = ocr.default_config()
    cfg.update({
        "enabled": True,
        "backend": a.backend,
        "cache_dir": a.cache_dir,
        "min_image_side": a.min_side,
        "inline_fallback": False,      # 预处理阶段不用内联回退
    })
    if a.backend == "remote":
        cfg["remote"] = {
            "url": a.remote_url,
            "token": a.remote_token,
            "timeout": a.remote_timeout,
            "send_b64": a.send_b64,
        }
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="目录或单张图片")
    ap.add_argument("--cache-dir", default=ocr.default_config()["cache_dir"])
    ap.add_argument("--backend", default="rapidocr", choices=["rapidocr", "remote"])
    ap.add_argument("--remote-url", default="")
    ap.add_argument("--remote-token", default="")
    ap.add_argument("--remote-timeout", type=int, default=300)
    ap.add_argument("--send-b64", action="store_true",
                    help="跨机传输图片本体（同机共享卷用默认的路径模式即可）")
    ap.add_argument("--min-side", type=int, default=32,
                    help="任一边小于该值的图跳过（装饰/碎片）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发数：本地 CPU 建议 1~2；远程 GPU 建议 2~4")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（0=全部）")
    ap.add_argument("--retry", type=int, default=1, help="失败重试次数")
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只统计不识别")
    ap.add_argument("--start-cmd", default="", help="批前执行的命令（拉起远程服务）")
    ap.add_argument("--stop-cmd", default="", help="批后执行的命令（关闭远程服务）")
    ap.add_argument("--report", default="", help="把统计写到该 JSON")
    a = ap.parse_args(argv)

    src = Path(a.source)
    if not src.exists():
        print("source 不存在: %s" % src, file=sys.stderr)
        return 2

    images = list(iter_images(src, not a.no_recursive))
    if a.limit:
        images = images[:a.limit]
    if not images:
        print("未找到图片", file=sys.stderr)
        return 1

    cfg = build_cfg(a)
    print("待处理图片 %d 张｜backend=%s｜cache=%s"
          % (len(images), a.backend, a.cache_dir), flush=True)

    # 预筛：已缓存 / 过小 的直接跳过，省掉传输与推理
    todo, cached, too_small = [], 0, 0
    for p in images:
        try:
            key = ocr.image_key(str(p))
        except OSError as e:
            print("跳过不可读 %s: %s" % (p, e), file=sys.stderr)
            continue
        hit = ocr.cache_load(cfg["cache_dir"], key)
        sig = ocr._config_signature(cfg)
        if hit is not None and hit.get("sig", sig) == sig:
            cached += 1
            continue
        todo.append(p)

    print("已缓存 %d 张，需识别 %d 张" % (cached, len(todo)), flush=True)
    if a.dry_run:
        print("dry-run：不执行识别")
        return 0

    started = 0

    def run_cmd(cmd, tag):
        if not cmd:
            return True
        import subprocess
        print("[%s] %s" % (tag, cmd), flush=True)
        r = subprocess.run(cmd, shell=True)
        if r.returncode != 0:
            print("[%s] 失败 exit=%d（继续执行）" % (tag, r.returncode), file=sys.stderr)
        return r.returncode == 0

    def one(p: Path):
        last_err = ""
        for attempt in range(max(1, a.retry + 1)):
            try:
                data = ocr.ocr_image(str(p), cfg, use_cache=True)
                text = (data.get("text") or "").strip()
                q = ocr.quality_of(data, cfg)
                return {"path": str(p), "chars": len(text), "level": q["level"],
                        "score": q["score"], "error": "", "attempt": attempt + 1}
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:200])
                time.sleep(min(5, 2 ** attempt))
        return {"path": str(p), "chars": 0, "level": "error", "score": None,
                "error": last_err, "attempt": max(1, a.retry + 1)}

    t0 = time.time()
    run_cmd(a.start_cmd, "start")
    started = time.time()
    results = []
    try:
        if a.workers > 1:
            with ThreadPoolExecutor(max_workers=a.workers) as pool:
                for i, r in enumerate(pool.map(one, todo), 1):
                    results.append(r)
                    if i % 20 == 0 or i == len(todo):
                        print("  %d/%d …" % (i, len(todo)), flush=True)
        else:
            for i, p in enumerate(todo, 1):
                r = one(p)
                results.append(r)
                if i % 20 == 0 or i == len(todo):
                    print("  %d/%d …" % (i, len(todo)), flush=True)
    finally:
        run_cmd(a.stop_cmd, "stop")

    elapsed = time.time() - (started or t0)
    ok = [r for r in results if not r["error"]]
    with_text = [r for r in ok if r["chars"] > 0]
    low = [r for r in ok if r["level"] == "low"]
    no_text = [r for r in ok if r["level"] == "no_text"]
    failed = [r for r in results if r["error"]]

    summary = {
        "total_images": len(images),
        "cached": cached,
        "processed": len(results),
        "with_text": len(with_text),
        "no_text": len(no_text),
        "low_quality": len(low),
        "failed": len(failed),
        "elapsed_seconds": round(elapsed, 1),
        "avg_seconds_per_image": round(elapsed / max(1, len(results)), 3),
        "backend": a.backend,
    }
    print("\n=== 预处理完成 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed:
        print("失败样例：", [(r["path"], r["error"][:100]) for r in failed[:5]])
    if low or no_text:
        print("低质量/无文字（仅标注，不重试）：", len(low) + len(no_text), "张")
        for r in (low + no_text)[:10]:
            print("  [%s] %s" % (r["level"], r["path"]))

    if a.report:
        Path(a.report).write_text(json.dumps(
            {"summary": summary, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("报告：%s" % a.report)
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(main())
