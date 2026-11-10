"""XLSX → Markdown 表格。用 openpyxl。

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- 单元格转义、数值格式化、日期友好输出由共享序列化器处理

向后兼容:
- convert() 方法保持原有签名和返回类型
- 新增 convert_to_document() 返回 Document Model
"""
import openpyxl
from datetime import datetime, date
from . import BaseConverter

# Document Model 导入
from model.document import Document
from model.block import Heading, Paragraph as MdParagraph, Table as MdTable
from model.inline import Text
from model.table import TableRow, TableCell
from render.markdown import document_to_markdown


def _fmt_cell_value(cell_value) -> str:
    """格式化单元格值为可读文本"""
    if cell_value is None:
        return ""
    if isinstance(cell_value, float) and cell_value.is_integer():
        return str(int(cell_value))
    if isinstance(cell_value, (datetime, date)):
        try:
            fmt = "%Y-%m-%d %H:%M" if isinstance(cell_value, datetime) else "%Y-%m-%d"
            return cell_value.strftime(fmt)
        except Exception:
            return str(cell_value)
    return str(cell_value).strip()


class XLSXConverter(BaseConverter):
    """XLSX → Markdown表格 (openpyxl)"""

    def supported_extensions(self) -> list:
        return ['.xlsx']

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型（带规模护栏：只读流式 + 行列/字符上限）"""
        from .spreadsheet_limits import get_limits, SheetBudget
        limits = get_limits()
        wb = openpyxl.load_workbook(
            file_path, data_only=True, read_only=bool(limits.get("read_only", True)))
        result = Document()

        try:
            sheet_limits = SheetBudget(limits)
            for si, sn in enumerate(wb.sheetnames):
                if not sheet_limits.take_sheet(si):
                    result.add_block(MdParagraph(children=[Text(
                        sheet_limits.sheet_note(len(wb.sheetnames) - si))]))
                    break
                ws = wb[sn]

                # Sheet 标题
                result.add_block(Heading(level=1, children=[Text(sn)]))

                # 只读模式下 max_row/max_column 可能为 None
                total_rows = getattr(ws, "max_row", None) or 0
                total_cols = getattr(ws, "max_column", None) or 0
                budget = SheetBudget(limits, total_rows, total_cols)
                ncols = budget.clamp_cols(total_cols) if total_cols else 0

                rows = []
                for ri, row in enumerate(ws.iter_rows(values_only=True)):
                    if ncols:
                        row = row[:ncols]
                    if not budget.take_row(len(row)):
                        break
                    cells = []
                    row_chars = 0
                    for val in row:
                        text = _fmt_cell_value(val)
                        row_chars += len(text)
                        cells.append(TableCell(
                            children=[Text(text)] if text else [],
                            is_header=(ri == 0)
                        ))

                    # 跳过全空行
                    if all(not c.text.strip() for c in cells):
                        continue

                    budget.use(len(row), row_chars)
                    rows.append(TableRow(cells=cells, is_header=(ri == 0)))

                if rows:
                    result.add_block(MdTable(rows=rows))
                note = budget.note()
                if note:
                    result.add_block(MdParagraph(children=[Text(note)]))
        finally:
            try:
                wb.close()
            except Exception:
                pass

        return result

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
