"""DOCX → Markdown。用 python-docx 提取文本及表格。"""
from docx import Document as DocxDoc
from . import BaseConverter


class DOCXConverter(BaseConverter):
    """DOCX → MD (python-docx, 保留标题/章节/表格)"""

    def supported_extensions(self) -> list:
        return ['.docx']

    def convert(self, file_path: str, **kwargs) -> str:
        doc = DocxDoc(file_path)
        lines = []
        for para in doc.paragraphs:
            t = para.text.strip()
            if not t:
                continue
            s = para.style.name if para.style else ""
            if 'Heading 1' in s:
                lines.append(f'\n# {t}\n')
            elif 'Heading 2' in s:
                lines.append(f'\n## {t}\n')
            elif 'Heading 3' in s:
                lines.append(f'\n### {t}\n')
            else:
                lines.append(t)
        for i, tbl in enumerate(doc.tables):
            lines.append(f'\n**表格 {i+1}**\n')
            for ri, row in enumerate(tbl.rows):
                cells = [c.text.strip() for c in row.cells]
                lines.append('| ' + ' | '.join(cells) + ' |')
                if ri == 0:
                    lines.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
        return '\n'.join(lines)
