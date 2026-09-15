"""HTM/HTML → Markdown。编码自动检测 + BeautifulSoup + markdownify。

架构改进（v2 - Document Model）:
- 新增 convert_to_document() 返回 Document Model（pragmatic: wraps markdown output）
- 使用结构化错误 ConversionError
- convert() 保持原有实现不变
"""
import chardet
from bs4 import BeautifulSoup
from markdownify import markdownify as md_func
import re
from pathlib import Path
from . import BaseConverter
from .image_pipeline import ImagePipelineMixin

# Document Model 导入
from model.document import Document
from model.block import Paragraph as MdParagraph
from model.inline import Text
from render.markdown import document_to_markdown
from errors import ConversionError


# markdownify 在"标题或表格单元格"上下文（内部伪标签 _inline）里会把
# <img> 降级成它的 alt 文本；alt 为空时整幅图就凭空消失。Word 导出的 HTML
# 几乎都把图片包在单格表格 + <span> 里，实测会成批丢图（交付语料里一篇制度
# 的流程图就是这样消失的，只剩一个空表格）。默认白名单是空的，这里给出常见
# 包裹标签作为基线，并在解析时把本文档中实际出现的父标签动态补进来。
_KEEP_INLINE_IMAGES = ("table", "td", "th", "tr", "tbody", "span", "b", "strong",
                       "i", "em", "u", "font", "a", "sub", "sup", "p", "div",
                       "center", "h1", "h2", "h3", "h4", "h5", "h6")


def html_to_markdown(html: str, base_title: str = "") -> str:
    """将 HTML 字符串转换为 Markdown（供 EML 等复用）"""
    try:
        # Word 导出的 HTML 含两类条件注释标记：
        #   降级隐藏: <!--[if gte vml 1]> ... <![endif]-->   （整体是合法注释，勿动）
        #   降级显示: <![if !vml]> ... <![endif]>            （非注释，会被当正文）
        # 只清理后者；若误删 <![endif]--> 会破坏前者注释的闭合，
        # 使解析器把整篇文档吞进注释、图片全部丢失。
        html = re.sub(r'<!\[if[^\]]*\]>', '', html)
        html = re.sub(r'<!\[endif\]>', '', html)
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'link']):
            tag.decompose()
        title = ""
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
        body = soup.find('body') or soup
        keep = set(_KEEP_INLINE_IMAGES)
        for img in body.find_all('img'):
            parent = getattr(img, 'parent', None)
            if parent is not None and getattr(parent, 'name', None):
                keep.add(parent.name)
        md = md_func(str(body), heading_style='ATX', strip=['script', 'style'],
                     keep_inline_images_in=sorted(keep))
        # 兜底：清理解析后仍可能残留的条件标记文本
        md = re.sub(r'^\s*if !(vml|support\w*)\?\s*', '', md, flags=re.MULTILINE)
        md = re.sub(r'\s*endif\?\s*', '', md)
        # Word 常把插图套在单格表格里，转换后剩下一张"表头为空、单元格里只有
        # 一张图"的单列表——纯噪声，还原成裸图片引用
        md = re.sub(
            r'\|\s*\|\s*\n\|\s*:?-{2,}:?\s*\|\s*\n\|(\s*(?:!\[[^\]]*\]\([^)]*\)\s*)+)\|',
            lambda m: m.group(1).strip(), md)
        md = re.sub(r'\n{4,}', '\n\n', md).strip()
        if (title or base_title) and not md.startswith('# '):
            md = '# {}\n\n{}'.format(title or base_title, md)
        return md
    except Exception as e:
        raise ConversionError("Failed to convert HTML to markdown: {}".format(e))


class HTMConverter(ImagePipelineMixin, BaseConverter):
    """HTML → MD (自动检测GBK/UTF-8编码, bs4提取正文+标题)

    支持与 CHM 一致的图片管线：图片落盘（link）、base64 内联、缺图占位、
    以及 OCR 文字注入（OCR 结果由预处理阶段写入缓存，此处只读取）。
    """

    def __init__(self, image_mode: str = "none", image_max_mb: float = 5.0,
                 assets_root: str = "", ocr_cfg: dict = None, chm_root: str = ""):
        self._init_image_pipeline(image_mode=image_mode,
                                  image_max_mb=image_max_mb,
                                  assets_root=assets_root,
                                  ocr_cfg=ocr_cfg,
                                  chm_root=chm_root)

    def supported_extensions(self) -> list:
        return ['.htm', '.html']

    def _convert_with_images(self, file_path: str, out_rel: str,
                             stats: dict = None) -> str:
        with open(file_path, 'rb') as f:
            raw = f.read()
        enc = chardet.detect(raw).get('encoding', 'utf-8') or 'utf-8'
        md = html_to_markdown(raw.decode(enc, errors='replace'))
        if self.image_mode != "none":
            md = self._process_images(md, file_path, out_rel, stats)
        return md

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型

        Pragmatic approach: 复用 Markdown 输出（含图片/OCR 处理）并包装为单个 Paragraph。
        """
        md_output = self.convert(file_path, **kwargs)
        doc = Document()
        if md_output.strip():
            doc.add_block(MdParagraph(children=[Text(md_output)]))
        return doc

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串"""
        out_rel = Path(file_path).with_suffix(".md").name
        return self._convert_with_images(file_path, out_rel, kwargs.get("stats"))

    def convert_to_files(self, file_path: str, rel_path: str) -> list:
        """单文件输出；out_rel 决定图片落到文档同级目录"""
        out_rel = str(Path(rel_path).with_suffix(".md"))
        return [(out_rel, self._convert_with_images(file_path, out_rel))]

