"""列表结构定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class MarkerKind(Enum):
    """列表标记类型"""
    BULLET = "bullet"           # 无序列表 (-, *, +)
    NUMBERED = "numbered"       # 有序列表 (1., 2., ...)
    TASK = "task"               # 任务列表 (- [ ], - [x])


@dataclass
class ListItem:
    """列表项"""
    children: List["Block"] = field(default_factory=list)  # 列表项内容（可嵌套）
    checked: Optional[bool] = None      # 任务列表：True=已完成, False=未完成, None=非任务
    
    @property
    def is_task(self) -> bool:
        return self.checked is not None


@dataclass
class ListBlock:
    """
    列表块
    
    支持嵌套列表、有序/无序/任务列表。
    """
    items: List[ListItem] = field(default_factory=list)
    marker: MarkerKind = MarkerKind.BULLET
    start: int = 1                      # 有序列表起始编号
    
    @property
    def is_ordered(self) -> bool:
        return self.marker == MarkerKind.NUMBERED
    
    @property
    def is_task_list(self) -> bool:
        return self.marker == MarkerKind.TASK


# 延迟导入
from .block import Block
