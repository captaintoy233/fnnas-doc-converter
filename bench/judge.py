"""L2 LLM 盲评：成对比较 docconverter 结构化 MD vs 朴素平文本抽取。

设计（成对互换去偏）：
  - 每份语料取两个版本：A=docconverter 输出（bench/out/<stem>.md），
    B=独立朴素抽取（pdf→pymupdf / docx→python-docx / xlsx→openpyxl / et(OLE2)→xlrd /
      doc,wps→Word piece table / ofd→XML TextCode）。
  - 盲评：judge 不知道哪个来自哪个转换器，只看到「文本A / 文本B」。
  - 位置互换跑两轮（A/B 与 B/A），只认两轮一致的胜者，消位置偏。
  - 评分权重：内容保真 50% + 结构可读 30% + 格式干净 20%。
    （重保真，防止「只奖励漂亮排版却丢内容」。）

用法：
  python bench/judge.py --pilot 10      # 抽样 N 份跨格式试点，看区分度
  python bench/judge.py                 # 全量（可 naive 抽取的格式）
结果写 bench/l2_results.csv。

依赖：litellm 网关 local-27b（思考模型，需给足 max_tokens）。
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "app" / "server"
OUT_DIR = HERE / "out"
CORPUS = ROOT / "tests" / "fixtures_corpus"
RESULTS = HERE / "l2_results.csv"

sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(HERE))
from corpus import corpus_files  # noqa: E402
from sniffer import sniff_format  # noqa: E402

GATEWAY = os.environ.get("L2_GATEWAY", "http://127.0.0.1:4000")
MODEL = os.environ.get("L2_MODEL", "local-27b")
# 密钥只从环境变量读取，绝不硬编码进仓库（公开仓安全）。
API_KEY = os.environ.get("L2_API_KEY", "")
CHAR_CAP = int(os.environ.get("L2_CHAR_CAP", "6000"))
MAX_TOKENS = int(os.environ.get("L2_MAX_TOKENS", "2500"))
# 思考模型对含二进制乱码的输入易退化推理循环（单轮 >180s）。L2_NO_THINK=1 关闭
# 思考链直接出 JSON，大幅提速；垃圾检测/结构对比这类显性判断无需深度推理。
NO_THINK = os.environ.get("L2_NO_THINK", "0") == "1"

RUBRIC = (
    "你是文档转换质量评审。下面给出同一份源文档的两个转换版本（文本A、文本B），"
    "你不知道它们分别来自哪个工具。请按以下权重判断哪个整体质量更高：\n"
    "  1) 内容保真 50%：是否完整保留源文档的文字/数字/表格数据，有无丢段、截断、乱码、占位符。\n"
    "  2) 结构可读 30%：标题/段落/表格组织是否清晰、便于阅读与下游解析。\n"
    "  3) 格式干净 20%：有无残留标记、断裂表格、多余空行、编码污染。\n"
    "若内容保真明显一方占优，优先据此判定。两者相当则判 tie。\n"
    "最后只输出一行 JSON：{\"winner\": \"A\" 或 \"B\" 或 \"tie\", \"reason\": \"不超过40字\"}"
)


def _openpyxl_text(path: Path) -> str:
    """openpyxl 只认 .xlsx 后缀；非 .xlsx（如 .et 实为 PK 包）先复制临时副本。"""
    import openpyxl
    load = str(path)
    tmp = None
    if path.suffix.lower() != ".xlsx":
        import shutil, tempfile
        fd, tmp = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
        shutil.copyfile(str(path), tmp)
        load = tmp
    try:
        wb = openpyxl.load_workbook(load, data_only=True, read_only=True)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _doc_text(path: Path) -> str:
    """独立 .doc/.wps 文本抽取：FIB → CLX piece table → 逐片解码。
    刻意不复用 docconverter 的 fcMin/fcMac+启发式（后者对 fComplex 文档会把
    格式二进制数据当文本，产生大块乱码）。piece table 只取真正的正文片。"""
    import struct
    import olefile
    ole = olefile.OleFileIO(str(path))
    try:
        wd = ole.openstream('WordDocument').read()
        flags = struct.unpack_from('<H', wd, 0x0A)[0]
        which = '1Table' if (flags & 0x0200) else '0Table'
        if not ole.exists(which):
            which = '0Table' if ole.exists('0Table') else '1Table'
        tbl = ole.openstream(which).read()
    finally:
        ole.close()
    fc_clx, lcb_clx = struct.unpack_from('<II', wd, 0x01A2)
    clx = tbl[fc_clx:fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:  # 跳过 Prc
        cb = struct.unpack_from('<H', clx, pos + 1)[0]
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise ValueError('no Pcdt in CLX')
    lcb = struct.unpack_from('<I', clx, pos + 1)[0]
    plc = clx[pos + 5:pos + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from('<%dI' % (n + 1), plc, 0)
    pcd_off = (n + 1) * 4
    out = []
    for i in range(n):
        fc_raw = struct.unpack_from('<I', plc, pcd_off + i * 8 + 2)[0]
        compressed = bool(fc_raw & 0x40000000)
        fc = fc_raw & 0x3FFFFFFF
        cch = cps[i + 1] - cps[i]
        if compressed:
            out.append(wd[fc // 2:fc // 2 + cch].decode('cp1252', errors='replace'))
        else:
            out.append(wd[fc:fc + cch * 2].decode('utf-16-le', errors='replace'))
    text = ''.join(out)
    text = text.replace('\r', '\n').replace('\x0b', '\n').replace('\x07', ' ')
    text = re.sub(r'[\x00-\x08\x0c\x0e-\x1f]', '', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _ofd_text(path: Path) -> str:
    """独立 OFD 抽取：按页序拼接所有 TextCode（不做字体→标题/段落重组）。"""
    import zipfile
    from lxml import etree
    NS = '{http://www.ofdspec.org/2016}'
    z = zipfile.ZipFile(str(path))
    pages = [n for n in z.namelist()
             if n.lower().endswith('content.xml') and 'pages' in n.lower()]

    def pnum(n):
        m = re.search(r'Page_(\d+)', n)
        return int(m.group(1)) if m else 0
    pages.sort(key=pnum)
    lines = []
    for n in pages:
        root = etree.fromstring(z.read(n))
        for tc in root.iter(NS + 'TextCode'):
            if tc.text and tc.text.strip():
                lines.append(tc.text.strip())
    return '\n'.join(lines)


def naive_extract(path: Path) -> str:
    """独立朴素平文本抽取（不用 docconverter 代码），按嗅探格式分派。"""
    ext, _ = sniff_format(str(path))
    if ext in (".doc", ".wps"):
        return _doc_text(path)
    if ext == ".ofd":
        return _ofd_text(path)
    if ext == ".pdf":
        import fitz
        doc = fitz.open(str(path))
        return "\n".join(page.get_text() for page in doc)
    if ext == ".docx":
        from docx import Document as D
        d = D(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if ext in (".xlsx",):
        return _openpyxl_text(path)
    if ext in (".xls", ".et"):
        # OLE2 BIFF → xlrd；若实为 PK(xlsx) 则走 openpyxl
        head = path.read_bytes()[:4]
        if head[:2] == b"PK":
            return _openpyxl_text(path)
        import xlrd
        wb = xlrd.open_workbook(str(path))
        lines = []
        for sh in wb.sheets():
            for r in range(sh.nrows):
                cells = [str(sh.cell_value(r, c)) for c in range(sh.ncols)]
                if any(x.strip() for x in cells):
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    raise ValueError("no naive extractor for " + ext)


def _cap(text: str) -> str:
    text = text or ""
    if len(text) > CHAR_CAP:
        # 首+尾取样：doc/wps 的二进制乱码集中在正文之后的尾部，
        # 仅取头部会漏掉乱码、误判 md 更优；首尾各半才能暴露尾部污染。
        half = CHAR_CAP // 2
        return text[:half] + "\n…[中段省略]…\n" + text[-half:]
    return text


def _call_judge(first: str, second: str, first_label: str, second_label: str) -> dict:
    """first/second 是两段文本；按 first_label/second_label 标注呈现。"""
    prompt = (RUBRIC + "\n\n=== 文本A ===\n" + _cap(first)
              + "\n\n=== 文本B ===\n" + _cap(second))
    r = None
    for attempt in range(3):
        try:
            r = httpx.post(GATEWAY + "/v1/chat/completions",
                           headers={"Authorization": "Bearer " + API_KEY},
                           json={"model": MODEL,
                                 "messages": [{"role": "user", "content": prompt}],
                                 "max_tokens": MAX_TOKENS, "temperature": 0,
                                 "chat_template_kwargs": {"enable_thinking": not NO_THINK}},
                           timeout=180)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 2:
                return {"winner": "parse_error", "reason": "call_failed:" + str(e)[:40]}
            time.sleep(2 * (attempt + 1))
    content = r.json()["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{[^{}]*\"winner\"[^{}]*\}", content, re.S)
    if not m:
        return {"winner": "parse_error", "reason": content[:60]}
    try:
        d = json.loads(m.group(0))
        w = str(d.get("winner", "")).strip().upper()
        if w not in ("A", "B", "TIE"):
            w = "parse_error"
        return {"winner": w, "reason": str(d.get("reason", ""))[:60]}
    except Exception:
        return {"winner": "parse_error", "reason": content[:60]}


def blind_compare(md: str, naive: str) -> dict:
    """两轮位置互换，只认一致胜者。返回 {winner, consistent, r1, r2}。
    winner 以「docconverter(md) 是否胜出」表述：md / naive / tie / inconsistent。"""
    # 轮1：A=md, B=naive
    j1 = _call_judge(md, naive, "A", "B")
    # 轮2：A=naive, B=md（互换）
    j2 = _call_judge(naive, md, "A", "B")

    def to_md_side(j, md_is_A):
        w = j["winner"]
        if w == "TIE":
            return "tie"
        if w == "parse_error":
            return "error"
        if md_is_A:
            return "md" if w == "A" else "naive"
        return "md" if w == "B" else "naive"

    s1 = to_md_side(j1, True)
    s2 = to_md_side(j2, False)
    if s1 == s2 and s1 in ("md", "naive", "tie"):
        return {"winner": s1, "consistent": True, "r1": j1, "r2": j2}
    return {"winner": "inconsistent", "consistent": False, "r1": j1, "r2": j2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", type=int, default=0, help="抽样 N 份跨格式试点")
    ap.add_argument("--redo", default="", help="重判文件名以此前缀开头的条目（先剔除旧结果再评）")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("缺少 L2_API_KEY 环境变量（litellm 网关密钥）。"
                 "请先设置，例如：set L2_API_KEY=<your-key>")

    files = list(corpus_files())
    # 只保留可 naive 抽取的格式
    usable = []
    for name, path in files:
        try:
            ext, _ = sniff_format(str(path))
            if ext in (".pdf", ".docx", ".xlsx", ".xls", ".et", ".doc", ".wps", ".ofd"):
                usable.append((name, path))
        except Exception:
            pass
    if args.pilot:
        # 跨格式均匀抽样
        by_ext = {}
        for name, path in usable:
            ext, _ = sniff_format(str(path))
            by_ext.setdefault(ext, []).append((name, path))
        picked, i = [], 0
        exts = list(by_ext)
        while len(picked) < args.pilot and any(by_ext.values()):
            e = exts[i % len(exts)]
            if by_ext[e]:
                picked.append(by_ext[e].pop(0))
            i += 1
        usable = picked

    rows = []
    wins = {"md": 0, "naive": 0, "tie": 0, "inconsistent": 0, "error": 0}
    # 增量：跳过已评过的文件（读现有结果），避免重复消耗推理
    judged = set()
    old_rows = []
    if RESULTS.exists():
        with open(RESULTS, encoding="utf-8-sig") as fh:
            old_rows = list(csv.DictReader(fh))
        judged = {r["file"] for r in old_rows}
    if args.redo:
        judged = {f for f in judged if not f.startswith(args.redo)}
    for name, path in usable:
        if name in judged:
            continue
        stem = Path(name).stem
        md_path = OUT_DIR / (stem + ".md")
        if not md_path.exists():
            print("%-24s SKIP (no md, run run_bench first)" % name)
            continue
        md = md_path.read_text(encoding="utf-8", errors="replace")
        try:
            naive = naive_extract(path)
        except Exception as e:
            print("%-24s SKIP naive: %s" % (name, str(e)[:50]))
            continue
        t0 = time.time()
        try:
            res = blind_compare(md, naive)
        except Exception as e:
            res = {"winner": "error", "consistent": False,
                   "r1": {"winner": "error", "reason": str(e)[:60]},
                   "r2": {"winner": "error", "reason": ""}}
        dt = round(time.time() - t0, 1)
        wins[res["winner"]] = wins.get(res["winner"], 0) + 1
        rows.append([name, res["winner"], res["consistent"],
                     res["r1"]["winner"], res["r2"]["winner"],
                     res["r1"]["reason"], dt])
        print("%-24s -> %-12s (r1=%s r2=%s) %ss" %
              (name, res["winner"], res["r1"]["winner"], res["r2"]["winner"], dt))

    header = ["file", "winner", "consistent", "round1", "round2", "reason", "seconds"]
    if args.redo:
        keep = [r for r in old_rows if not r["file"].startswith(args.redo)]
        with open(RESULTS, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for r in keep:
                w.writerow([r.get(h, "") for h in header])
            w.writerows(rows)
    else:
        new_file = not RESULTS.exists()
        with open(RESULTS, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(header)
            w.writerows(rows)
    n = len(rows)
    print("\n=== L2 盲评汇总（%d 份）===" % n)
    for k in ("md", "naive", "tie", "inconsistent"):
        print("  %-12s %d" % (k, wins.get(k, 0)))
    print("结果：%s" % RESULTS)


if __name__ == "__main__":
    main()
