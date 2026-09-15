"""
统一文档模型 (Document Model)

借鉴 AnyDoc 的架构设计：所有格式先解析为统一的中间模型，
再通过单一序列化器输出 Markdown。这样表格转义、标题锚点、
列表嵌套等逻辑只需修复一次，所有格式受益。

核心类型:
- Document: 顶层文档容器
- Block: 块级元素（段落、标题、表格、代码块等）
- Inline: 行内元素（文本、加粗、斜体、链接等）
- Table: 表格结构
- Asset: 嵌入资源（图片等）
"""

from .document import Document, Note, NoteKind
from .block import Block, Heading, Paragraph, CodeBlock, BlockQuote, ThematicBreak
from .inline import Inline, Text, Bold, Italic, Strikethrough, InlineCode, Link, Image, NoteRef
from .table import Table, TableRow, TableCell
from .list import ListBlock, ListItem, MarkerKind
from .asset import Asset, AssetId

__all__ = [
    # Document
    "Document", "Note", "NoteKind",
    # Blocks
    "Block", "Heading", "Paragraph", "CodeBlock", "BlockQuote", "ThematicBreak",
    # Inlines
    "Inline", "Text", "Bold", "Italic", "Strikethrough", "InlineCode", "Link", "Image", "NoteRef",
    # Table
    "Table", "TableRow", "TableCell",
    # List
    "ListBlock", "ListItem", "MarkerKind",
    # Asset
    "Asset", "AssetId",
]
