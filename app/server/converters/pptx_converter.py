"""PPTX → Markdown。python-pptx 提取幻灯片文本。"""
from pptx import Presentation
from . import BaseConverter


class PPTXConverter(BaseConverter):
    """PPTX → MD (python-pptx提取每页文字)"""

    def supported_extensions(self) -> list:
        return ['.pptx']

    def convert(self, file_path: str, **kwargs) -> str:
        prs = Presentation(file_path)
        parts = [f'# 幻灯片摘要\n共 {len(prs.slides)} 页\n']
        for i, slide in enumerate(prs.slides):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            texts.append(t)
            if texts:
                parts.append(f'\n## 第{i+1}页\n')
                parts.append('\n\n'.join(texts))
        return '\n'.join(parts)
