"""
Markdown 序列化器

将 Document Model 序列化为 GitHub-Flavored Markdown。
所有格式转换器共享此序列化器，确保输出一致性。

设计原则:
- 表格转义、标题锚点、列表嵌套等逻辑只在此处实现一次
- 合并 3 个以上连续空行为 2 个
- 自动处理特殊字符转义
"""
from __future__ import annotations
import re
from typing import List, Optional

from model.document import Document, Note, NoteKind
from model.block import Block, Heading, Paragraph, CodeBlock, BlockQuote, ThematicBreak
from model.inline import Inline, Text, Bold, Italic, Strikethrough, InlineCode, Link, Image, NoteRef
from model.table import Table, TableRow, TableCell
from model.list import ListBlock, ListItem, MarkerKind


def document_to_markdown(doc: Document) -> str:
    """将 Document 转换为 Markdown 字符串"""
    renderer = MarkdownRenderer()
    return renderer.render(doc)


# 别名，方便外部调用
render_markdown = document_to_markdown


class MarkdownRenderer:
    """Markdown 渲染器"""
    
    def __init__(self):
        self._note_counter = 0
        self._note_map: dict[str, int] = {}
    
    def render(self, doc: Document) -> str:
        """渲染完整文档"""
        parts = []
        
        # 元数据（可选）
        if doc.title:
            parts.append(f"# {doc.title}\n")
        if doc.author:
            parts.append(f"> **作者**: {doc.author}\n")
        
        # 正文
        for block in doc.blocks:
            md = self._render_block(block)
            if md is not None:
                parts.append(md)
        
        # 脚注/尾注
        if doc.notes:
            parts.append("\n---\n")
            for note in doc.notes:
                num = self._note_map.get(note.id, 0)
                content = self._render_blocks_inline(note.blocks)
                parts.append(f"[^{num}]: {content}")
        
        result = "\n".join(parts)
        
        # 清理：合并 3+ 连续空行为 2 个
        result = re.sub(r'\n{4,}', '\n\n\n', result)
        
        return result.strip()
    
    def _render_block(self, block: Block) -> Optional[str]:
        """渲染单个块级元素"""
        if isinstance(block, Heading):
            prefix = "#" * block.level
            content = self._render_inlines(block.children)
            return f"\n{prefix} {content}\n"
        
        elif isinstance(block, Paragraph):
            if block.is_empty():
                return ""
            content = self._render_inlines(block.children)
            return content
        
        elif isinstance(block, Table):
            return self._render_table(block)
        
        elif isinstance(block, ListBlock):
            return self._render_list(block)
        
        elif isinstance(block, CodeBlock):
            lang = block.language or ""
            return f"\n```{lang}\n{block.content}\n```\n"
        
        elif isinstance(block, BlockQuote):
            inner = self._render_blocks_inline(block.blocks)
            lines = inner.split("\n")
            quoted = "\n".join(f"> {line}" for line in lines)
            return f"\n{quoted}\n"
        
        elif isinstance(block, ThematicBreak):
            return "\n---\n"
        
        return None
    
    def _render_blocks_inline(self, blocks: List[Block]) -> str:
        """将多个块渲染为内联文本（用于注释等场景）"""
        parts = []
        for block in blocks:
            md = self._render_block(block)
            if md:
                parts.append(md.strip())
        return " ".join(parts)
    
    def _render_inlines(self, children: List[Inline]) -> str:
        """渲染行内元素列表"""
        parts = []
        for child in children:
            parts.append(self._render_inline(child))
        return "".join(parts)
    
    def _render_inline(self, inline: Inline) -> str:
        """渲染单个行内元素"""
        if isinstance(inline, Text):
            return inline.content
        
        elif isinstance(inline, Bold):
            content = self._render_inlines(inline.children)
            return f"**{content}**"
        
        elif isinstance(inline, Italic):
            content = self._render_inlines(inline.children)
            return f"*{content}*"
        
        elif isinstance(inline, Strikethrough):
            content = self._render_inlines(inline.children)
            return f"~~{content}~~"
        
        elif isinstance(inline, InlineCode):
            return f"`{inline.content}`"
        
        elif isinstance(inline, Link):
            text = self._render_inlines(inline.children)
            title = f' "{inline.title}"' if inline.title else ""
            return f"[{text}]({inline.url}{title})"
        
        elif isinstance(inline, Image):
            title = f' "{inline.title}"' if inline.title else ""
            return f"![{inline.alt}]({inline.src}{title})"
        
        elif isinstance(inline, NoteRef):
            if inline.id not in self._note_map:
                self._note_counter += 1
                self._note_map[inline.id] = self._note_counter
            num = self._note_map[inline.id]
            return f"[^{num}]"
        
        return ""
    
    def _render_table(self, table: Table) -> str:
        """
        渲染表格为 Markdown 表格
        
        处理要点:
        - 单元格内容转义 | 和换行
        - 空行跳过
        - 第一行作为表头
        """
        if table.is_empty():
            return ""
        
        lines = []
        
        # 可选的表格标题
        if table.caption:
            lines.append(f"**{table.caption}**\n")
        
        for ri, row in enumerate(table.rows):
            cells = []
            for cell in row.cells:
                text = self._escape_cell(cell.text)
                cells.append(text)
            
            # 去除尾部空单元格
            while cells and cells[-1] == "":
                cells.pop()
            
            if not cells or all(c == "" for c in cells):
                continue
            
            lines.append("| " + " | ".join(cells) + " |")
            
            # 在第一行后添加分隔线
            if ri == 0:
                lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
        
        return "\n" + "\n".join(lines) + "\n" if lines else ""
    
    def _render_list(self, lst: ListBlock, indent: int = 0) -> str:
        """渲染列表（支持嵌套）"""
        lines = []
        prefix = "  " * indent
        
        for i, item in enumerate(lst.items):
            # 确定标记
            if lst.marker == MarkerKind.BULLET:
                marker = "- "
            elif lst.marker == MarkerKind.NUMBERED:
                marker = f"{lst.start + i}. "
            elif lst.marker == MarkerKind.TASK:
                checked = "[x]" if item.checked else "[ ]"
                marker = f"- {checked} "
            else:
                marker = "- "
            
            # 渲染列表项内容
            item_parts = []
            for block in item.children:
                md = self._render_block(block)
                if md:
                    item_parts.append(md.strip())
            
            content = " ".join(item_parts) if item_parts else ""
            lines.append(f"{prefix}{marker}{content}")
        
        return "\n".join(lines)
    
    @staticmethod
    def _escape_cell(text: str) -> str:
        """表格单元格文本转义"""
        if not text:
            return ""
        # 转义反斜杠和管道符
        text = text.replace("\\", "\\\\").replace("|", "\\|")
        # 换行转 <br>
        text = text.replace("\r\n", "<br>").replace("\n", "<br>")
        return text.strip()
