"""表格结构定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TableCell:
    """表格单元格"""
    children: List["Inline"] = field(default_factory=list)  # 单元格内容
    colspan: int = 1                    # 列合并数
    rowspan: int = 1                    # 行合并数
    is_header: bool = False             # 是否为表头
    
    @property
    def text(self) -> str:
        from .inline import inlines_to_plain_text
        return inlines_to_plain_text(self.children)


@dataclass
class TableRow:
    """表格行"""
    cells: List[TableCell] = field(default_factory=list)
    is_header: bool = False             # 整行是否为表头


@dataclass
class Table:
    """
    表格
    
    支持简单表格和复杂表格（合并单元格）。
    Markdown 序列化时，合并单元格会被展开或简化处理。
    """
    rows: List[TableRow] = field(default_factory=list)
    caption: Optional[str] = None       # 表格标题
    
    @property
    def row_count(self) -> int:
        return len(self.rows)
    
    @property
    def col_count(self) -> int:
        if not self.rows:
            return 0
        return max(sum(c.colspan for c in row.cells) for row in self.rows)
    
    def is_empty(self) -> bool:
        return not self.rows or all(
            all(not cell.text.strip() for cell in row.cells)
            for row in self.rows
        )


# 延迟导入
from .inline import Inline
