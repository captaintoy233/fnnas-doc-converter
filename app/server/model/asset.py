"""嵌入资源定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AssetId:
    """资源唯一标识符"""
    value: int
    
    def __str__(self) -> str:
        return f"asset-{self.value}"


@dataclass
class Asset:
    """
    嵌入资源（图片、OLE 对象等）
    
    资源字节存储在 Document.assets 列表中，
    Markdown 中用 alt text 或 asset ID 引用。
    """
    id: AssetId                         # 唯一 ID
    media_type: str                     # MIME 类型 (image/png, image/jpeg 等)
    data: bytes = b""                   # 原始字节数据
    filename: Optional[str] = None      # 原始文件名
    description: str = ""               # 描述/alt text
    
    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")
