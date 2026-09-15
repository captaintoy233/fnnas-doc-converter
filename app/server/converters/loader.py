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
from converters.libreoffice_converter import LibreOfficeConverter
from converters.rtf_converter import RTFConverter
from converters.text_converter import TextConverter
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

    # HTML：启用 OCR 时默认同时把图片复制到文档旁（link），
    # 使图片里的文字可检索、原图也随文档可见
    _ocr_cfg = dict(config.get("ocr", {}) or {})
    _htm_cfg = config.get("htm", {}) or {}
    _htm_mode = _htm_cfg.get("image_mode") or (
        "link" if _ocr_cfg.get("enabled") else "none")
    reg.register(HTMConverter(image_mode=_htm_mode, ocr_cfg=_ocr_cfg))

    reg.register(PPTXConverter())
    _chm_cfg = config.get("chm", {}) or {}
    reg.register(CHMConverter(
        seven_zip_path=_chm_cfg.get("seven_zip_path", "7zz"),
        workers=int(_chm_cfg.get("extract_workers", 4)),
        layout=_chm_cfg.get("output_layout", "tree"),
        front_matter=bool(_chm_cfg.get("front_matter", True)),
        namespace_output=bool(_chm_cfg.get("namespace_output", True)),
        skip_scaffold=bool(_chm_cfg.get("skip_scaffold", True)),
        render_processes=bool(_chm_cfg.get("render_processes", True)),
        mp_threshold=int(_chm_cfg.get("mp_threshold", 50)),
        image_mode=_chm_cfg.get("image_mode") or (
            "link" if _ocr_cfg.get("enabled") else "none"),
        ocr_cfg=_ocr_cfg,
    ))
    reg.register(ArchiveConverter())
    reg.register(EMLConverter())
    reg.register(MSGConverter())
    reg.register(WPSXConverter())
    reg.register(DPSXConverter())
    reg.register(ETXConverter())

    # LibreOffice 后端：处理 .doc / .xls / .dps / .gd
    lo_config = config.get("libreoffice", {}) or {}
    reg.register(LibreOfficeConverter(
        soffice_path=lo_config.get("soffice_path", ""),
    ))

    # RTF 转换器
    reg.register(RTFConverter())

    # 纯文本类：.md 透传 / .txt 分段 / .csv 表格
    reg.register(TextConverter())

    # 仍为占位的格式（无纯 Python 方案且 LO 不一定可用）
    reg.register(PlaceholderConverter("PPT(旧版)", ['.ppt']))
