"""行内元素定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Union, Optional


@dataclass
class Text:
    """纯文本"""
    content: str


@dataclass
class Bold:
    """加粗"""
    children: List["Inline"] = field(default_factory=list)


@dataclass
class Italic:
    """斜体"""
    children: List["Inline"] = field(default_factory=list)


@dataclass
class Strikethrough:
    """删除线"""
    children: List["Inline"] = field(default_factory=list)


@dataclass
class InlineCode:
    """行内代码"""
    content: str


@dataclass
class Link:
    """链接"""
    url: str
    title: Optional[str] = None         # 可选的 title 属性
    children: List["Inline"] = field(default_factory=list)


@dataclass
class Image:
    """图片引用"""
    src: str                            # URL 或 asset ID
    alt: str = ""                       # 替代文本
    title: Optional[str] = None


@dataclass
class NoteRef:
    """脚注/尾注引用"""
    id: str                             # 对应 Note.id


# Inline 联合类型
Inline = Union[Text, Bold, Italic, Strikethrough, InlineCode, Link, Image, NoteRef]


def inlines_to_plain_text(children: List[Inline]) -> str:
    """将行内元素列表转为纯文本"""
    parts = []
    for child in children:
        if isinstance(child, Text):
            parts.append(child.content)
        elif isinstance(child, (Bold, Italic, Strikethrough)):
            parts.append(inlines_to_plain_text(child.children))
        elif isinstance(child, InlineCode):
            parts.append(child.content)
        elif isinstance(child, Link):
            parts.append(inlines_to_plain_text(child.children))
        elif isinstance(child, Image):
            parts.append(child.alt or "")
        elif isinstance(child, NoteRef):
            pass  # 注释引用不输出文本
    return "".join(parts)


def inlines_are_empty(children: List[Inline]) -> bool:
    """检查行内元素列表是否为空（无可见内容）"""
    return not inlines_to_plain_text(children).strip()


def checkbox_text(checked: bool) -> str:
    """生成任务列表复选框文本"""
    return "[x]" if checked else "[ ]"
