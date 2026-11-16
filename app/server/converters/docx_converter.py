"""DOCX → Markdown。用 python-docx 提取文本及表格。

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- 保留文档顺序、标题层级、列表、表格
- 支持中文样式名（标题 1-6、列表段落）

向后兼容:
- convert() 方法保持原有签名和返回类型
- 新增 convert_to_document() 返回 Document Model
"""
from docx import Document as DocxDoc
import re
import zipfile
from docx.table import Table
from docx.text.paragraph import Paragraph
from . import BaseConverter
from errors import MalformedDocumentError

# Document Model 导入
from model.document import Document
from model.block import Heading, Paragraph as MdParagraph, Table as MdTable, ThematicBreak
from model.inline import Text, Bold, Italic
from model.table import TableRow, TableCell
from model.list import ListBlock, ListItem, MarkerKind
from render.markdown import document_to_markdown

_HEADING_STYLES = {
    'Heading 1': 1, 'Heading 2': 2, 'Heading 3': 3, 'Heading 4': 4,
    'Heading 5': 5, 'Heading 6': 6,
    '标题 1': 1, '标题 2': 2, '标题 3': 3, '标题 4': 4,
    '标题 5': 5, '标题 6': 6,
}

# 文本「自带编号」的识别：中文层级编号文档（一、/（一）/第X条/1. 等）的段落
# 常带 numPr/List 样式，若再叠加 "- " 圆点会冗余且不一致（L2 盲评 docx-02/06/07
# 因此失分）。命中此正则的列表项按原生编号渲染，不再加 "- "。
_SELF_NUM_RE = re.compile(
    r"^(?:"
    r"[一二三四五六七八九十百千]+[、.．]"                 # 一、 二.
    r"|[（(][一二三四五六七八九十百千]+[）)]"              # （一） (一)
    r"|第[一二三四五六七八九十百千0-9]+[条章节款编部分]"    # 第一条 第二章
    r"|[0-9]+[、.．)）]"                                 # 1. 1、 1) 1）
    r"|[（(][0-9]+[）)]"                                 # （1） (1)
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]"                          # ① ②
    r")"
)

# Word「自动编号」列表的 numFmt（编号由 numbering.xml 生成、不在正文文本里）。
# 这类列表项再叠加 "- " 会得到「- 条 …」「- .企业…」之类的冗余/残缺标记
# （L2 盲评 docx-02 因此失分）。命中这些格式时按普通段落渲染，不加 "- "。
# 真正的无序列表 numFmt=bullet，不在此列，仍保留 "- "。
_NUMBERED_FMTS = {
    "decimal", "decimalZero", "chineseCounting", "chineseCountingThousand",
    "chineseLegal", "lowerLetter", "upperLetter", "lowerRoman", "upperRoman",
    "ordinal", "cardinalText", "ordinalText", "japaneseCounting",
    "japaneseLegal", "koreanCounting", "koreanLegal", "ideographTraditional",
    "ideographZodiac", "taiwaneseCounting",
}


def _numbering_formats(doc) -> dict:
    """解析 numbering.xml，建 (numId, ilvl) -> numFmt 映射；无编号部件返回 {}。"""
    from docx.oxml.ns import qn
    fmtmap = {}
    try:
        np = doc.part.numbering_part.element
    except Exception:
        return fmtmap
    absfmt = {}
    for an in np.findall(qn('w:abstractNum')):
        aid = an.get(qn('w:abstractNumId'))
        fmts = {}
        for lvl in an.findall(qn('w:lvl')):
            nf = lvl.find(qn('w:numFmt'))
            fmts[lvl.get(qn('w:ilvl'))] = nf.get(qn('w:val')) if nf is not None else None
        absfmt[aid] = fmts
    for num in np.findall(qn('w:num')):
        nid = num.get(qn('w:numId'))
        anref = num.find(qn('w:abstractNumId'))
        if anref is None:
            continue
        for ilvl, fmt in absfmt.get(anref.get(qn('w:val')), {}).items():
            fmtmap[(nid, ilvl)] = fmt
    return fmtmap


def _para_numfmt(item, fmtmap: dict):
    """取段落 numPr 对应的 numFmt；无 numPr 或查不到返回 None。"""
    try:
        pPr = item._p.pPr
        if pPr is None or pPr.numPr is None:
            return None
        numId = pPr.numPr.numId.val if pPr.numPr.numId is not None else None
        ilvl = pPr.numPr.ilvl.val if pPr.numPr.ilvl is not None else 0
        return fmtmap.get((str(numId), str(ilvl)))
    except Exception:
        return None


def _is_list_paragraph(para) -> bool:
    """检测是否列表项（numPr 存在或样式为 List 系列）。

    numId=0 是 Word「取消编号」标记：段落虽带 numPr 但实际不是列表项，
    若误判会叠加出「- 条 …」「- .企业…」之类残缺冗余标记（L2 docx-02）。
    """
    try:
        pPr = para._p.pPr
        if pPr is not None and pPr.numPr is not None:
            numId = pPr.numPr.numId
            if numId is not None:
                if numId.val == 0:
                    return False  # 取消编号 → 普通段落
                return True       # 有实际编号
    except Exception:
        pass
    style = para.style.name if para.style else ""
    return style.startswith("List") or style in ("List Paragraph", "列表段落")


def _iter_body_items(doc):
    """按文档顺序遍历 body 中的段落与表格"""
    from docx.oxml.ns import qn
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield Table(child, doc)


def _extract_inline_runs(para) -> list:
    """从段落提取行内格式（加粗、斜体等）"""
    from model.inline import Text, Bold, Italic
    
    inlines = []
    for run in para.runs:
        text = run.text
        if not text:
            continue
        
        # 构建行内元素
        inline = Text(text)
        if run.bold:
            inline = Bold([inline])
        if run.italic:
            inline = Italic([inline] if not run.bold else [Text(text)])
        
        inlines.append(inline)
    
    # 如果没有 runs（纯文本段落），回退到 para.text
    if not inlines and para.text.strip():
        inlines = [Text(para.text)]
    
    return inlines


def _table_to_model(table: Table) -> MdTable:
    """将 python-docx Table 转为 Document Model Table"""
    rows = []
    for ri, row in enumerate(table.rows):
        cells = []
        seen_texts = []
        
        for cell in row.cells:
            text = cell.text.strip()
            # 去除重复合并单元格导致的重复列
            if seen_texts and seen_texts[-1] == text and text != "":
                continue
            seen_texts.append(text)
            
            cells.append(TableCell(
                children=[Text(text)] if text else [],
                is_header=(ri == 0)
            ))
        
        if all(not c.text.strip() for c in cells):
            continue
        
        rows.append(TableRow(cells=cells, is_header=(ri == 0)))
    
    return MdTable(rows=rows)


class DOCXConverter(BaseConverter):
    """DOCX → MD (python-docx, 保留文档顺序/标题/列表/表格)"""

    def supported_extensions(self) -> list:
        return ['.docx']

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        try:
            doc = DocxDoc(file_path)
        except zipfile.BadZipFile as e:
            raise MalformedDocumentError(
                f"Cannot open docx (not a valid OOXML zip): {e}",
                file_path=file_path,
            ) from e
        except Exception as e:
            # python-docx 对空文件抛 PackageNotFoundError；损坏 zip 内容抛
            # BadZipFile/OSError（zip 内部条目截断时 seek 报 Errno 22）——
            # 全部归一为 MalformedDocumentError，不让裸异常漏出转换边界
            raise MalformedDocumentError(
                f"Cannot open docx: {type(e).__name__}: {e}",
                file_path=file_path,
            ) from e
        result = Document()
        fmtmap = _numbering_formats(doc)

        pending_empty = False  # 跟踪空段落，避免连续空行
        
        for item in _iter_body_items(doc):
            if isinstance(item, Paragraph):
                t = item.text.strip()
                
                if not t:
                    pending_empty = True
                    continue
                
                style = item.style.name if item.style else ""
                level = _HEADING_STYLES.get(style)
                
                if level:
                    # 标题
                    inlines = _extract_inline_runs(item)
                    if not inlines:
                        inlines = [Text(t)]
                    result.add_block(Heading(level=level, children=inlines))
                    pending_empty = False
                    
                elif _is_list_paragraph(item):
                    fmt = _para_numfmt(item, fmtmap)
                    if _SELF_NUM_RE.match(t) or fmt in _NUMBERED_FMTS:
                        # 文本已自带编号（一、/（一）/第X条/1. 等），或属 Word
                        # 自动编号列表（编号不在正文里）：按普通段落渲染、保留
                        # 行内格式，不叠加冗余 "- "。
                        inlines = _extract_inline_runs(item)
                        if not inlines:
                            inlines = [Text(t)]
                        result.add_block(MdParagraph(children=inlines))
                    else:
                        # 真正的无序列表项（numFmt=bullet 等）：带 "- " 前缀
                        result.add_block(MdParagraph(children=[Text("- " + t)]))
                    pending_empty = False
                    
                else:
                    # 普通段落
                    inlines = _extract_inline_runs(item)
                    if not inlines:
                        inlines = [Text(t)]
                    result.add_block(MdParagraph(children=inlines))
                    pending_empty = False
                    
            elif isinstance(item, Table):
                md_table = _table_to_model(item)
                if not md_table.is_empty():
                    result.add_block(md_table)
                    pending_empty = False
        
        return result

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
