"""占位转换器：需要系统工具 (LibreOffice/Tesseract) 的格式。"""
from . import BaseConverter


class PlaceholderConverter(BaseConverter):
    """占位：返回提示信息"""

    def __init__(self, name: str, exts: list, msg: str):
        self._name = name
        self._exts = exts
        self._msg = msg

    def supported_extensions(self) -> list:
        return self._exts

    @property
    def display_name(self) -> str:
        return self._name

    def convert(self, file_path: str, **kwargs) -> str:
        return self._msg
