"""
CHM (Compiled HTML Help) → Markdown 转换器

CHM 为 ITS 格式，无法纯 Python 解析正文压缩流，依赖 7-Zip (7zz) 解压。
流程（与生产环境 v3 一致）:
1. `7zz x -y -o<temp> <chm>` 一次性解压（逐文件 stdout 提取太慢）
2. 解析 .hhc 目录树，按顶层章节分组
3. 章节内 HTML 文件并行转 Markdown 并合并
4. 输出: 每个顶层章节一个 .md（convert_to_files）；API 单文件转换返回全文合并

7zz 路径可通过配置 chm.seven_zip_path 指定（默认 "7zz"）。
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bs4 import BeautifulSoup

from . import BaseConverter
from .htm_converter import HTMConverter
from extract import make_temp

logger = logging.getLogger("docconverter.chm")

# lxml 对未闭合/紧凑 HTML（CHM 常见）的嵌套解析更可靠
try:
    import lxml  # noqa: F401
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False


class CHMConverter(BaseConverter):
    """CHM → MD (7zz 解压 + .hhc 章节分组 + HTML→MD)"""

    def __init__(self, seven_zip_path: str = "7zz", workers: int = 4):
        self.seven_zip_path = seven_zip_path
        self.workers = workers

    def supported_extensions(self) -> list:
        return ['.chm']

    @property
    def display_name(self) -> str:
        return "CHM"

    def _find_7zz(self) -> str:
        """定位 7zz 可执行文件"""
        cfg_path = self.seven_zip_path
        if cfg_path and cfg_path != "7zz":
            if Path(cfg_path).exists():
                return cfg_path
        found = shutil.which(cfg_path or "7zz") or shutil.which("7z")
        if found:
            return found
        raise RuntimeError(
            "未找到 7zz/7z 可执行文件，无法解析 CHM。\n"
            "请安装 7-Zip（如 GitHub ip7z/7zip 的 7zz）并设置配置 "
            "chm.seven_zip_path 指向其路径。"
        )

    def extract(self, file_path: str, dest: str) -> list:
        """解压 CHM 到 dest，返回解压出的文件列表（复用 extract.extract_7z）"""
        from extract import extract_7z, make_temp
        seven_zip = self._find_7zz()
        return extract_7z(Path(file_path), Path(dest), seven_zip)

    def _find_hhc(self, files: list) -> str:
        """找到 .hhc 目录文件（优先根目录/小写）"""
        hhcs = [Path(f) for f in files if Path(f).suffix.lower() == ".hhc"]
        if not hhcs:
            return ""
        hhcs.sort(key=lambda p: (len(p.parts), str(p).lower()))
        return str(hhcs[0])

    def parse_toc(self, hhc_path: str) -> list:
        """解析 .hhc 目录树，返回顶层章节列表

        Returns:
            [ { "name": str, "files": [abs_path, ...] }, ... ]
        """
        with open(hhc_path, "rb") as f:
            raw = f.read()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("gb18030", errors="replace")
        soup = BeautifulSoup(html, "lxml" if _HAS_LXML else "html.parser")
        base = Path(hhc_path).parent

        def extract_li(li) -> dict:
            """递归提取一个 <li> 的章节名与文件"""
            name = ""
            local = ""
            for obj in li.find_all("object", recursive=False):
                params = {}
                for p in obj.find_all("param"):
                    n = (p.get("name") or "").lower()
                    v = p.get("value") or ""
                    params[n] = v
                if not name and params.get("name"):
                    name = params["name"]
                if not local and params.get("local"):
                    local = params["local"]
            files = []
            if local:
                fpath = (base / local).resolve()
                try:
                    if fpath.exists():
                        files.append(str(fpath))
                except OSError:
                    # 超长文件名路径（ENAMETOOLONG）
                    pass
            children = []
            # 子章节位于 <li> 内嵌的 <ul> 下
            ul = li.find("ul")
            if ul is not None:
                for sub_li in ul.find_all("li", recursive=False):
                    sub = extract_li(sub_li)
                    if sub["name"] or sub["files"] or sub["children"]:
                        children.append(sub)
            return {"name": name, "files": files, "children": children}

        chapters = []
        root_ul = soup.find("ul")
        if root_ul is not None:
            for li in root_ul.find_all("li", recursive=False):
                ch = extract_li(li)
                if ch["name"] or ch["files"] or ch["children"]:
                    chapters.append(ch)
        return chapters

    def _flatten(self, node: dict) -> list:
        """章节树展平为文件列表（保持目录顺序）"""
        files = list(node.get("files", []))
        for child in node.get("children", []):
            files.extend(self._flatten(child))
        return files

    def _html_to_md(self, file_path: str) -> str:
        try:
            # 超大页面（>1.5MB）：跳过 markdownify，用快速文本提取
            if Path(file_path).stat().st_size > 1.5 * 1024 * 1024:
                import chardet
                import re
                with open(file_path, "rb") as f:
                    raw = f.read()
                enc = chardet.detect(raw).get("encoding", "utf-8") or "utf-8"
                soup = BeautifulSoup(raw.decode(enc, errors="replace"), "html.parser")
                for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                    tag.decompose()
                title_tag = soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else ""
                text = soup.get_text("\n")
                text = re.sub(r"[ \t]+\n", "\n", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if title:
                    text = "# {}\n\n{}".format(title, text)
                return text
            return HTMConverter().convert(file_path)
        except Exception as e:
            logger.warning("HTML→MD 失败 %s: %s", file_path, e)
            return ""

    def convert_chapter(self, name: str, files: list) -> str:
        """将一章内所有 HTML 并行转换为合并的 Markdown"""
        if not files:
            return ""
        parts = []
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            results = list(pool.map(self._html_to_md, files))
        for md in results:
            md = md.strip()
            if md:
                parts.append(md)
        if not parts:
            return ""
        if name:
            return "# {}\n\n{}".format(name, "\n\n---\n\n".join(parts))
        return "\n\n---\n\n".join(parts)

    def _extract_all(self, file_path: str, tmp_root: str) -> str:
        """解压并返回文件列表（内部使用独立临时目录）"""
        work = make_temp("chm_")
        return work

    def to_chapters(self, file_path: str) -> list:
        """解压 CHM 并返回章节列表 [(chapter_name, chapter_files)]"""
        work = make_temp("chm_")
        try:
            files = self.extract(file_path, work)
            hhc = self._find_hhc(files)
            if not hhc:
                # 无目录文件：全部文件作为一章
                md_files = [str(f) for f in files
                            if f.suffix.lower() in (".htm", ".html")]
                return [("", md_files)], work
            toc = self.parse_toc(hhc)
            chapters = []
            seen = set()
            for ch in toc:
                ch_files = self._flatten(ch)
                ch_files = [f for f in ch_files if f not in seen and
                            Path(f).suffix.lower() in (".htm", ".html")]
                for f in ch_files:
                    seen.add(f)
                chapters.append((ch["name"], ch_files))
            # 目录外未覆盖的 html 文件补到最后一章
            leftover = []
            for f in files:
                if Path(f).suffix.lower() in (".htm", ".html") and str(f) not in seen:
                    leftover.append(str(f))
            if leftover:
                chapters.append(("其他", leftover))
            return chapters, work
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

    def convert(self, file_path: str, **kwargs) -> str:
        """API 单文件转换：返回全文合并的 Markdown"""
        chapters, work = self.to_chapters(file_path)
        try:
            parts = []
            for name, ch_files in chapters:
                md = self.convert_chapter(name, ch_files)
                if md:
                    parts.append(md)
            title = Path(file_path).stem
            if parts:
                return "# {}\n\n{}".format(title, "\n\n---\n\n".join(parts))
            return "# {}\n\n（CHM 中未找到可转换的 HTML 内容）".format(title)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def convert_to_files(self, file_path: str, rel_path: str) -> list:
        """批量输出：每个顶层章节一个 .md 文件，保留源文件名目录"""
        chapters, work = self.to_chapters(file_path)
        try:
            stem = Path(rel_path).stem
            outputs = []
            if not chapters:
                return [(str(Path(rel_path).with_suffix(".md")),
                         "# {}\n\n（CHM 中未找到可转换的 HTML 内容）".format(stem))]
            for i, (name, ch_files) in enumerate(chapters, 1):
                md = self.convert_chapter(name, ch_files)
                if not md:
                    continue
                chapter_name = name or "章节{}".format(i)
                # 清理文件名非法字符
                safe = re.sub(r'[\\/:*?"<>|]+', "_", chapter_name).strip() or "章节{}".format(i)
                out_rel = "{}/{}.md".format(stem, safe[:80])
                outputs.append((out_rel, md))
            if not outputs:
                return [(str(Path(rel_path).with_suffix(".md")),
                         "# {}\n\n（CHM 中未找到可转换的 HTML 内容）".format(stem))]
            return outputs
        finally:
            shutil.rmtree(work, ignore_errors=True)
