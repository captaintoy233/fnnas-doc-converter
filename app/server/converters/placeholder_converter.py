"""占位转换器：需要系统工具 (LibreOffice/Tesseract) 或暂未实现的格式。"""
from . import BaseConverter


class PlaceholderConverter(BaseConverter):
    """占位：返回提示信息"""

    def __init__(self, name: str, exts: list, msg: str = ""):
        self._name = name
        self._exts = exts
        self._msg = msg or (
            "**暂不支持自动转换**\n\n"
            "该格式需要额外的系统工具（如 LibreOffice / OCR）才能转换为 Markdown，"
            "当前版本未集成。可将原始文件直接推送到 WeKnora 由服务端解析，"
            "或先手动转换为 DOCX/PDF 后再转换。"
        )

    def supported_extensions(self) -> list:
        return self._exts

    @property
    def display_name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return False

    def convert(self, file_path: str, **kwargs) -> str:
        return self._msg
