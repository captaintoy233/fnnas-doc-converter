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

    def convert_to_files(self, file_path: str, rel_path: str) -> list:
        """转换为一个或多个 Markdown 文件（默认单文件；CHM 等多文件格式可重写）

        Args:
            file_path: 源文件绝对路径
            rel_path:  源文件相对路径（用于生成输出相对路径）

        Returns:
            list[(rel_output_path, content)]，路径相对输出目录
        """
        from pathlib import Path
        return [(str(Path(rel_path).with_suffix(".md")), self.convert(file_path))]

    @property
    def display_name(self) -> str:
        return self.__class__.__name__.replace('Converter', '')

    @property
    def available(self) -> bool:
        """该转换器是否真正可用（占位转换器返回 False）"""
        return True

    @property
    def description(self) -> str:
        return (self.__doc__ or "").strip()


class ConverterRegistry:
    """转换器注册表 - 管理所有格式的转换器

    - 真实转换器与占位转换器分表存储：同一扩展名有真实转换器时，
      占位转换器不会覆盖它（修复旧版 PDF 占位符覆盖 PDFConverter 的问题）。
    - get() 优先返回真实转换器，找不到时回退到占位转换器（便于提示）。
    """

    def __init__(self):
        self._converters: dict[str, BaseConverter] = {}
        self._placeholders: dict[str, BaseConverter] = {}

    def register(self, converter: BaseConverter):
        target = self._placeholders if not converter.available else self._converters
        for ext in converter.supported_extensions():
            target[ext.lower()] = converter

    def rebuild(self, register_fn=None):
        """清空并重建注册表（配置热更新后调用）"""
        self._converters.clear()
        self._placeholders.clear()
        if register_fn is not None:
            register_fn(self)

    def get(self, ext: str) -> Optional[BaseConverter]:
        ext = ext.lower()
        if ext in self._converters:
            return self._converters[ext]
        return self._placeholders.get(ext)

    def is_available(self, ext: str) -> bool:
        return ext.lower() in self._converters

    def get_all_formats(self) -> list[dict]:
        """按显示名分组返回格式列表；status 区分 available / pending"""
        seen = {}
        for conv in list(self._converters.values()) + list(self._placeholders.values()):
            name = conv.display_name
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "extensions": set(),
                    "available": False,
                    "description": conv.description,
                }
            info = seen[name]
            exts = [e for e, c in list(self._converters.items())
                    + list(self._placeholders.items()) if c is conv]
            info["extensions"].update(e.lower() for e in exts)
            if conv.available:
                info["available"] = True

        formats = []
        for name, info in sorted(seen.items()):
            formats.append({
                "name": name,
                "extensions": sorted(info["extensions"]),
                "status": "available" if info["available"] else "pending",
                "description": info["description"],
            })
        return formats

    def list_supported_extensions(self) -> list[str]:
        return sorted(self._converters.keys())


# 全局注册表
registry = ConverterRegistry()
