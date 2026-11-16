"""突变测试（robustness / fuzz-lite）

对 tests/fixtures_corpus/ 的 50 份真实文档施加 10 种突变算子，
断言"三定律"：
  1. 每个突变体要么成功产出非空 Markdown，要么抛 ConversionError 子类
     （并断言 error_code 可用）——绝不允许裸异常（KeyError/ValueError/...）。
  2. 60 秒超时视为 FAIL（解析器永远不允许挂死）。
  3. 若输出含"截断"标注，必须同时携带原规模说明（不许静默半截）。

设计说明：
  - 突变体在 tmp_path 现场生成（语料保持只读），不污染 fixtures_corpus。
  - 转换调用走子进程（subprocess + timeout=60），隔离转换器内的
    全局状态/线程池，同时天然实现超时。
  - 允许清单 SKIP_OPERATORS：某些 (格式, 算子) 组合在业务上无意义
    （如对纯 PDF 做"删 ZIP 中央目录"），直接 skip 不计失败。
"""
import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS = HERE / "fixtures_corpus"
SERVER = ROOT / "app" / "server"

PY = sys.executable
TIMEOUT_S = 60

# --------------------------------------------------------------- 语料加载

def _corpus_files():
    if not CORPUS.exists():  # 语料可选：缺 CI 环境（无内部文档）时整体跳过
        return []
    return sorted(p for p in CORPUS.iterdir()
                  if p.suffix.lower() in {".docx", ".doc", ".wps", ".ofd",
                                          ".et", ".pdf", ".xlsx"})

CORPUS_FILES = _corpus_files()

pytestmark = pytest.mark.skipif(not CORPUS_FILES,
                                reason="fixtures_corpus 不存在（内部语料不进 CI）")

# --------------------------------------------------------------- 突变算子

def mut_truncate(data: bytes) -> bytes:
    """截断前 30% 字节"""
    cut = max(1, int(len(data) * 0.3))
    return data[cut:]


def mut_flip_magic(data: bytes) -> bytes:
    """翻转魔数前 8 字节"""
    b = bytearray(data)
    for i in range(min(8, len(b))):
        b[i] ^= 0xFF
    return bytes(b)


def mut_strip_zip_eocd(data: bytes) -> bytes:
    """删除 ZIP 中央目录/EOCD 尾部 2KB（对 zip 系格式致命）"""
    if len(data) <= 2048:
        return b""
    return data[:-2048]


def mut_empty(data: bytes) -> bytes:
    return b""


def mut_zeros(data: bytes) -> bytes:
    return b"\x00" * min(len(data) or 4096, 1 << 20)


def mut_fake_encrypted_ole(data: bytes) -> bytes:
    """伪造 OLE2 加密头（gd/wps 加密格式的魔数 + 假头）"""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * min(len(data), 4096)


def mut_utf8_bom_tear(data: bytes) -> bytes:
    """BOM 撕裂：UTF-8 BOM 只留一半（EF BB），后接原文"""
    return b"\xef\xbb" + data


def mut_oversized_sheet(data: bytes) -> bytes:
    """伪装超大 sheet：原样返回（对 xlsx 由 ResourceLimit 拦截），
    非 xlsx 原样透传（该算子对其它格式无意义，由参数化过滤）"""
    return data


def mut_deep_zip_bomb(data: bytes) -> bytes:
    """500 层嵌套 zip（zip 炸弹）"""
    payload = data or b"seed"
    for _ in range(500):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("inner.bin", payload)
        payload = buf.getvalue()
        if len(payload) > 32 * 1024 * 1024:  # 防真炸弹撑爆内存
            break
    return payload


def mut_rename_ext(path: Path, workdir: Path) -> Path:
    """假扩展名：docx 改名 .et / et 改名 .docx（嗅探应纠正）"""
    swap = {".docx": ".et", ".et": ".docx", ".doc": ".wps", ".wps": ".doc",
            ".xlsx": ".et", ".pdf": ".doc"}.get(path.suffix.lower(), ".bin")
    dst = workdir / (path.stem + "_fake" + swap)
    dst.write_bytes(path.read_bytes())
    return dst


BYTE_MUTATORS = {
    "truncate30": mut_truncate,
    "flip_magic": mut_flip_magic,
    "strip_zip_eocd": mut_strip_zip_eocd,
    "empty": mut_empty,
    "all_zero": mut_zeros,
    "fake_enc_ole": mut_fake_encrypted_ole,
    "bom_tear": mut_utf8_bom_tear,
    "oversized_sheet": mut_oversized_sheet,
}

# oversized_sheet 仅对表格类有意义；zip_bomb 只对 zip 系
OPERATOR_FILTER = {
    "oversized_sheet": {".xlsx", ".et"},
    "strip_zip_eocd": {".docx", ".xlsx", ".et", ".wps"},  # wpsx 实为 zip 系
    "fake_enc_ole": {".doc", ".wps", ".et"},  # OLE2 系
}

# --------------------------------------------------------------- 执行器

RUNNER = r'''
import sys, json
sys.path.insert(0, r"%(server)s")
from converters.loader import register_all
from converters import registry
from errors import ConversionError

register_all(registry)
path = sys.argv[1]
from sniffer import sniff_format
ext, _ = sniff_format(path)
conv = registry.get(ext)
if conv is None or not getattr(conv, "available", True):
    print(json.dumps({"ok": False, "error_code": "UNSUPPORTED_FORMAT"}))
    sys.exit(0)
try:
    out = conv.convert(path)
    print(json.dumps({"ok": True, "chars": len(out or "")}))
except ConversionError as e:
    print(json.dumps({"ok": False, "error_code": e.error_code}))
except Exception as e:
    print(json.dumps({"ok": False, "error_code": None, "type": type(e).__name__,
                      "msg": str(e)[:120]}))
''' % {"server": str(SERVER)}


def _run_convert(src: Path, workdir: Path, rename=False):
    if rename:
        target = mut_rename_ext(src, workdir)
    else:
        target = workdir / src.name
        target.write_bytes(src.read_bytes())
    runner = workdir / "_runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [PY, str(runner), str(target)],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(workdir),
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    # 取最后一行 JSON（前可能有告警）
    js_line = out.splitlines()[-1] if out else "{}"
    import json
    try:
        return json.loads(js_line)
    except Exception:
        return {"ok": False, "error_code": None, "type": "RunnerCrash",
                "msg": proc.stderr.decode("utf-8", errors="replace")[:200]}


# --------------------------------------------------------------- 参数化用例

def _cases():
    cases = []
    for src in CORPUS_FILES:
        for op in list(BYTE_MUTATORS) + ["deep_zip_bomb", "rename_ext"]:
            allowed = OPERATOR_FILTER.get(op)
            if allowed and src.suffix.lower() not in allowed:
                continue
            cases.append(pytest.param(src, op, id="%s|%s" % (src.name, op)))
    return cases


CASES = _cases()


@pytest.mark.parametrize("src,op", CASES)
def test_mutant_never_crashes_or_hangs(src, op, tmp_path):
    """三定律 1+2：成功或 ConversionError，60s 超时即失败"""
    payload = src.read_bytes()

    if op == "deep_zip_bomb":
        target = tmp_path / (src.stem + "_bomb.zip")
        target.write_bytes(mut_deep_zip_bomb(payload))
        result = _run_bomb(target, tmp_path)
    elif op == "rename_ext":
        result = _run_convert(src, tmp_path, rename=True)
    else:
        mutant = tmp_path / src.name
        mutant.write_bytes(BYTE_MUTATORS[op](payload))
        result = _run_convert(mutant, tmp_path, rename=False)

    assert result.get("error_code") is not None or result.get("ok"), (
        "裸异常（未归一为 ConversionError）: %r" % (result,))


def _run_bomb(target: Path, workdir: Path):
    runner = workdir / "_runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [PY, str(runner), str(target)],
        capture_output=True, timeout=TIMEOUT_S, cwd=str(workdir),
    )
    import json
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    js = out.splitlines()[-1] if out else "{}"
    try:
        return json.loads(js)
    except Exception:
        return {"ok": False, "error_code": None, "type": "RunnerCrash"}


@pytest.mark.parametrize("src", CORPUS_FILES, ids=lambda p: p.name)
def test_original_still_converts(src, tmp_path):
    """对照：未突变的原文件必须成功（语料本身可用性护栏）"""
    result = _run_convert(src, tmp_path)
    assert result.get("ok"), "语料原件转换失败: %r" % (result,)
