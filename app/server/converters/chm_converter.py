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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from . import BaseConverter
from .htm_converter import HTMConverter
from .image_pipeline import (
    ImagePipelineMixin, _safe_component, _norm_ref, _IMG_EXT, _MIME,
)
from extract import make_temp

logger = logging.getLogger("docconverter.chm")

# lxml 对未闭合/紧凑 HTML（CHM 常见）的嵌套解析更可靠
try:
    import lxml  # noqa: F401
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False


# ============================================================================
# 目录树镜像输出（v4）
#
# 背景：同一本 CHM 手册会持续更新（新增文件、旧文件转为废止），转换结果
# 需要作为 WeKnora 个人知识库的数据源，因此输出必须满足：
#   1. 按 .hhc 索引层级建立同名文件夹，文档落在其索引位置
#   2. 每个文档携带元数据（索引路径 / 状态 / 源文件 / 内容哈希）
#   3. 状态可区分"现行有效"与"已废止"，避免废旧制度被当作现行依据
#   4. 输出可增量比对（新增/变更/删除），供推送管线决定 push/delete
# ============================================================================

# 废止判定标记：仅用于匹配"祖先目录名"，不匹配文档标题，
# 以免把《关于废止部分司法解释的决定》这类现行文件误判为废止。
_DEPRECATED_DIR_MARKS = ("已废止", "废止", "作废")

# Word/Excel 导出 HTML 时产生的"空壳辅助页"。
# 注意：只列入确认无内容的骨架文件——`sheet0XX.htm` / `file0XXX.htm` 这类
# 实测含真实表格内容（如"附表2-1：..."），**不可**按模式一概过滤。
_SCAFFOLD_NAMES = {
    "header.htm", "header.html", "footer.htm", "footer.html",
    "tabstrip.htm", "tabstrip.html", "tabscript.htm", "tabscript.html",
    "filelist.xml", "stylesheet.css", "oledata.mso", "editdata.mso",
    "header", "footer", "tabstrip", "tabscript",
}


def _is_scaffold_doc(source_files: list) -> bool:
    """判断文档是否全部由空壳辅助页构成（全为骨架才跳过）"""
    if not source_files:
        return False
    for f in source_files:
        if os.path.basename(f).lower() not in _SCAFFOLD_NAMES:
            return False
    return True


# ----------------------------------------------------------------------
# 渲染并行：大文档集必须走**多进程**
#
# HTML→MD（BeautifulSoup + markdownify）是纯 Python 的 CPU 密集任务，
# 线程受 GIL 限制无法并行。实测 4471 篇：ThreadPoolExecutor 约 25 分钟
# （仅 1 核），ProcessPoolExecutor 约 127 秒。
# 小批量仍用线程，避免进程启动开销反而不划算。
# ----------------------------------------------------------------------

_MP_CTX = {}


def _mp_render_init(conv_kwargs: dict, chm_root: str, chm_name: str,
                    link_map: dict):
    """子进程初始化：各自构建一个轻量转换器实例"""
    conv = CHMConverter(workers=1, **conv_kwargs)
    conv.chm_root = chm_root
    _MP_CTX["conv"] = conv
    _MP_CTX["chm_name"] = chm_name
    _MP_CTX["link_map"] = link_map


def _mp_render_one(doc: dict):
    """子进程渲染单篇；异常不中断整批"""
    try:
        md = _MP_CTX["conv"].render_document(
            doc, _MP_CTX["chm_name"], _MP_CTX["link_map"])
        return doc["rel_path"], md, None
    except Exception as e:  # noqa: BLE001
        return doc["rel_path"], "", "{}: {}".format(type(e).__name__, e)[:200]



def _sha1_file(path: str) -> str:
    """文件内容 SHA-1（用于增量比对）"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _front_matter(meta: dict) -> str:
    """生成 YAML front-matter（用 JSON 字符串转义，值均为合法 YAML 标量）"""
    lines = ["---"]
    for k, v in meta.items():
        lines.append("{}: {}".format(k, json.dumps(v, ensure_ascii=False)))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ----------------------------------------------------------------------
# 图片引用处理
#
# CHM 内的图片引用有三种形态（同一本手册可能混用）：
#   1. 相对正斜杠   image001.png / xxx.files/image001.png
#   2. 相对反斜杠   .\xxx.files\file0001.png      （Linux 下需归一化）
#   3. Windows 绝对 d:\wxw\河南信贷手册\xxx.files\file0001.jpg
# 第 3 类需按 CHM 根目录名剥离前缀后再定位；部分图片并未打包进 CHM，
# 这类引用无法修复，保持原样并计数上报。
# ----------------------------------------------------------------------

_IMG_EXT = re.compile(r'\.(png|jpe?g|gif|bmp|tiff?|emf|wmf)$', re.I)

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".emf": "image/emf", ".wmf": "image/wmf",
}


def _norm_ref(ref: str) -> str:
    t = ref.split("#")[0].split("?")[0].replace("\\", "/")
    try:
        from urllib.parse import unquote
        return unquote(t)
    except Exception:
        return t


class CHMConverter(ImagePipelineMixin, BaseConverter):
    """CHM → MD (7zz 解压 + .hhc 章节分组 + HTML→MD)"""

    def __init__(self, seven_zip_path: str = "7zz", workers: int = 4,
                 layout: str = "tree", front_matter: bool = True,
                 image_mode: str = "none", image_max_mb: float = 5.0,
                 assets_root: str = "", ocr_cfg: dict = None,
                 namespace_output: bool = True,
                 skip_scaffold: bool = True,
                 render_processes: bool = True,
                 mp_threshold: int = 50):
        """
        Args:
            seven_zip_path: 7zz/7z 可执行文件路径
            workers:        HTML→MD 并行度
            layout:         "tree"    = 按索引层级建文件夹、每文档一个 MD（默认）
                            "chapter" = 旧行为：每个顶层章节合并为一个 MD
            front_matter:   是否在 MD 头部写入 YAML 元数据（供 WeKnora 摄入）
            image_mode:     图片处理方式
                            "none"   = 保留原始引用（默认，不落图片）
                            "link"   = 复制图片到文档同级 .files/ 并改写为相对链接
                            "base64" = 以 data URI 内联嵌入 MD（单文件自包含）
            image_max_mb:   base64 模式下超过该大小的图片不内联（避免单文件过大）
            assets_root:    输出根目录；link/base64 模式需要（用于落盘/统计）
            namespace_output: 是否以 CHM 文件名建一层命名空间目录（批处理共用输出根目录时应为 True）
        """
        self.seven_zip_path = seven_zip_path
        self.workers = workers
        self.layout = (layout or "tree").lower()
        self.front_matter = front_matter
        self.namespace_output = bool(namespace_output)
        self.skip_scaffold = bool(skip_scaffold)
        self.skipped_scaffold = 0
        # 大文档集用多进程绕开 GIL；阈值以下用线程（省去进程启动开销）
        self.render_processes = bool(render_processes)
        self.mp_threshold = int(mp_threshold)
        self._init_image_pipeline(image_mode=image_mode,
                                  image_max_mb=image_max_mb,
                                  assets_root=assets_root,
                                  ocr_cfg=ocr_cfg,
                                  chm_root="")

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
        # 常见本地安装位置（无 root 权限时的用户级安装）
        for cand in (
            Path.home() / ".local" / "bin" / "7zz",
            Path.home() / "bin" / "7zz",
            Path("/usr/local/bin/7zz"),
            Path("/opt/7zz/7zz"),
        ):
            if cand.is_file() and os.access(str(cand), os.X_OK):
                return str(cand)
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
                # 不能用 soup.get_text()：那会把插图全部丢掉（最大的一篇 22MB
                # 里有 13 张图）。按文档顺序遍历，遇到 <img> 就插入引用，
                # 这样图片位置相对周围文字仍然保持。
                chunks = []
                for node in soup.descendants:
                    name = getattr(node, "name", None)
                    if name == "img":
                        src = (node.get("src") or "").strip()
                        if src:
                            chunks.append("\n\n![]({})\n\n".format(src))
                    elif name is None:
                        t = str(node)
                        if t.strip():
                            chunks.append(t)
                text = "\n".join(chunks)
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

    # ------------------------------------------------------------------
    # 目录树镜像输出
    # ------------------------------------------------------------------

    def to_toc_documents(self, file_path: str, work: str = None) -> tuple:
        """解压 CHM 并按 TOC 展开为文档清单

        Returns:
            (docs, work_dir)  docs: [{
                rel_path:   相对输出路径（含层级文件夹），如 "01.外部规章/人民银行/xx.md"
                title:      索引入口名（原始节点名）
                toc_path:   在索引中的层级路径
                status:     "现行有效" | "已废止"
                source_files: 源 HTML 绝对路径列表
                source_hash:  源内容 SHA-1（多文件时按序拼接）
            }, ...]
        """
        work = work or make_temp("chm_")
        try:
            files = self.extract(file_path, work)
            hhc = self._find_hhc(files)

            # 无 .hhc：退化为平铺（按物理目录结构）
            if not hhc:
                docs = []
                used = {}
                for f in sorted(files, key=lambda p: str(p).lower()):
                    if Path(f).suffix.lower() not in (".htm", ".html"):
                        continue
                    rel = os.path.relpath(str(f), work)
                    comp = _safe_component(Path(rel).stem)
                    self._emit_doc(docs, used, [], comp, [], [str(f)], "现行有效")
                return docs, work

            toc = self.parse_toc(hhc)
            docs = []
            used = {}   # tuple(dirs) -> {name: count}

            def emit(node: dict, dirs: list, ancestors: list):
                name = node.get("name") or ""
                has_children = bool(node.get("children"))
                has_files = bool(node.get("files"))
                status = "已废止" if any(
                    m in a for a in ancestors for m in _DEPRECATED_DIR_MARKS
                ) else "现行有效"

                if has_children:
                    comp = self._unique(used, dirs, _safe_component(name))
                    sub_dirs = dirs + [comp]
                    # 分组自身也挂文档时，放入该分组目录内
                    if has_files:
                        self._emit_doc(docs, used, sub_dirs, comp,
                                       node["files"], ancestors + [name], status)
                    for child in node["children"]:
                        emit(child, sub_dirs, ancestors + [name])
                elif has_files:
                    comp = self._unique(used, dirs, _safe_component(name))
                    self._emit_doc(docs, used, dirs, comp,
                                   node["files"], ancestors + [name], status)

            for node in toc:
                emit(node, [], [])

            # 目录未覆盖的 HTML（孤儿页）逐篇归入 _未编目，避免合并成巨型文档
            # 注意：被跳过的空壳辅助页也会"未被覆盖"，此处必须一并排除，
            # 否则它们会换个名字从 _未编目 再进来。
            covered = {f for d in docs for f in d["source_files"]}
            orphans = [
                str(f) for f in files
                if Path(f).suffix.lower() in (".htm", ".html")
                and str(f) not in covered
                and not (getattr(self, "skip_scaffold", True)
                         and _is_scaffold_doc([str(f)]))
            ]
            if orphans:
                orphans.sort()
                for f in orphans:
                    stem = Path(f).stem or "未命名"
                    comp = self._unique(used, ["_未编目"], _safe_component(stem))
                    self._emit_doc(docs, used, ["_未编目"], comp,
                                   [f], ["_未编目", stem], "现行有效")
            return docs, work
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise

    @staticmethod
    def _unique(used: dict, dirs: list, comp: str) -> str:
        """同一目录内去重：重名时追加 _2/_3 …"""
        key = tuple(dirs)
        bucket = used.setdefault(key, {})
        low = comp.lower()
        if low not in bucket:
            bucket[low] = 1
            return comp
        bucket[low] += 1
        return "{}_{}".format(comp, bucket[low])

    def _emit_doc(self, docs: list, used: dict, dirs: list, comp: str,
                  source_files: list, ancestors: list, status: str):
        """登记一个文档条目（超出过滤条件的空壳辅助页在此跳过）"""
        if getattr(self, "skip_scaffold", True) and _is_scaffold_doc(source_files):
            self.skipped_scaffold = getattr(self, "skipped_scaffold", 0) + 1
            return
        rel = "/".join(dirs + [comp + ".md"])
        h = hashlib.sha1()
        for f in source_files:
            try:
                h.update(_sha1_file(f).encode("ascii"))
            except OSError:
                h.update(b"missing")
        docs.append({
            "rel_path": rel,
            "title": ancestors[-1] if ancestors else comp,
            "toc_path": "/".join(ancestors),
            "status": status,
            "source_files": source_files,
            "source_hash": h.hexdigest(),
        })

    @staticmethod
    def build_link_map(docs: list) -> dict:
        """构建 源HTML绝对路径 → 输出MD相对路径 的映射（用于内部链接重写）"""
        m = {}
        for d in docs:
            for f in d["source_files"]:
                try:
                    m[os.path.normpath(os.path.abspath(f))] = d["rel_path"]
                except OSError:
                    continue
        return m

    @staticmethod
    def _rewrite_links(md: str, source_file: str, out_rel: str, link_map: dict) -> str:
        """把指向 CHM 内部 .htm/.html 的链接改写为对应 .md 相对路径"""
        if not md or not link_map:
            return md
        from urllib.parse import quote, unquote

        src_dir = os.path.dirname(os.path.abspath(source_file))
        out_dir = os.path.dirname(out_rel)

        def repl(match):
            target = match.group(1)
            if not target or "://" in target or target.startswith(("mailto:", "#", "data:")):
                return match.group(0)
            frag = ""
            if "#" in target:
                target, frag = target.split("#", 1)
                frag = "#" + frag
            low = target.lower()
            if not low.endswith((".htm", ".html")):
                return match.group(0)
            try:
                cand = os.path.normpath(
                    os.path.join(src_dir, unquote(target).replace("/", os.sep))
                )
            except (OSError, ValueError):
                return match.group(0)
            dst = link_map.get(cand)
            if not dst:
                return match.group(0)
            rel = os.path.relpath(dst, out_dir).replace(os.sep, "/")
            return "]({}{})".format(quote(rel), frag)

        return re.sub(r"\]\(([^)\s]+)\)", repl, md)

    def render_document(self, doc: dict, chm_name: str = "",
                        link_map: dict = None, img_stats: dict = None) -> str:
        """将文档条目渲染为带元数据的 Markdown

        img_stats: 可选，累计图片处理统计（found/missing/linked/inlined/
                   copied/oversize/bytes），供调用方汇总上报。
        """
        parts = []
        for f in doc["source_files"]:
            md = self._html_to_md(f)
            if md and md.strip():
                if self.image_mode != "none":
                    md = self._process_images(md, f, doc["rel_path"], img_stats)
                if link_map:
                    md = self._rewrite_links(md, f, doc["rel_path"], link_map)
                parts.append(md.strip())
        body = "\n\n---\n\n".join(parts)

        # 标题缺失时补一个，保证 WeKnora 有可用标题
        if body and not body.lstrip().startswith("# "):
            body = "# {}\n\n{}".format(doc["title"], body)
        if not body:
            body = "（本条目对应的 HTML 内容为空或无法解析）"

        if doc["status"] == "已废止":
            body = ("> ⚠️ **本文档已废止**，仅为历史留存，请勿作为现行依据。\n\n"
                    + body)

        if not self.front_matter:
            return body

        meta = {
            "title": doc["title"],
            "status": doc["status"],
            "toc_path": doc["toc_path"],
            "source_chm": chm_name or "",
            "source_files": [os.path.basename(f) for f in doc["source_files"]],
            "content_hash": doc["source_hash"],
        }
        return _front_matter(meta) + body

    def convert_tree_to_files(self, file_path: str, rel_path: str,
                              chm_name: str = "", docs: list = None,
                              work: str = None) -> list:
        """按索引层级输出多个 MD（保留并发转换）

        Args:
            docs/work: 可传入 to_toc_documents() 的已解压结果以复用临时目录
        Returns:
            [(rel_output_path, content), ...]

        命名空间：批处理管线里多个源文件共用同一个输出根目录，若直接输出
        `01.外部规章/...`，第二本 CHM 会与第一本混进同一棵树。故默认以
        CHM 文件名建一层命名空间目录（`<CHM名>/01.外部规章/...`）；
        独立导出脚本的输出根目录本就是专用的，可用 namespace_output=False 关闭。
        """
        own = docs is None
        if own:
            docs, work = self.to_toc_documents(file_path, work)
        try:
            if not docs:
                stem = Path(rel_path).stem
                return [(str(Path(rel_path).with_suffix(".md")),
                         "# {}\n\n（CHM 中未找到可转换的 HTML 内容）".format(stem))]

            # 统一加前缀后再渲染：图片落盘位置由 out_rel 推导，
            # 必须与 MD 的最终路径保持一致，否则图片会落到前缀之外
            prefix = ""
            if getattr(self, "namespace_output", True):
                stem = Path(rel_path).stem or Path(file_path).stem
                if stem:
                    prefix = _safe_component(stem, max_bytes=120) + "/"
            if prefix:
                out_docs = []
                for d in docs:
                    e = dict(d)
                    e["rel_path"] = prefix + d["rel_path"]
                    out_docs.append(e)
            else:
                out_docs = docs

            link_map = self.build_link_map(out_docs)

            # 大文档集走多进程（绕开 GIL）；小批量用线程避免进程启动开销
            if self.render_processes and len(out_docs) >= self.mp_threshold:
                conv_kwargs = dict(
                    layout=self.layout,
                    front_matter=self.front_matter,
                    image_mode=self.image_mode,
                    image_max_mb=self.image_max_mb,
                    assets_root=self.assets_root,
                    ocr_cfg=self.ocr_cfg,
                    namespace_output=self.namespace_output,
                    skip_scaffold=self.skip_scaffold,
                )
                outputs = []
                with ProcessPoolExecutor(
                    max_workers=max(1, self.workers),
                    initializer=_mp_render_init,
                    initargs=(conv_kwargs, self.chm_root, chm_name, link_map),
                ) as pool:
                    for rel, content, err in pool.map(
                            _mp_render_one, out_docs, chunksize=8):
                        if err:
                            logger.warning("CHM 渲染失败 %s: %s", rel, err)
                        if content and content.strip():
                            outputs.append((rel, content))
                return outputs

            with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
                contents = list(pool.map(
                    lambda d: self.render_document(d, chm_name, link_map), out_docs
                ))

            outputs = []
            for d, content in zip(out_docs, contents):
                if content and content.strip():
                    outputs.append((d["rel_path"], content))
            return outputs
        finally:
            if own and work:
                shutil.rmtree(work, ignore_errors=True)

    def convert_chapters_to_files(self, file_path: str, rel_path: str) -> list:
        """旧行为：每个顶层章节合并为一个 .md（保留兼容）"""
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
                safe = _safe_component(chapter_name)[:80]
                out_rel = "{}/{}.md".format(stem, safe)
                outputs.append((out_rel, md))
            if not outputs:
                return [(str(Path(rel_path).with_suffix(".md")),
                         "# {}\n\n（CHM 中未找到可转换的 HTML 内容）".format(stem))]
            return outputs
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
        """批量输出（按 self.layout 分发）

        - layout="tree"（默认）: 按 .hhc 索引层级建文件夹，每个文档一个 .md
        - layout="chapter":      每个顶层章节合并为一个 .md（旧行为）
        """
        if self.layout == "chapter":
            return self.convert_chapters_to_files(file_path, rel_path)
        return self.convert_tree_to_files(
            file_path, rel_path, chm_name=Path(file_path).name
        )

