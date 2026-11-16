"""bench 语料冻结清单：与 tests/fixtures_corpus/MANIFEST.csv 同源。

corpus_files() 返回 [(name, Path), ...]，供 run_bench.py 使用。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORPUS_DIR = ROOT / "tests" / "fixtures_corpus"

VALID_EXTS = {".docx", ".doc", ".wps", ".ofd", ".et", ".pdf", ".xlsx"}


def corpus_files():
    """返回排序后的 [(file_name, absolute_path)]。语料缺失时返回空表。"""
    if not CORPUS_DIR.exists():
        return []
    return sorted((p.name, p) for p in CORPUS_DIR.iterdir()
                  if p.suffix.lower() in VALID_EXTS)


if __name__ == "__main__":
    files = corpus_files()
    print("corpus size:", len(files))
    for name, _ in files[:5]:
        print(" ", name)
