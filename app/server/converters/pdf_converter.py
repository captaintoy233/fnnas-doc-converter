"""PDF → Markdown。文字型PDF用PyMuPDF提取，图片型PDF返回提示。"""
import fitz
from . import BaseConverter


class PDFConverter(BaseConverter):
    """PDF → MD (文字型: PyMuPDF, 图片型: 提示需OCR)"""

    def supported_extensions(self) -> list:
        return ['.pdf']

    def convert(self, file_path: str, **kwargs) -> str:
        doc = fitz.open(file_path)
        pages_text = []
        total_text = 0
        for page in doc:
            t = page.get_text().strip()
            pages_text.append(t)
            total_text += len(t)
        doc.close()

        if total_text == 0:
            return (u"**\u26a0\ufe0f \u65e0\u6cd5\u63d0\u53d6\u6587\u5b57**\n\n"
                    u"\u8be5PDF\u6587\u4ef6\u662f\u626b\u63cf\u4ef6/\u56fe\u7247\u578bPDF\uff0c\u65e0\u5d4c\u5165\u5f0f\u6587\u5b57\u3002\n"
                    u"\u8bf7\u4f7f\u7528 OCR \u5de5\u5177\uff08\u5982 Tesseract + OCRmyPDF\uff09\u9884\u5904\u7406\u540e\u518d\u8bd5\u3002\n"
                    u"\u6b64\u529f\u80fd\u5c06\u5728\u540e\u7eed\u7248\u672c\u4e2d\u96c6\u6210\u3002")
        return '\n\n'.join(pages_text).strip()
