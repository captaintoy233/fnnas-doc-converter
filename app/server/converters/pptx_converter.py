"""PPTX → Markdown。python-pptx 提取幻灯片文本。

改进点: 支持表格、组合图形（递归）、演讲者备注。
"""
from pptx import Presentation
from . import BaseConverter


def _shape_text(shape, texts: list):
    """递归提取形状文本（含组合、表格、文本框）"""
    if shape.shape_type == 6:  # GROUP
        for sub in shape.shapes:
            _shape_text(sub, texts)
        return
    if shape.has_text_frame:
        for para in shape.text_frame.paragraphs:
            t = para.text.strip()
            if t:
                texts.append(t)
    if getattr(shape, "has_table", False):
        try:
            tbl = shape.table
            for row in tbl.rows:
                cells = []
                for cell in row.cells:
                    c = (cell.text or "").strip().replace('|', '\\|').replace('\n', '<br>')
                    cells.append(c)
                if any(cells):
                    texts.append('| ' + ' | '.join(cells) + ' |')
        except Exception:
            pass


class PPTXConverter(BaseConverter):
    """PPTX → MD (python-pptx提取每页文字/表格/备注)"""

    def supported_extensions(self) -> list:
        return ['.pptx']

    def convert(self, file_path: str, **kwargs) -> str:
        prs = Presentation(file_path)
        parts = [f'# 幻灯片摘要\n\n共 {len(prs.slides)} 页\n']
        for i, slide in enumerate(prs.slides):
            texts = []
            for shape in slide.shapes:
                _shape_text(shape, texts)
            slide_parts = []
            if texts:
                slide_parts.append('\n\n'.join(texts))
            # 演讲者备注
            try:
                if slide.has_notes_slide:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                    if notes:
                        slide_parts.append(f'> **备注**: {notes}')
            except Exception:
                pass
            if slide_parts:
                parts.append(f'\n## 第 {i+1} 页\n')
                parts.append('\n\n'.join(slide_parts))
        return '\n'.join(parts)
