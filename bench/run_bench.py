"""bench 运行器：对冻结语料逐份转换，产出 Markdown + 耗时 + L1 规则分。

用法：
  python bench/run_bench.py              # 跑一轮，写 bench/scores.csv + bench/out/
  python bench/run_bench.py --check      # 与 bench/frozen_scores.json 回归护栏比较

回归护栏规则：
  - 单文档 L1 分数较冻结值下跌 >5 分 → exit 1
  - 语料均分较冻结值下跌 >3 分   → exit 1
"""
import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "app" / "server"
OUT_DIR = HERE / "out"
SCORES = HERE / "scores.csv"
FROZEN = HERE / "frozen_scores.json"

sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(HERE))

from corpus import corpus_files  # noqa: E402
from quality import assess_quality  # noqa: E402

RUNNER = r'''
import sys, json, time
sys.path.insert(0, r"%(server)s")
from converters.loader import register_all
from converters import registry
from sniffer import sniff_format

register_all(registry)
path = sys.argv[1]
out_path = sys.argv[2]
ext, _ = sniff_format(path)
conv = registry.get(ext)
if conv is None or not getattr(conv, "available", True):
    print(json.dumps({"ok": False, "error": "no converter for " + ext}))
    sys.exit(0)
t0 = time.perf_counter()
try:
    out = conv.convert(path)
    dt = time.perf_counter() - t0
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(out or "")
    print(json.dumps({"ok": True, "chars": len(out or ""), "seconds": round(dt, 3)}))
except Exception as e:
    print(json.dumps({"ok": False, "error": "%%s: %%s" %% (type(e).__name__, str(e)[:100])}))
''' % {"server": str(SERVER)}


def convert_one(src: Path, out_md: Path) -> dict:
    """子进程转换单份语料，写 out_md，返回 {ok, chars, seconds, score}"""
    runner = OUT_DIR / "_runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, str(runner), str(src), str(out_md)],
                          capture_output=True, timeout=120, cwd=str(OUT_DIR))
    wall = time.perf_counter() - t0
    import json as _json
    text = proc.stdout.decode("utf-8", errors="replace").strip()
    line = text.splitlines()[-1] if text else "{}"
    try:
        res = _json.loads(line)
    except Exception:
        res = {"ok": False, "error": "runner crash: " + text[:100]}

    entry = {"ok": bool(res.get("ok")),
             "seconds": round(res.get("seconds", wall), 3),
             "chars": res.get("chars", 0)}
    if res.get("ok"):
        md = out_md.read_text(encoding="utf-8", errors="replace") if out_md.exists() else ""
        entry["score"] = assess_quality(md, src.suffix.lower()).score
    else:
        entry["error"] = res.get("error", "unknown")
        entry["score"] = 0
    return entry


def run_all() -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    results = {}
    for name, path in corpus_files():
        out_md = OUT_DIR / (Path(name).stem + ".md")
        try:
            entry = convert_one(path, out_md)
        except subprocess.TimeoutExpired:
            entry = {"ok": False, "error": "timeout>120s", "seconds": 120, "score": 0}
        results[name] = entry
        status = "OK %5.1f分 %6.2fs" % (entry["score"], entry["seconds"]) if entry["ok"] \
            else "FAIL %s" % entry.get("error", "")[:60]
        print("%-28s %s" % (name, status))
    return results


def write_scores(results: dict):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_file = not SCORES.exists()
    with open(SCORES, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["ts", "file", "ok", "score", "seconds", "chars", "error"])
        for name, e in sorted(results.items()):
            w.writerow([now, name, e["ok"], e.get("score", ""), e.get("seconds", ""),
                        e.get("chars", ""), e.get("error", "")])


def check(results: dict) -> int:
    """回归护栏：与 frozen_scores.json 比较"""
    if not FROZEN.exists():
        frozen = {n: e.get("score", 0) for n, e in results.items()}
        avg = sum(frozen.values()) / max(len(frozen), 1)
        FROZEN.write_text(json.dumps(
            {"frozen_at": datetime.now(timezone.utc).isoformat(),
             "avg": round(avg, 2), "scores": frozen}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print("首次运行：冻结基线 frozen_scores.json（avg=%.2f）" % avg)
        return 0

    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    bad = []
    drops = []
    for name, e in sorted(results.items()):
        base = frozen["scores"].get(name)
        if base is None:
            continue
        if not e["ok"]:
            bad.append(name)
            continue
        if base - e["score"] > 5:
            drops.append((name, base, e["score"]))
    avg_now = sum(e.get("score", 0) for e in results.values()) / max(len(results), 1)
    avg_drop = frozen["avg"] - avg_now

    rc = 0
    if bad:
        print("回归失败：原本成功的文档现在转换失败：%s" % bad)
        rc = 1
    if drops:
        print("回归失败：单文档 L1 分数下跌 >5：%s" %
              [(n, b, s) for n, b, s in drops])
        rc = 1
    if avg_drop > 3:
        print("回归失败：语料均分 %.2f → %.2f（跌 %.2f > 3）" %
              (frozen["avg"], avg_now, avg_drop))
        rc = 1
    if rc == 0:
        print("回归通过：均分 %.2f（冻结 %.2f）" % (avg_now, frozen["avg"]))
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="跑完后与冻结基线比较")
    args = ap.parse_args()
    results = run_all()
    write_scores(results)
    if args.check:
        sys.exit(check(results))
    ok_n = sum(1 for e in results.values() if e["ok"])
    avg = sum(e.get("score", 0) for e in results.values()) / max(len(results), 1)
    print("完成：%d/%d 成功，均分 %.2f（明细见 %s）" % (ok_n, len(results), avg, SCORES))


if __name__ == "__main__":
    main()
