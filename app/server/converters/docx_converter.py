"""DOCX → Markdown。用 python-docx 提取文本及表格。

改进点（相对旧版）:
- 按文档真实顺序遍历段落与表格（旧版把表格全部追加在段落之后，破坏顺序）
- 支持 Heading 1-6 级标题
- 支持有序/无序列表（numPr 检测）
- 表格单元格内容转义 `|` 与换行
"""
from docx import Document as DocxDoc
from docx.table import Table
from docx.text.paragraph import Paragraph
from . import BaseConverter

_HEADING_STYLES = {
    'Heading 1': 1, 'Heading 2': 2, 'Heading 3': 3, 'Heading 4': 4,
    'Heading 5': 5, 'Heading 6': 6,
    '标题 1': 1, '标题 2': 2, '标题 3': 3, '标题 4': 4,
    '标题 5': 5, '标题 6': 6,
}


def _escape_cell(text: str) -> str:
    """表格单元格文本转义：`|` 转义、换行转 <br>"""
    text = text.replace('\\', '\\\\').replace('|', '\\|')
    text = text.replace('\r\n', '<br>').replace('\n', '<br>')
    return text.strip()


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


def _table_to_md(table: Table) -> str:
    lines = []
    for ri, row in enumerate(table.rows):
        cells = [_escape_cell(c.text) for c in row.cells]
        # 去除重复合并单元格导致的重复列
        seen = []
        for c in cells:
            if not seen or seen[-1] != c or c != "":
                seen.append(c)
        if all(c == '' for c in seen):
            continue
        lines.append('| ' + ' | '.join(seen) + ' |')
        if ri == 0:
            lines.append('| ' + ' | '.join(['---'] * len(seen)) + ' |')
    return '\n'.join(lines) if lines else ""


class DOCXConverter(BaseConverter):
    """DOCX → MD (python-docx, 保留文档顺序/标题/列表/表格)"""

    def supported_extensions(self) -> list:
        return ['.docx']

    def convert(self, file_path: str, **kwargs) -> str:
        doc = DocxDoc(file_path)
        blocks = []
        pending_para = False  # 段落缓冲，避免连续空行

        def flush_para():
            nonlocal pending_para
            if pending_para:
                blocks.append("")
                pending_para = False

        for item in _iter_body_items(doc):
            if isinstance(item, Paragraph):
                t = item.text.strip()
                if not t:
                    flush_para()
                    continue
                style = item.style.name if item.style else ""
                level = _HEADING_STYLES.get(style)
                if level:
                    flush_para()
                    blocks.append('\n' + '#' * level + ' ' + t + '\n')
                    continue
                if _is_list_paragraph(item):
                    blocks.append('- ' + t)
                    pending_para = False
                    continue
                blocks.append(t)
                pending_para = False
            elif isinstance(item, Table):
                md = _table_to_md(item)
                if md:
                    flush_para()
                    blocks.append('\n' + md + '\n')

        out = '\n'.join(blocks)
        # 合并 3 个以上连续空行
        import re
        out = re.sub(r'\n{4,}', '\n\n\n', out)
        return out.strip()
