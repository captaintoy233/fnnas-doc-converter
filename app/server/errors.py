"""
结构化异常体系

借鉴 AnyDoc 的 ConvertError 设计，提供明确的错误分类：
- ConversionError: 基类
- UnsupportedFormatError: 不支持的格式
- NeedsOcrError: 需要 OCR（含具体页码）
- MalformedDocumentError: 文档结构损坏
- EncryptedDocumentError: 加密文件
- ResourceLimitError: 资源超限（解压/嵌套/节点数）
- MissingPartError: 关键部分缺失

这些异常让调用方能精确区分"无法转换"和"转换质量差"，
便于后续处理策略（如回退推送原始文件）。
"""
from __future__ import annotations
from typing import List, Optional


class ConversionError(Exception):
    """转换错误基类"""
    
    def __init__(self, message: str, file_path: Optional[str] = None):
        self.file_path = file_path
        super().__init__(message)
    
    @property
    def error_code(self) -> str:
        """错误代码，用于程序化处理"""
        return "conversion_error"


class UnsupportedFormatError(ConversionError):
    """不支持的格式或无法识别的文件"""
    
    def __init__(self, message: str, format_name: Optional[str] = None, **kwargs):
        self.format_name = format_name
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "unsupported_format"


class NeedsOcrError(ConversionError):
    """PDF 需要 OCR（扫描/图片型页面）"""
    
    def __init__(
        self,
        message: str = "PDF contains scanned/image-only pages",
        pages: Optional[List[int]] = None,
        **kwargs
    ):
        self.pages = pages or []  # 需要 OCR 的页码列表（0-indexed）
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "needs_ocr"
    
    @property
    def page_count(self) -> int:
        return len(self.pages)


class MalformedDocumentError(ConversionError):
    """文档结构损坏，无法提取有意义内容"""
    
    def __init__(self, message: str = "Document is malformed", **kwargs):
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "malformed_document"


class EncryptedDocumentError(ConversionError):
    """加密或密码保护的文件"""
    
    def __init__(self, message: str = "Document is encrypted", **kwargs):
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "encrypted_document"


class ResourceLimitError(ConversionError):
    """超出安全限制（解压大小、嵌套深度、节点数等）"""
    
    def __init__(
        self,
        message: str = "Resource limit exceeded",
        limit_type: Optional[str] = None,
        limit_value: Optional[int] = None,
        **kwargs
    ):
        self.limit_type = limit_type      # 如 "decompression_size", "nesting_depth"
        self.limit_value = limit_value
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "resource_limit"


class MissingPartError(ConversionError):
    """文档缺少关键部分"""
    
    def __init__(self, message: str = "Required part is missing", part_name: Optional[str] = None, **kwargs):
        self.part_name = part_name
        super().__init__(message, **kwargs)
    
    @property
    def error_code(self) -> str:
        return "missing_part"
