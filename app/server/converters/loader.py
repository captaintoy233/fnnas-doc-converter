"""
转换器加载器: 注册全部内置转换器

统一入口，确保无论从 main.py 还是 CLI 启动，注册表都完整。
"""
from converters import registry
from converters.ofd import OFDConverter
from converters.wps import WPSConverter
from converters.docx_converter import DOCXConverter
from converters.xlsx_converter import XLSXConverter
from converters.et_converter import ETConverter
from converters.pdf_converter import PDFConverter
from converters.htm_converter import HTMConverter
from converters.pptx_converter import PPTXConverter
from converters.chm_converter import CHMConverter
from converters.archive_converter import ArchiveConverter
from converters.eml_converter import EMLConverter, MSGConverter
from converters.ooxml_compat import WPSXConverter, DPSXConverter, ETXConverter
from converters.placeholder_converter import PlaceholderConverter


def register_all(reg=None, config: dict = None) -> None:
    """注册全部转换器到指定注册表（默认全局 registry）"""
    if reg is None:
        reg = registry
    if config is None:
        from config import get_config
        config = get_config()

    reg.register(OFDConverter())
    reg.register(WPSConverter())
    reg.register(DOCXConverter())
    reg.register(XLSXConverter())
    reg.register(ETConverter())
    reg.register(PDFConverter())
    reg.register(HTMConverter())
    reg.register(PPTXConverter())
    reg.register(CHMConverter(
        seven_zip_path=(config.get("chm", {}) or {}).get("seven_zip_path", "7zz"),
        workers=int((config.get("chm", {}) or {}).get("extract_workers", 4)),
    ))
    reg.register(ArchiveConverter())
    reg.register(EMLConverter())
    reg.register(MSGConverter())
    reg.register(WPSXConverter())
    reg.register(DPSXConverter())
    reg.register(ETXConverter())
    reg.register(PlaceholderConverter("PPT(旧版)", ['.ppt']))
    reg.register(PlaceholderConverter("DPS(WPS演示)", ['.dps']))
    reg.register(PlaceholderConverter("DOC(旧版)", ['.doc']))
    reg.register(PlaceholderConverter("XLS(旧版)", ['.xls']))
