"""
WPS Office 新版格式兼容转换器

- .wpsx (WPS文字)  → OOXML 文档，与 .docx 同结构
- .dpsx (WPS演示)  → OOXML 演示，与 .pptx 同结构
- .etx  (WPS表格)  → OLE2/BIFF 兼容，与 .et/.xls 同结构（xlrd 可读）
"""
from .docx_converter import DOCXConverter
from .pptx_converter import PPTXConverter
from .et_converter import ETConverter


class WPSXConverter(DOCXConverter):
    """WPS文字(x) → MD (OOXML)"""

    def supported_extensions(self) -> list:
        return ['.wpsx']


class DPSXConverter(PPTXConverter):
    """WPS演示(x) → MD (OOXML)"""

    def supported_extensions(self) -> list:
        return ['.dpsx']


class ETXConverter(ETConverter):
    """WPS表格(x) → MD (BIFF)"""

    def supported_extensions(self) -> list:
        return ['.etx']
