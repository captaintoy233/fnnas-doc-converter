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
from docx.table import Table
from docx.text.paragraph import Paragraph
from . import BaseConverter

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


def _is_list_paragraph(para) -> bool:
    """检测是否列表项（numPr 存在或样式为 List 系列）"""
    try:
        pPr = para._p.pPr
        if pPr is not None and pPr.numPr is not None:
            return True
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
        doc = DocxDoc(file_path)
        result = Document()
        
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
                    # 列表项 - 简化处理，作为带前缀的段落
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
