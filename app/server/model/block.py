"""块级元素定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Union, Optional


@dataclass
class Heading:
    """标题（1-6 级）"""
    level: int                          # 1-6
    children: List["Inline"] = field(default_factory=list)  # 行内内容
    
    @property
    def text(self) -> str:
        """纯文本内容（用于快速访问）"""
        from .inline import inlines_to_plain_text
        return inlines_to_plain_text(self.children)


@dataclass
class Paragraph:
    """段落"""
    children: List["Inline"] = field(default_factory=list)
    
    @property
    def text(self) -> str:
        from .inline import inlines_to_plain_text
        return inlines_to_plain_text(self.children)
    
    def is_empty(self) -> bool:
        from .inline import inlines_are_empty
        return inlines_are_empty(self.children)


@dataclass
class CodeBlock:
    """代码块"""
    language: Optional[str] = None      # 语言标识（如 python, sql）
    content: str = ""                   # 代码内容


@dataclass
class BlockQuote:
    """引用块"""
    blocks: List["Block"] = field(default_factory=list)


@dataclass
class ThematicBreak:
    """分隔线 (---)"""
    pass


# Block 联合类型
Block = Union[Heading, Paragraph, "Table", "ListBlock", CodeBlock, BlockQuote, ThematicBreak]

# 延迟导入避免循环
from .table import Table
from .list import ListBlock
from .inline import Inline
