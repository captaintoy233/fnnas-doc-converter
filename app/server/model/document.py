"""文档顶层容器"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class NoteKind(Enum):
    """注释类型"""
    FOOTNOTE = "footnote"  # 脚注（页底）
    ENDNOTE = "endnote"    # 尾注（文末）


@dataclass
class Note:
    """脚注或尾注的内容体"""
    id: str                           # 文档内唯一 ID
    kind: NoteKind                    # 脚注/尾注
    blocks: List["Block"] = field(default_factory=list)  # 注的内容


@dataclass
class Document:
    """
    统一文档模型 - 所有格式解析后的中间表示
    
    一个 Document 是自包含的：嵌入资源携带字节数据，
    源文件关闭后仍可序列化。
    """
    blocks: List["Block"] = field(default_factory=list)   # 正文内容（按阅读顺序）
    notes: List[Note] = field(default_factory=list)       # 脚注/尾注列表
    assets: List["Asset"] = field(default_factory=list)   # 嵌入资源（图片等）
    
    # 元数据（可选）
    title: Optional[str] = None
    author: Optional[str] = None
    
    def add_block(self, block: "Block") -> None:
        """添加块级元素"""
        self.blocks.append(block)
    
    def add_note(self, note: Note) -> str:
        """添加注释，返回其 ID"""
        self.notes.append(note)
        return note.id
    
    def add_asset(self, asset: "Asset") -> "AssetId":
        """添加嵌入资源，返回其 ID"""
        self.assets.append(asset)
        return asset.id
    
    def is_empty(self) -> bool:
        """文档是否为空"""
        return not self.blocks and not self.notes


# 避免循环导入：在模块加载后设置前向引用
from .block import Block
from .asset import Asset, AssetId
