"""表格类转换的统一规模护栏（xlsx / et / xls 共用）。

为什么必须有：真实邮件语料里一个 3.59 MB 的 `.xlsx` 附件能解出 **1459 万字符**，
openpyxl 峰值内存 **1.27 GB**、耗时 83 秒（实测，见 EML 方案评估）。
容器 mem_limit 1.5 G 下这直接 OOM；即便不 OOM，14 MB 的 Markdown 对 RAG 也毫无价值——
切分后全是表格碎片，反而污染检索。

策略：
1. `read_only=True` 流式读（xlsx），大幅降低峰值内存；
2. 行列与字符三重上限，任一触顶即停止并写**截断说明**；
3. 说明里给出真实总行/总列，让读者和模型知道"这只是前 N 行"。

上限可用配置 `spreadsheet.*` 或环境变量覆盖：
    SPREADSHEET_MAX_ROWS=3000
    SPREADSHEET_MAX_COLS=80
    SPREADSHEET_MAX_CELLS=200000
    SPREADSHEET_MAX_CHARS=120000
"""
from __future__ import annotations

import os

DEFAULTS = {
    # 每个 sheet 最多保留的行（含表头行）
    "max_rows": 3000,
    # 最多保留的列（超宽表只取左侧列，右侧通常是备注/公式列）
    "max_cols": 80,
    # 每个 sheet 最多处理的单元格数（行 × 列，防超宽超长组合）
    "max_cells": 200000,
    # 每个 sheet 最多产出的字符数（粗粒度兜底）
    "max_chars": 120000,
    # 一个工作簿最多处理多少个 sheet（多标签台账常见上百个 sheet）
    "max_sheets": 20,
    # xlsx 是否用 openpyxl 的只读模式（流式，省内存）
    "read_only": True,
}

_ENV = {
    "max_rows": "SPREADSHEET_MAX_ROWS",
    "max_cols": "SPREADSHEET_MAX_COLS",
    "max_cells": "SPREADSHEET_MAX_CELLS",
    "max_chars": "SPREADSHEET_MAX_CHARS",
    "max_sheets": "SPREADSHEET_MAX_SHEETS",
    "read_only": "SPREADSHEET_READ_ONLY",
}


def get_limits() -> dict:
    """取当前生效的护栏（配置优先，其次环境变量，最后默认值）"""
    limits = dict(DEFAULTS)
    try:
        from config import get_config
        cfg = (get_config() or {}).get("spreadsheet") or {}
        for k in DEFAULTS:
            if k in cfg and cfg[k] is not None:
                limits[k] = cfg[k]
    except Exception:
        pass
    for k, env in _ENV.items():
        val = os.environ.get(env)
        if val is None:
            continue
        try:
            limits[k] = (val.lower() in ("1", "true", "yes", "on")) if k == "read_only" else int(val)
        except ValueError:
            pass
    return limits


class SheetBudget:
    """单个 sheet 的行/列/单元格/字符预算计数器。

    用法：
        budget = SheetBudget(limits, total_rows=nrows, total_cols=ncols)
        for row in ...:
            if not budget.take_row(row_chars=...):
                break
            budget.use(row_chars=...)
        note = budget.note()      # 触顶时返回截断说明，否则空串
    """

    def __init__(self, limits: dict, total_rows: int = 0, total_cols: int = 0):
        self.max_rows = int(limits.get("max_rows", DEFAULTS["max_rows"]))
        self.max_cols = int(limits.get("max_cols", DEFAULTS["max_cols"]))
        self.max_cells = int(limits.get("max_cells", DEFAULTS["max_cells"]))
        self.max_chars = int(limits.get("max_chars", DEFAULTS["max_chars"]))
        self.max_sheets = int(limits.get("max_sheets", DEFAULTS["max_sheets"]))
        self.total_rows = total_rows
        self.total_cols = total_cols
        self.rows = 0
        self.cells = 0
        self.chars = 0
        self.truncated_by = ""

    def take_sheet(self, index: int) -> bool:
        """是否还能处理第 index 个 sheet（从 0 计）"""
        if self.max_sheets > 0 and index >= self.max_sheets:
            return False
        return True

    def sheet_note(self, skipped_sheets: int) -> str:
        """被 sheet 数上限截断时的说明"""
        if skipped_sheets <= 0:
            return ""
        return ("⚠️ 工作簿共 {} 个 sheet，仅处理前 {} 个"
                "（SPREADSHEET_MAX_SHEETS 可调），其余请查阅原始文件。").format(
                    skipped_sheets + self.max_sheets, self.max_sheets)

    def clamp_cols(self, ncols: int) -> int:
        """返回允许处理的列数，并记录是否被列上限截断"""
        if self.max_cols > 0 and ncols > self.max_cols:
            self.truncated_by = self.truncated_by or "列数"
            return self.max_cols
        return ncols

    def take_row(self, ncols: int = 0) -> bool:
        """是否还能再收一行（在真正处理该行之前调用）"""
        if self.max_rows > 0 and self.rows >= self.max_rows:
            self.truncated_by = self.truncated_by or "行数"
            return False
        if self.max_cells > 0 and self.cells + max(1, ncols) > self.max_cells:
            self.truncated_by = self.truncated_by or "单元格数"
            return False
        if self.max_chars > 0 and self.chars >= self.max_chars:
            self.truncated_by = self.truncated_by or "字符数"
            return False
        return True

    def use(self, ncols: int = 0, chars: int = 0) -> None:
        self.rows += 1
        self.cells += max(1, ncols)
        self.chars += chars

    def note(self) -> str:
        """截断说明（未截断时返回空串）"""
        if not self.truncated_by:
            return ""
        total = ""
        if self.total_rows:
            total += "共 {} 行".format(self.total_rows)
        if self.total_cols:
            total += ("×{} 列".format(self.total_cols) if total else "共 {} 列".format(self.total_cols))
        return ("⚠️ 表格过大已截断（触发{}上限）：仅保留前 {} 行{}，"
                "完整数据请查阅原始文件。").format(
                    self.truncated_by, self.rows,
                    "（原表{}）".format(total) if total else "")
