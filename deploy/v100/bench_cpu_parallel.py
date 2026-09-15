#!/usr/bin/env python3
"""V100 主机 CPU OCR 并行度标定（36 核）"""
import os, sys, time, glob, random, shutil
from concurrent.futures import ProcessPoolExecutor
from rapidocr import RapidOCR

_C = {}
def init(threads, cache):
    _C["eng"] = RapidOCR(params={"Global.log_level": "error",
                                 "EngineConfig.onnxruntime.intra_op_num_threads": threads})
def work(p):
    r = _C["eng"](p)
    return sum(len(x) for x in (list(r.txts) if getattr(r, "txts", None) else []))

if __name__ == "__main__":
    random.seed(11)
    imgs = [p for p in glob.glob("/imgs/**/*", recursive=True)
            if p.lower().endswith((".png", ".jpg")) and os.path.getsize(p) > 3000]
    sample = random.sample(imgs, 64)
    print("抽样 %d 张（共 %d）" % (len(sample), len(imgs)), flush=True)
    for procs, threads in ((1, 4), (1, 36), (4, 4), (8, 4), (12, 3)):
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=procs, initializer=init,
                                 initargs=(threads, None)) as pool:
            list(pool.map(work, sample, chunksize=1))
        dt = time.perf_counter() - t0
        rate = len(sample) / dt
        print("%2d进程×%2d线程: %6.1fs  单张 %5.2fs  吞吐 %5.2f 张/秒  → 4000张约 %4.1f 分钟"
              % (procs, threads, dt, dt/len(sample), rate, 4000/rate/60), flush=True)
