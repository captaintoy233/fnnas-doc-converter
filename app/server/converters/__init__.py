"""转换器基类和注册表"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseConverter(ABC):
    """所有转换器的抽象基类"""

    @abstractmethod
    def convert(self, file_path: str, **kwargs) -> str:
        """将输入文件转换为 Markdown，返回 Markdown 文本"""
        pass

    @abstractmethod
    def supported_extensions(self) -> list:
        """返回支持的文件扩展名列表"""
        pass

    @property
    def display_name(self) -> str:
        return self.__class__.__name__.replace('Converter', '')


class ConverterRegistry:
    """转换器注册表 - 管理所有格式的转换器"""

    def __init__(self):
        self._converters: dict[str, BaseConverter] = {}

    def register(self, converter: BaseConverter):
        for ext in converter.supported_extensions():
            self._converters[ext.lower()] = converter

    def get(self, ext: str) -> Optional[BaseConverter]:
        return self._converters.get(ext.lower())

    def get_all_formats(self) -> list[dict]:
        seen = set()
        formats = []
        for ext, conv in sorted(self._converters.items()):
            if conv.display_name not in seen:
                seen.add(conv.display_name)
                exts = [e for e, c in self._converters.items() if c is conv]
                formats.append({
                    "name": conv.display_name,
                    "extensions": exts,
                    "status": "available",
                    "description": conv.__doc__ or ""
                })
        return formats

    def list_supported_extensions(self) -> list[str]:
        return sorted(self._converters.keys())


# 全局注册表
registry = ConverterRegistry()
